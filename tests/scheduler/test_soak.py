"""Soak metrics: window handling, honest gaps and idempotent progress rows."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

from aevorex.scheduler.soak import SECTION, render_row, summarize_day, upsert_row

END = datetime(2026, 10, 4, 3, 55, tzinfo=UTC)
START = END - timedelta(hours=24)


def run(kind: str, status: str, minutes_ago: int, duration: int | None = 60, **counts: int) -> dict:
    return {"kind": kind, "status": status, "started_at": END - timedelta(minutes=minutes_ago),
            "duration_s": duration, "counts": counts}


def log(message: str, minutes_ago: int) -> str:
    return json.dumps({"ts": (END - timedelta(minutes=minutes_ago)).isoformat(), "msg": message})


def test_summarize_day_counts_only_the_window() -> None:
    runs = [
        run("check", "succeeded", 10), run("check", "succeeded", 130), run("check", "failed", 250),
        run("check", "succeeded", 60 * 30),  # outside the 24 h window
        run("refresh", "succeeded", 300, duration=1800, attempts=100, blocked=5),
        run("refresh", "cancelled", 400, duration=600, attempts=100, blocked=15),
        run("analyze", "succeeded", 20),
    ]
    lines = [
        log("Market is late: market=miami-fl overdue_minutes=45", 200),
        log("Market is late: market=miami-fl overdue_minutes=45", 60 * 40),  # outside
        log("Alerts enqueued: market=orlando-fl events=3 emails=2 deferred_quiet=1", 100),
        log('Brief sweep: {"brief_enqueued": 4, "brief_failed": 0}', 90),
        "not json at all",
    ]
    metrics = summarize_day(runs, lines, day=date(2026, 10, 3), start=START, end=END,
                            local_db_bytes=900 * 1048576, cloud_db_mib=88.1)
    assert (metrics.checks_ok, metrics.checks_failed) == (2, 1)
    assert (metrics.refreshes_ok, metrics.refreshes_failed) == (1, 1)
    assert metrics.refresh_p50_min == 20.0 and metrics.refresh_max_min == 30.0
    assert metrics.block_rate == 0.1
    assert (metrics.late_events, metrics.alert_emails, metrics.briefs_enqueued) == (1, 2, 4)
    assert metrics.local_db_mib == 900.0


def test_no_fetches_means_block_rate_is_not_available_not_zero() -> None:
    metrics = summarize_day([], [], day=date(2026, 10, 3), start=START, end=END,
                            local_db_bytes=None, cloud_db_mib=None)
    row = render_row(metrics)
    assert metrics.block_rate is None and "n/a" in row
    assert "| 0/0 |" in row


def test_upsert_row_creates_section_appends_and_replaces_same_day() -> None:
    started = datetime(2026, 10, 3, 20, 0, tzinfo=UTC)
    doc = "# Progress\n\n## earlier\n\ntext\n"
    first = upsert_row(doc, "| 2026-10-03 | a |", date(2026, 10, 3), label="e5-7d", started=started)
    assert SECTION in first and first.count("| 2026-10-03 |") == 1 and "## earlier" in first
    second = upsert_row(first, "| 2026-10-04 | b |", date(2026, 10, 4), label="e5-7d", started=started)
    assert second.index("| 2026-10-03 |") < second.index("| 2026-10-04 |")
    third = upsert_row(second, "| 2026-10-03 | a2 |", date(2026, 10, 3), label="e5-7d", started=started)
    assert third.count("| 2026-10-03 |") == 1 and "a2" in third and "| 2026-10-04 | b |" in third
    assert third.count(SECTION) == 1
