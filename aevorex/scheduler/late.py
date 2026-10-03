"""Late-market detection: did a scheduled check slot pass without a completed check?"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


def aware_utc(value: datetime | None) -> datetime | None:
    """Local columns hold naive UTC (``utc_now``); make them comparable."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class LateVerdict:
    late: bool
    due: datetime | None
    overdue_minutes: float


def evaluate_lateness(
    now: datetime,
    last_checked_at: datetime | None,
    due_slot: datetime | None,
    grace: timedelta,
    *,
    discovered_at: datetime | None = None,
) -> LateVerdict:
    """A market is late when the most recent slot is older than ``grace`` and unserved.

    ``due_slot`` is the latest scheduled check at or before ``now``. A slot is *served*
    if a complete check finished at or after it. Slots that predate the market's
    discovery are ignored, so a freshly added city is ``warming`` rather than late.
    """
    due = due_slot
    if due is not None and discovered_at is not None:
        due = max(due, discovered_at)
    if due is None:
        return LateVerdict(False, None, 0.0)
    checked = aware_utc(last_checked_at)
    if checked is not None and checked >= due:
        return LateVerdict(False, due, 0.0)
    overdue = now - due
    return LateVerdict(overdue >= grace, due, max(0.0, overdue.total_seconds() / 60))
