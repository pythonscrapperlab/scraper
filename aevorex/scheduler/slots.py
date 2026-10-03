"""Deterministic market-local slot arithmetic shared by the live service and the preview.

Every fire time comes from :func:`slots_for_day`; :class:`SlotTrigger` merely adapts it
to APScheduler. Because the live scheduler and ``scheduler preview`` read the same
function, the preview cannot drift from what the service will actually do.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.triggers.base import BaseTrigger

from aevorex.scheduler.config import MarketSchedule

CHECK = "check"
REFRESH = "refresh"


def stagger_offset_minutes(slug: str, zips: Iterable[str], spread: int) -> int:
    """Stable 0..spread-1 minute offset from a hash of the market slug and its zips.

    Hash-based (not random) so restarts and previews agree, and so cities spread
    across the cadence instead of all hitting Redfin on the hour.
    """
    if spread <= 1:
        return 0
    material = slug.strip().lower() + "|" + ",".join(sorted({z.strip() for z in zips if z}))
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % spread


def _wall(day: date, clock: time, tz: ZoneInfo, offset_minutes: int) -> datetime:
    """Local wall time -> UTC instant, shifting non-existent (spring-forward) times ahead.

    A wall time inside the DST gap is interpreted with the pre-transition offset, which
    lands one hour later on the clock (02:30 -> 03:30). Ambiguous (fall-back) times
    resolve to their first occurrence. Either way the slot exists exactly once.
    """
    naive = datetime.combine(day, clock) + timedelta(minutes=offset_minutes)
    local = naive.replace(tzinfo=tz)
    return local.astimezone(UTC)


def slots_for_day(
    kind: str, schedule: MarketSchedule, tz: ZoneInfo, day: date, offset_minutes: int
) -> list[datetime]:
    """All UTC fire instants of one kind for one market-local calendar day."""
    if kind == CHECK:
        clocks: list[time] = []
        cursor = datetime.combine(day, schedule.window_start)
        end = datetime.combine(day, schedule.window_end)
        step = timedelta(minutes=schedule.check_cadence_minutes)
        while cursor <= end:
            clocks.append(cursor.time())
            cursor += step
        if schedule.overnight_check is not None:
            clocks.append(schedule.overnight_check)
    elif kind == REFRESH:
        clocks = [schedule.nightly_refresh] if schedule.nightly_refresh is not None else []
    else:
        raise ValueError("Unknown slot kind")
    return sorted({_wall(day, clock, tz, offset_minutes) for clock in clocks})


def slots_between(
    kind: str,
    schedule: MarketSchedule,
    tz: ZoneInfo,
    offset_minutes: int,
    start: datetime,
    end: datetime,
) -> list[datetime]:
    """Fire instants in ``(start, end]`` (both aware)."""
    first = start.astimezone(tz).date() - timedelta(days=1)
    last = end.astimezone(tz).date() + timedelta(days=1)
    found: list[datetime] = []
    day = first
    while day <= last:
        found.extend(
            s for s in slots_for_day(kind, schedule, tz, day, offset_minutes) if start < s <= end
        )
        day += timedelta(days=1)
    return sorted(set(found))


def next_slot(
    kind: str,
    schedule: MarketSchedule,
    tz: ZoneInfo,
    offset_minutes: int,
    after: datetime,
) -> datetime | None:
    """First slot strictly after ``after``; ``None`` when the kind is disabled."""
    day = after.astimezone(tz).date() - timedelta(days=1)
    for _ in range(5):
        candidates = [
            s for s in slots_for_day(kind, schedule, tz, day, offset_minutes) if s > after
        ]
        if candidates:
            return min(candidates)
        day += timedelta(days=1)
    return None


def previous_slot(
    kind: str,
    schedule: MarketSchedule,
    tz: ZoneInfo,
    offset_minutes: int,
    at_or_before: datetime,
) -> datetime | None:
    """Most recent slot at or before ``at_or_before``."""
    day = at_or_before.astimezone(tz).date() + timedelta(days=1)
    for _ in range(5):
        candidates = [
            s for s in slots_for_day(kind, schedule, tz, day, offset_minutes) if s <= at_or_before
        ]
        if candidates:
            return max(candidates)
        day -= timedelta(days=1)
    return None


class SlotTrigger(BaseTrigger):  # type: ignore[misc]
    """APScheduler trigger backed by :func:`next_slot`."""

    def __init__(
        self, kind: str, schedule: MarketSchedule, timezone: str, offset_minutes: int
    ) -> None:
        self.kind = kind
        self.schedule = schedule
        self.tz = ZoneInfo(timezone)
        self.offset_minutes = offset_minutes

    def get_next_fire_time(
        self, previous_fire_time: datetime | None, now: datetime
    ) -> datetime | None:
        # next_slot is strictly-after: step back 1 us so a slot equal to `now` still fires.
        reference = now.astimezone(UTC) - timedelta(microseconds=1)
        if previous_fire_time is not None:
            reference = max(reference, previous_fire_time.astimezone(UTC))
        return next_slot(self.kind, self.schedule, self.tz, self.offset_minutes, reference)

    def __str__(self) -> str:
        return f"slots[{self.kind}, offset={self.offset_minutes}m]"


@dataclass(frozen=True)
class PlannedRun:
    """One previewed fire time."""

    at_utc: datetime
    at_local: datetime
    slug: str
    kind: str


def plan(
    markets: Sequence[tuple[str, str, MarketSchedule, int]],
    start: datetime,
    hours: float,
) -> list[PlannedRun]:
    """Preview every fire time in ``(start, start+hours]`` across ``(slug, tz, schedule, offset)``."""
    end = start + timedelta(hours=hours)
    runs: list[PlannedRun] = []
    for slug, timezone, schedule, offset in markets:
        tz = ZoneInfo(timezone)
        for kind in (CHECK, REFRESH):
            for slot in slots_between(kind, schedule, tz, offset, start, end):
                runs.append(PlannedRun(slot, slot.astimezone(tz), slug, kind))
    return sorted(runs, key=lambda item: (item.at_utc, item.slug, item.kind))


def as_dict(item: PlannedRun) -> dict[str, Any]:
    return {
        "utc": item.at_utc.isoformat(),
        "local": item.at_local.isoformat(),
        "market": item.slug,
        "kind": item.kind,
    }
