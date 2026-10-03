"""Scheduler brief wiring, config parsing and the publisher's alert candidate selection."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from aevorex.alerts.alerts import Candidate
from aevorex.alerts.brief import BriefSweep
from aevorex.alerts.rules import ScoreState
from aevorex.publisher.service import Publisher
from aevorex.scheduler.config import ScheduleConfig, load_config, parse_config
from aevorex.scheduler.service import SchedulerService
from tests.scheduler.fakes import Clock, FakeOps


def test_brief_config_defaults_and_overrides() -> None:
    assert ScheduleConfig().service.brief_counts["pro"] == 10
    parsed = parse_config({"service": {"brief_counts": {"pro": 12}, "brief_sweep_minutes": 5}})
    assert parsed.service.brief_counts == {"starter": 5, "pro": 12, "growth": 15, "brokerage": 25}
    assert parsed.service.brief_sweep_minutes == 5
    assert load_config().service.brief_counts["brokerage"] == 25  # shipped scheduler.yaml


@pytest.mark.asyncio
async def test_brief_sweep_job_calls_sweeper_and_survives_failure() -> None:
    seen: list[Mapping[str, int]] = []

    async def sweeper(now: datetime, counts: Mapping[str, int]) -> BriefSweep:
        seen.append(counts)
        return BriefSweep(enqueued=2)

    service = SchedulerService(
        ScheduleConfig(), FakeOps(), None, clock=Clock(), brief_sweeper=sweeper  # type: ignore[arg-type]
    )
    await service._brief_sweep()
    assert seen and seen[0]["starter"] == 5

    async def broken(now: datetime, counts: Mapping[str, int]) -> BriefSweep:
        raise RuntimeError("remote down")

    service.brief_sweeper = broken
    await service._brief_sweep()  # never raises into the scheduler

    service.paused = True
    service.brief_sweeper = sweeper
    seen.clear()
    await service._brief_sweep()
    assert seen == []


def test_publisher_alert_candidates_skip_delisted_and_inactive() -> None:
    now = datetime(2026, 10, 3, 18, tzinfo=UTC)
    live = SimpleNamespace(id="a", delisted_at=None, listing_status_normalized="active",
                           address="1 A St", price=100)
    delisted = SimpleNamespace(id="b", delisted_at=now, listing_status_normalized="active",
                               address="2 B St", price=100)
    sold = SimpleNamespace(id="c", delisted_at=None, listing_status_normalized="sold",
                           address="3 C St", price=100)
    rows = [
        {"property_id": item.id, "lens": "fix_flip", "percentile": 97.0, "tier": "top",
         "score": 70, "grade": "B", "computed_at": now}
        for item in (live, delisted, sold)
    ]
    publisher = object.__new__(Publisher)
    previous = {("a", "fix_flip"): ScoreState(60.0, "rest")}
    candidates: list[Candidate] = publisher._alert_candidates(  # type: ignore[arg-type]
        [live, delisted, sold], rows, previous
    )
    assert [c.property_id for c in candidates] == ["a"]
    assert candidates[0].old == ScoreState(60.0, "rest")
    assert candidates[0].new == ScoreState(97.0, "top")
