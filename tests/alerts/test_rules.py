"""Crossing logic, quiet hours, brief windows and plan sizes (all pure)."""

from __future__ import annotations

from datetime import UTC, datetime, time
from uuid import uuid4

import pytest

from aevorex.alerts.alerts import Candidate, OrgRules, Recipient, plan_alerts
from aevorex.alerts.rules import (
    QuietHours,
    ScoreState,
    Threshold,
    brief_count,
    brief_window,
    crossed,
    qualifies,
    send_after,
)

TOP = Threshold(tier="top")
STRONG = Threshold(tier="strong")
P90 = Threshold(min_percentile=90)


def state(percentile: float) -> ScoreState:
    tier = "top" if percentile >= 95 else "strong" if percentile >= 80 else "rest"
    return ScoreState(percentile, tier)


def test_threshold_requires_a_rule() -> None:
    with pytest.raises(ValueError):
        Threshold()
    with pytest.raises(ValueError):
        Threshold(tier="rest")


@pytest.mark.parametrize(
    ("old", "new", "threshold", "fires"),
    [
        (79.9, 80.0, STRONG, True),       # exact tier boundary enters
        (80.0, 94.9, STRONG, False),      # already inside: no repeat
        (94.9, 95.0, TOP, True),
        (95.0, 99.0, TOP, False),
        (96.0, 70.0, TOP, False),         # leaving the tier never alerts
        (70.0, 96.0, STRONG, True),       # jumping over a tier still enters it
        (None, 97.0, TOP, True),          # new listing that qualifies
        (None, 60.0, STRONG, False),      # new listing that does not qualify
        (89.9, 90.0, P90, True),          # percentile floor is inclusive
        (90.0, 99.0, P90, False),
        (92.0, 85.0, P90, False),
    ],
)
def test_crossing_matrix(old: float | None, new: float, threshold: Threshold, fires: bool) -> None:
    before = None if old is None else state(old)
    assert crossed(before, state(new), threshold) is fires


def test_percentile_floor_wins_over_tier_when_both_set() -> None:
    both = Threshold(min_percentile=70, tier="top")
    assert qualifies(state(75), both)  # tier alone would say no


def test_missing_score_never_qualifies() -> None:
    assert not qualifies(None, TOP)
    assert not crossed(None, None, TOP)


@pytest.mark.parametrize(
    ("hour", "minute", "inside"),
    [(20, 59, False), (21, 0, True), (23, 59, True), (0, 0, True), (6, 59, True), (7, 0, False)],
)
def test_quiet_window_crossing_midnight(hour: int, minute: int, inside: bool) -> None:
    assert QuietHours().contains(time(hour, minute)) is inside


def test_quiet_window_same_day_and_disabled() -> None:
    lunch = QuietHours(True, time(12, 0), time(14, 0))
    assert lunch.contains(time(13, 0)) and not lunch.contains(time(14, 0))
    assert not QuietHours(False).contains(time(23, 0))
    assert not QuietHours(True, time(8, 0), time(8, 0)).contains(time(8, 0))


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def test_send_after_outside_quiet_hours_is_immediate() -> None:
    now = utc(2026, 10, 3, 18, 0)  # 14:00 New York (EDT)
    assert send_after(now, "America/New_York", QuietHours()) == now


def test_send_after_waits_until_morning_before_midnight() -> None:
    now = utc(2026, 10, 4, 2, 30)  # 22:30 on Oct 3 in New York
    assert send_after(now, "America/New_York", QuietHours()) == utc(2026, 10, 4, 11, 0)


def test_send_after_waits_until_morning_after_midnight() -> None:
    now = utc(2026, 10, 3, 7, 0)  # 03:00 on Oct 3 in New York
    assert send_after(now, "America/New_York", QuietHours()) == utc(2026, 10, 3, 11, 0)


def test_send_after_uses_org_timezone_not_utc() -> None:
    now = utc(2026, 10, 3, 5, 0)  # 22:00 on Oct 2 in Los Angeles (PDT)
    assert send_after(now, "America/Los_Angeles", QuietHours()) == utc(2026, 10, 3, 14, 0)


def test_send_after_across_dst_fall_back() -> None:
    # 2026-11-01 01:30 EDT is inside quiet hours on the day clocks fall back.
    now = utc(2026, 11, 1, 5, 30)
    assert send_after(now, "America/New_York", QuietHours()) == utc(2026, 11, 1, 12, 0)  # 07:00 EST


def test_brief_window_boundaries_and_dst() -> None:
    assert brief_window(utc(2026, 10, 3, 10, 59), "America/New_York").reason == "too_early"
    assert brief_window(utc(2026, 10, 3, 11, 0), "America/New_York").due  # 07:00 EDT
    assert brief_window(utc(2026, 10, 3, 14, 59), "America/New_York").due
    assert brief_window(utc(2026, 10, 3, 15, 0), "America/New_York").reason == "too_late"
    # After fall-back 07:00 local is 12:00Z.
    assert not brief_window(utc(2026, 11, 1, 11, 59), "America/New_York").due
    assert brief_window(utc(2026, 11, 1, 12, 0), "America/New_York").due
    assert brief_window(utc(2026, 10, 3, 14, 0), "America/Los_Angeles").due  # 07:00 PDT


def test_brief_count_by_plan_with_override_and_unknown_plan() -> None:
    assert [brief_count(p) for p in ("starter", "pro", "growth", "brokerage")] == [5, 10, 15, 25]
    assert brief_count("pro", {"pro": 12}) == 12
    assert brief_count("mystery") == 5


ORG = uuid4()
USER_A, USER_B, USER_C = uuid4(), uuid4(), uuid4()
NOW = utc(2026, 10, 3, 18, 0)  # 14:00 New York


def candidate(pid: str, lens: str, old: float | None, new: float) -> Candidate:
    return Candidate(
        property_id=pid, lens=lens, new=state(new), old=None if old is None else state(old),
        score=60.0, grade="C", computed_at=NOW, address="1 Test St", price=300_000,
    )


def org(*recipients: Recipient, thresholds: dict[str, Threshold] | None = None) -> OrgRules:
    return OrgRules(
        org_id=ORG, plan="pro", tz="America/New_York", default_lens="motivated_seller",
        thresholds=thresholds or {"motivated_seller": TOP}, recipients=recipients,
    )


def test_plan_alerts_fires_once_per_crossing_and_respects_lens() -> None:
    rules = org(Recipient(USER_A))
    plan = plan_alerts(
        rules, "orlando-fl",
        [
            candidate("p1", "motivated_seller", 90, 96),   # crosses
            candidate("p2", "motivated_seller", 96, 97),   # already top
            candidate("p3", "fix_flip", 10, 99),           # lens has no threshold
            candidate("p4", "motivated_seller", None, 98), # new qualifier
        ],
        NOW,
    )
    assert [e["property_id"] for e in plan.events] == ["p4", "p1"]  # highest percentile first
    assert plan.events[0]["payload"]["reason"] == "new_qualifier"
    assert plan.events[1]["payload"]["reason"] == "crossed"
    assert len(plan.emails) == 1 and plan.emails[0]["payload"]["total"] == 2
    assert plan.emails[0]["send_after"] == NOW


def test_plan_alerts_is_deterministic_for_idempotent_keys() -> None:
    cands = [candidate("p1", "motivated_seller", 90, 96)]
    first = plan_alerts(org(Recipient(USER_A)), "orlando-fl", cands, NOW)
    second = plan_alerts(org(Recipient(USER_A)), "orlando-fl", cands, NOW)
    assert first.events[0]["dedupe_key"] == second.events[0]["dedupe_key"]
    assert first.emails[0]["dedupe_key"] == second.emails[0]["dedupe_key"]


def test_plan_alerts_honours_channels_and_quiet_hours() -> None:
    night = utc(2026, 10, 4, 3, 0)  # 23:00 New York
    rules = org(
        Recipient(USER_A),                                   # default quiet hours: deferred
        Recipient(USER_B, quiet=QuietHours(enabled=False)),  # opted out of quiet hours
        Recipient(USER_C, email=False),                      # in-app only
    )
    plan = plan_alerts(rules, "orlando-fl", [candidate("p1", "motivated_seller", 50, 99)], night)
    by_user = {e["user_id"]: e for e in plan.emails}
    assert set(by_user) == {USER_A, USER_B}
    assert by_user[USER_A]["send_after"] == utc(2026, 10, 4, 11, 0)
    assert by_user[USER_B]["send_after"] == night
    assert plan.deferred == 1
    assert len(plan.events) == 1  # in-app event exists because someone has in_app on


def test_plan_alerts_no_in_app_recipients_means_no_event_rows() -> None:
    rules = org(Recipient(USER_A, in_app=False), Recipient(USER_B, tier_alerts=False))
    plan = plan_alerts(rules, "orlando-fl", [candidate("p1", "motivated_seller", 50, 99)], NOW)
    assert plan.events == []
    assert [e["user_id"] for e in plan.emails] == [USER_A]
