"""Configuration validation, late detector and backoff arithmetic."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest

from aevorex.scheduler.backoff import SourceBackoff
from aevorex.scheduler.config import (
    ScheduleConfigError,
    load_config,
    parse_config,
)
from aevorex.scheduler.late import evaluate_lateness

NOW = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)
GRACE = timedelta(minutes=30)


def test_shipped_yaml_loads_with_agents_defaults() -> None:
    config = load_config()
    assert config.defaults.check_cadence_minutes == 120
    assert (config.defaults.window_start, config.defaults.window_end) == (time(7), time(21))
    assert config.defaults.overnight_check == time(3)
    assert config.defaults.nightly_refresh == time(2)
    assert config.service.heartbeat_minutes == 10
    assert config.service.late_detector_minutes == 5
    assert config.service.backoff_cap_minutes == 120


def test_missing_file_falls_back_to_builtin_defaults(tmp_path: object) -> None:
    assert load_config(str(tmp_path) + "/nope.yaml").defaults.check_cadence_minutes == 120  # type: ignore[operator]


def test_per_market_override_inherits_defaults() -> None:
    config = parse_config({"markets": {"San-Jose-CA": {"check_cadence_minutes": 180}}})
    assert config.for_market("san-jose-ca").check_cadence_minutes == 180
    assert config.for_market("san-jose-ca").window_start == time(7)
    assert config.for_market("orlando-fl").check_cadence_minutes == 120


@pytest.mark.parametrize(
    "raw",
    [
        {"defaults": {"check_cadence_minutes": 5}},
        {"defaults": {"window_start": "22:00", "window_end": "21:00"}},
        {"defaults": {"stagger_minutes": 120}},
        {"defaults": {"mystery": 1}},
        {"service": {"mystery": 1}},
        {"top_level_typo": {}},
        {"defaults": {"window_start": 420}},  # unquoted 7:00 parses as a YAML int
        {"defaults": {"window_start": "seven"}},
        {"service": {"nightly_finalize_tz": "Mars/Base"}},
    ],
)
def test_invalid_configuration_fails_closed(raw: dict[str, object]) -> None:
    with pytest.raises(Exception) as caught:
        parse_config(raw)
    assert isinstance(caught.value, ScheduleConfigError | KeyError | ValueError)


def test_null_disables_overnight_and_nightly() -> None:
    config = parse_config({"defaults": {"overnight_check": None, "nightly_refresh": None}})
    assert config.defaults.overnight_check is None
    assert config.defaults.nightly_refresh is None


# --- late detector -----------------------------------------------------------------


def test_served_slot_is_not_late() -> None:
    due = NOW - timedelta(hours=1)
    verdict = evaluate_lateness(NOW, due + timedelta(minutes=1), due, GRACE)
    assert not verdict.late


def test_unserved_slot_inside_grace_is_not_late_yet() -> None:
    verdict = evaluate_lateness(NOW, NOW - timedelta(hours=3), NOW - timedelta(minutes=29), GRACE)
    assert not verdict.late


def test_unserved_slot_past_grace_is_late() -> None:
    verdict = evaluate_lateness(NOW, NOW - timedelta(hours=3), NOW - timedelta(minutes=30), GRACE)
    assert verdict.late
    assert verdict.overdue_minutes == pytest.approx(30)


def test_naive_local_timestamps_are_read_as_utc() -> None:
    due = NOW - timedelta(minutes=45)
    served = (due + timedelta(minutes=1)).replace(tzinfo=None)
    assert not evaluate_lateness(NOW, served, due, GRACE).late


def test_new_market_is_warming_not_late_until_grace_after_discovery() -> None:
    old_slot = NOW - timedelta(hours=5)
    discovered = NOW - timedelta(minutes=10)
    assert not evaluate_lateness(NOW, None, old_slot, GRACE, discovered_at=discovered).late
    stuck = evaluate_lateness(
        NOW, None, old_slot, GRACE, discovered_at=NOW - timedelta(minutes=40)
    )
    assert stuck.late


def test_no_slot_means_not_late() -> None:
    assert not evaluate_lateness(NOW, None, None, GRACE).late


# --- backoff -------------------------------------------------------------------------


def test_backoff_sequence_is_5_10_20_40_80_then_two_hour_cap() -> None:
    backoff = SourceBackoff()
    minutes = [int(backoff.record_failure(NOW).total_seconds() // 60) for _ in range(8)]
    assert minutes == [5, 10, 20, 40, 80, 120, 120, 120]


def test_backoff_matches_the_e2_detail_queue_schedule() -> None:
    from aevorex.freshness.refresh import backoff_delay_minutes

    backoff = SourceBackoff()
    for failures in range(1, 9):
        assert backoff.delay(failures) == timedelta(minutes=backoff_delay_minutes(failures))


def test_backoff_window_and_reset() -> None:
    backoff = SourceBackoff()
    delay = backoff.record_failure(NOW)
    assert backoff.active(NOW + delay - timedelta(seconds=1))
    assert not backoff.active(NOW + delay)
    assert backoff.remaining(NOW) == delay
    backoff.record_failure(NOW)
    backoff.record_success()
    assert backoff.failures == 0 and not backoff.active(NOW)
    assert backoff.record_failure(NOW) == timedelta(minutes=5)
