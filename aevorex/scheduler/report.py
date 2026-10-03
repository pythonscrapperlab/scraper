"""Soak-run bookkeeping: publisher status before/after plus per-run metrics from the ledger."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from aevorex.db.models import Run
from aevorex.db.session import async_session_maker

SOAK_DIR = Path("logs") / "soak"
SUMMED = (
    "listings", "events", "changed", "queued", "pages", "success", "failed", "attempts",
    "http_405", "blocked", "properties", "scores", "change_events",
)


def start_path(label: str) -> Path:
    return SOAK_DIR / f"{label}.json"


def write_start(label: str, status: Mapping[str, Any], started_at: datetime) -> Path:
    """Persist the 'before' snapshot. Status contains counts and timestamps only."""
    SOAK_DIR.mkdir(parents=True, exist_ok=True)
    path = start_path(label)
    path.write_text(
        json.dumps({"label": label, "started_at": started_at.isoformat(), "before": status},
                   default=str, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


def read_start(label: str) -> dict[str, Any]:
    return json.loads(start_path(label).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


async def load_runs(since: datetime) -> list[dict[str, Any]]:
    naive = since.astimezone(UTC).replace(tzinfo=None) if since.tzinfo else since
    async with async_session_maker() as session:
        rows = (
            await session.execute(
                select(Run).where(Run.started_at >= naive).order_by(Run.started_at)
            )
        ).scalars().all()
    return [
        {
            "kind": r.kind, "scope": dict(r.scope or {}), "status": r.status,
            "counts": dict(r.counts or {}), "error_class": r.error_class,
            "duration_s": r.duration_s, "started_at": r.started_at,
        }
        for r in rows
    ]


def summarize_runs(runs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per (kind, city): counts, durations and summed Redfin-facing counters."""
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for run in runs:
        if run["kind"] == "scheduler":
            continue
        city = str(run["scope"].get("city") or "*")
        groups[(str(run["kind"]), city)].append(run)
    rows: list[dict[str, Any]] = []
    for (kind, city), items in sorted(groups.items()):
        durations = [float(i["duration_s"]) for i in items if i["duration_s"] is not None]
        sums: dict[str, float] = defaultdict(float)
        for item in items:
            for key in SUMMED:
                value = item["counts"].get(key)
                if isinstance(value, int | float) and not isinstance(value, bool):
                    sums[key] += value
        errors: dict[str, int] = defaultdict(int)
        for item in items:
            if item["error_class"]:
                errors[str(item["error_class"])] += 1
        rows.append({
            "kind": kind, "city": city, "runs": len(items),
            "succeeded": sum(i["status"] == "succeeded" for i in items),
            "failed": sum(i["status"] in {"failed", "cancelled"} for i in items),
            "p50_s": round(statistics.median(durations), 1) if durations else None,
            "max_s": round(max(durations), 1) if durations else None,
            "sums": {k: int(v) for k, v in sums.items()},
            "errors": dict(errors),
        })
    return rows


def _market_table(snapshot: Mapping[str, Any]) -> list[str]:
    lines = ["| market | status | active | last checked | last refreshed |", "|---|---|---:|---|---|"]
    for m in snapshot.get("markets", []):
        lines.append(
            f"| {m['slug']} | {m['check_status']} | {m['listings_active']} | "
            f"{m['last_checked_at']} | {m['last_refreshed_at']} |"
        )
    return lines


def render_report(
    label: str,
    started_at: datetime,
    finished_at: datetime,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    runs: Sequence[Mapping[str, Any]],
    config_note: str,
) -> str:
    hours = (finished_at - started_at).total_seconds() / 3600
    out = [
        f"## {finished_at:%Y-%m-%d} — E4 scheduler soak `{label}`",
        "",
        f"Window: {started_at:%Y-%m-%d %H:%MZ} to {finished_at:%Y-%m-%d %H:%MZ} ({hours:.1f} h). "
        f"{config_note}",
        "",
        "### Publisher status",
        "",
        f"Cloud size: before {before.get('database_size_mb')} MiB, "
        f"after {after.get('database_size_mb')} MiB "
        f"(guard action after: {after.get('size_action')}).",
        "",
        "**Before**",
        "",
        *_market_table(before),
        "",
        "**After**",
        "",
        *_market_table(after),
        "",
        "| serving table | before | after | delta |",
        "|---|---:|---:|---:|",
    ]
    b, a = before.get("table_counts", {}), after.get("table_counts", {})
    for table in sorted(set(b) | set(a)):
        out.append(f"| {table} | {b.get(table, 0)} | {a.get(table, 0)} | {a.get(table, 0) - b.get(table, 0):+d} |")
    out += ["", "### Per-run metrics (local `runs` ledger)", "",
            "| kind | market | runs | ok | failed | p50 s | max s | summed counters | errors |",
            "|---|---|---:|---:|---:|---:|---:|---|---|"]
    for row in summarize_runs(runs):
        counters = ", ".join(f"{k}={v}" for k, v in sorted(row["sums"].items())) or "-"
        errors = ", ".join(f"{k}x{v}" for k, v in sorted(row["errors"].items())) or "-"
        out.append(
            f"| {row['kind']} | {row['city']} | {row['runs']} | {row['succeeded']} | "
            f"{row['failed']} | {row['p50_s']} | {row['max_s']} | {counters} | {errors} |"
        )
    return "\n".join(out) + "\n"


def append_progress(markdown: str, path: Path = Path("docs/progress.md")) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else "# Progress\n"
    path.write_text(existing.rstrip("\n") + "\n\n" + markdown, encoding="utf-8")
