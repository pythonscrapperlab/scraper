"""Window, stagger and DST behaviour of the shared slot arithmetic."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from aevorex.scheduler.config import MarketSchedule
from aevorex.scheduler.slots import (
    CHECK,
    REFRESH,
    SlotTrigger,
    next_slot,
    plan,
    previous_slot,
    slots_between,
    slots_for_day,
    stagger_offset_minutes,
)

NY = ZoneInfo("America/New_York")
PT = ZoneInfo("America/Los_Angeles")
DEFAULT = MarketSchedule()


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def local_times(slots: list[datetime], tz: ZoneInfo) -> list[str]:
    return [slot.astimezone(tz).strftime("%H:%M") for slot in slots]


def test_check_window_is_seven_to_twenty_one_every_two_hours_plus_overnight() -> None:
    slots = slots_for_day(CHECK, DEFAULT, NY, date(2026, 10, 3), 0)
    assert local_times(slots, NY) == [
        "03:00", "07:00", "09:00", "11:00", "13:00", "15:00", "17:00", "19:00", "21:00",
    ]


def test_nightly_refresh_is_two_am_local() -> None:
    slots = slots_for_day(REFRESH, DEFAULT, PT, date(2026, 10, 3), 0)
    assert local_times(slots, PT) == ["02:00"]
    assert slots[0] == utc(2026, 10, 3, 9, 0)  # PDT is UTC-7


def test_disabled_overnight_and_nightly_produce_no_slots() -> None:
    schedule = replace(DEFAULT, overnight_check=None, nightly_refresh=None)
    assert "03:00" not in local_times(slots_for_day(CHECK, schedule, NY, date(2026, 10, 3), 0), NY)
    assert slots_for_day(REFRESH, schedule, NY, date(2026, 10, 3), 0) == []


def test_stagger_is_deterministic_bounded_and_zip_sensitive() -> None:
    zips = ("32801", "32803", "32804")
    first = stagger_offset_minutes("orlando-fl", zips, 30)
    assert first == stagger_offset_minutes("orlando-fl", tuple(reversed(zips)), 30)
    assert 0 <= first < 30
    assert first == 4  # pinned: restarts and previews must agree forever
    offsets = {
        stagger_offset_minutes(slug, ("33101",), 30)
        for slug in ("orlando-fl", "miami-fl", "tampa-fl", "vero-beach-fl", "san-jose-ca")
    }
    assert len(offsets) >= 3  # cities actually spread out
    assert stagger_offset_minutes("anything", (), 0) == 0
    assert stagger_offset_minutes("anything", (), 1) == 0


def test_offset_shifts_every_slot() -> None:
    slots = slots_for_day(CHECK, DEFAULT, NY, date(2026, 10, 3), 17)
    assert local_times(slots, NY)[1] == "07:17"


def test_non_default_cadence_is_honoured() -> None:
    schedule = replace(DEFAULT, check_cadence_minutes=180, overnight_check=None)
    slots = slots_for_day(CHECK, schedule, NY, date(2026, 10, 3), 0)
    assert local_times(slots, NY) == ["07:00", "10:00", "13:00", "16:00", "19:00"]


def test_window_wall_clock_is_stable_across_spring_forward() -> None:
    before = slots_for_day(CHECK, DEFAULT, NY, date(2026, 3, 7), 0)
    gap_day = slots_for_day(CHECK, DEFAULT, NY, date(2026, 3, 8), 0)
    after = slots_for_day(CHECK, DEFAULT, NY, date(2026, 3, 9), 0)
    for day in (before, gap_day, after):
        assert len(day) == 9
        assert len(set(day)) == 9
        assert local_times(day, NY)[1] == "07:00"
    assert before[1] == utc(2026, 3, 7, 12, 0)  # EST
    assert gap_day[1] == utc(2026, 3, 8, 11, 0)  # EDT
    assert after[1] == utc(2026, 3, 9, 11, 0)


def test_nonexistent_spring_forward_time_fires_once_an_hour_later() -> None:
    schedule = replace(DEFAULT, nightly_refresh=time(2, 30))
    slots = slots_for_day(REFRESH, schedule, NY, date(2026, 3, 8), 0)
    assert slots == [utc(2026, 3, 8, 7, 30)]  # 03:30 EDT: shifted forward, not skipped
    assert local_times(slots, NY) == ["03:30"]


def test_default_nightly_two_am_on_gap_day_still_runs_exactly_once() -> None:
    slots = slots_for_day(REFRESH, DEFAULT, NY, date(2026, 3, 8), 0)
    assert len(slots) == 1
    assert slots[0] == utc(2026, 3, 8, 7, 0)  # 03:00 EDT


def test_ambiguous_fall_back_time_fires_once_at_first_occurrence() -> None:
    schedule = replace(DEFAULT, nightly_refresh=time(1, 30))
    slots = slots_for_day(REFRESH, schedule, NY, date(2026, 11, 1), 0)
    assert slots == [utc(2026, 11, 1, 5, 30)]  # 01:30 EDT, first occurrence only


def test_fall_back_day_keeps_nine_distinct_check_slots() -> None:
    slots = slots_for_day(CHECK, DEFAULT, NY, date(2026, 11, 1), 0)
    assert len(set(slots)) == 9
    assert slots[1] == utc(2026, 11, 1, 12, 0)  # 07:00 EST


def test_pacific_and_eastern_slots_interleave_correctly() -> None:
    east = slots_for_day(CHECK, DEFAULT, NY, date(2026, 10, 3), 0)[1]
    west = slots_for_day(CHECK, DEFAULT, PT, date(2026, 10, 3), 0)[1]
    assert west - east == timedelta(hours=3)


def test_next_and_previous_slot_are_strict_and_inclusive_respectively() -> None:
    at = utc(2026, 10, 3, 11, 0)  # exactly 07:00 EDT
    assert next_slot(CHECK, DEFAULT, NY, 0, at) == utc(2026, 10, 3, 13, 0)
    assert previous_slot(CHECK, DEFAULT, NY, 0, at) == at
    assert previous_slot(CHECK, DEFAULT, NY, 0, at - timedelta(seconds=1)) == utc(2026, 10, 3, 7, 0)


def test_next_slot_none_when_kind_disabled() -> None:
    schedule = replace(DEFAULT, nightly_refresh=None)
    assert next_slot(REFRESH, schedule, NY, 0, utc(2026, 10, 3)) is None


def test_trigger_fires_at_now_when_now_is_a_slot_and_advances_after_previous() -> None:
    trigger = SlotTrigger(CHECK, DEFAULT, "America/New_York", 0)
    slot = utc(2026, 10, 3, 11, 0)
    assert trigger.get_next_fire_time(None, slot) == slot
    assert trigger.get_next_fire_time(slot, slot) == utc(2026, 10, 3, 13, 0)


def test_trigger_crosses_spring_forward_overnight() -> None:
    trigger = SlotTrigger(CHECK, DEFAULT, "America/New_York", 0)
    now = utc(2026, 3, 8, 6, 30)  # 01:30 EST, before the gap
    assert trigger.get_next_fire_time(None, now) == utc(2026, 3, 8, 7, 0)  # 03:00 EDT


def test_trigger_sequence_equals_preview_plan() -> None:
    start, hours = utc(2026, 10, 3, 12, 0), 60
    end = start + timedelta(hours=hours)
    trigger = SlotTrigger(CHECK, DEFAULT, "America/New_York", 9)
    fired: list[datetime] = []
    previous: datetime | None = None
    cursor = start
    while True:
        nxt = trigger.get_next_fire_time(previous, cursor)
        if nxt is None or nxt > end:
            break
        fired.append(nxt)
        previous, cursor = nxt, nxt
    planned = [
        run.at_utc
        for run in plan([("orlando-fl", "America/New_York", DEFAULT, 9)], start, hours)
        if run.kind == CHECK
    ]
    assert fired == planned
    assert fired == slots_between(CHECK, DEFAULT, NY, 9, start, end)


@pytest.mark.parametrize("kind", ["bogus", ""])
def test_unknown_kind_is_rejected(kind: str) -> None:
    with pytest.raises(ValueError):
        slots_for_day(kind, DEFAULT, NY, date(2026, 10, 3), 0)
