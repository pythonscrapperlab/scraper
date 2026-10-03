"""E5 seven-day soak: one honest metrics row per day appended to ``docs/progress.md``.

The collector only *reads* (the local ``runs`` ledger, the scheduler log and database sizes) and
never invents a day: a day the collector did not run has no row, and the report says so.
"""

from __future__ import annotations

import json
import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

SECTION = "## E5 seven-day soak"
HEADER = (
    "| day (local) | window UTC | checks ok/failed | refreshes ok/failed | refresh p50 / max min "
    "| block rate | late events | alerts / briefs queued | local DB MiB | cloud DB MiB |"
)
RULE = "|---|---|---|---|---:|---:|---:|---|---:|---:|"
LATE_MARKER = "Market is late"
ALERT_PATTERN = re.compile(r"Alerts enqueued: .*? events=(\d+) emails=(\d+)")
BRIEF_PATTERN = re.compile(r"Brief sweep: (\{.*\})")


@dataclass(frozen=True)
class DayMetrics:
    """Everything one soak row reports. All fields are aggregates; none identify a listing."""

    day: date
    start: datetime
    end: datetime
    checks_ok: int
    checks_failed: int
    refreshes_ok: int
    refreshes_failed: int
    refresh_p50_min: float | None
    refresh_max_min: float | None
    block_rate: float | None  # blocked / attempts over detail fetches; None = no fetches
    late_events: int
    alert_emails: int
    briefs_enqueued: int
    local_db_mib: float | None
    cloud_db_mib: float | None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def summarize_day(
    runs: Iterable[Mapping[str, Any]],
    log_lines: Iterable[str],
    *,
    day: date,
    start: datetime,
    end: datetime,
    local_db_bytes: int | None,
    cloud_db_mib: float | None,
) -> DayMetrics:
    """Aggregate one trailing window. Runs/log lines outside ``[start, end)`` are ignored."""
    in_window = [r for r in runs if start <= _utc(r["started_at"]) < end]
    checks = [r for r in in_window if r["kind"] == "check"]
    refreshes = [r for r in in_window if r["kind"] == "refresh"]

    def failed(item: Mapping[str, Any]) -> bool:
        return str(item["status"]) in {"failed", "cancelled"}

    durations = [float(r["duration_s"]) / 60 for r in refreshes if r["duration_s"] is not None]
    attempts = blocked = 0
    for run in refreshes:
        counts = run.get("counts") or {}
        attempts += int(counts.get("attempts", 0) or 0)
        blocked += int(counts.get("blocked", 0) or 0)

    late = alerts = briefs = 0
    for line in log_lines:
        try:
            record = json.loads(line)
            stamp = _utc(datetime.fromisoformat(record["ts"]))
        except (ValueError, KeyError, TypeError):
            continue
        if not start <= stamp < end:
            continue
        message = str(record.get("msg", ""))
        if LATE_MARKER in message:
            late += 1
        if match := ALERT_PATTERN.search(message):
            alerts += int(match.group(2))
        if match := BRIEF_PATTERN.search(message):
            try:
                briefs += int(json.loads(match.group(1)).get("brief_enqueued", 0))
            except ValueError:
                continue

    return DayMetrics(
        day=day, start=start, end=end,
        checks_ok=sum(not failed(r) and r["status"] == "succeeded" for r in checks),
        checks_failed=sum(failed(r) for r in checks),
        refreshes_ok=sum(not failed(r) and r["status"] == "succeeded" for r in refreshes),
        refreshes_failed=sum(failed(r) for r in refreshes),
        refresh_p50_min=round(statistics.median(durations), 1) if durations else None,
        refresh_max_min=round(max(durations), 1) if durations else None,
        block_rate=round(blocked / attempts, 4) if attempts else None,
        late_events=late, alert_emails=alerts, briefs_enqueued=briefs,
        local_db_mib=None if local_db_bytes is None else round(local_db_bytes / 1048576, 1),
        cloud_db_mib=cloud_db_mib,
    )


def _fmt(value: float | None, suffix: str = "") -> str:
    return "n/a" if value is None else f"{value}{suffix}"


def render_row(metrics: DayMetrics) -> str:
    rate = "n/a" if metrics.block_rate is None else f"{metrics.block_rate:.2%}"
    return (
        f"| {metrics.day.isoformat()} | {metrics.start:%m-%d %H:%M}Z → {metrics.end:%m-%d %H:%M}Z "
        f"| {metrics.checks_ok}/{metrics.checks_failed} "
        f"| {metrics.refreshes_ok}/{metrics.refreshes_failed} "
        f"| {_fmt(metrics.refresh_p50_min)} / {_fmt(metrics.refresh_max_min)} "
        f"| {rate} | {metrics.late_events} "
        f"| {metrics.alert_emails} / {metrics.briefs_enqueued} "
        f"| {_fmt(metrics.local_db_mib)} | {_fmt(metrics.cloud_db_mib)} |"
    )


def upsert_row(document: str, row: str, day: date, *, label: str, started: datetime) -> str:
    """Insert or replace the row for ``day`` in the soak table, creating it on first use."""
    lines = document.rstrip("\n").split("\n") if document.strip() else ["# Progress"]
    prefix = f"| {day.isoformat()} |"
    if SECTION not in lines:
        lines += [
            "", SECTION, "",
            f"Soak `{label}` started {started:%Y-%m-%d %H:%MZ}. One row per day from `main.py "
            "scheduler soak-daily`; a day with no row means the collector did not run - "
            "nothing is back-filled.",
            "", HEADER, RULE,
        ]
    start = lines.index(SECTION)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    for index in range(start, end):
        if lines[index].startswith(prefix):
            lines[index] = row
            break
    else:
        # Append after the last table row (or the rule) inside the section.
        insert_at = end
        while insert_at > start and not lines[insert_at - 1].startswith("|"):
            insert_at -= 1
        lines.insert(insert_at, row)
    return "\n".join(lines) + "\n"


def log_files(directory: Path = Path("logs")) -> list[Path]:
    return sorted(directory.glob("scheduler.jsonl*"))


def read_log_lines(paths: Sequence[Path], since: datetime) -> list[str]:
    """Lines from files touched since the window opened (bounded read, text only)."""
    lines: list[str] = []
    for path in paths:
        try:
            if datetime.fromtimestamp(path.stat().st_mtime, UTC) < since:
                continue
            lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            continue
    return lines


def window_for(now: datetime, hours: int = 24) -> tuple[datetime, datetime]:
    end = _utc(now)
    return end - timedelta(hours=hours), end
