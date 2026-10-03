"""Discovery, warming, late detection and heartbeat wiring of the service object."""

from __future__ import annotations

from datetime import timedelta

import pytest

from aevorex.scheduler.cities import discover
from aevorex.scheduler.config import ScheduleConfig
from aevorex.scheduler.service import SchedulerService
from tests.scheduler.fakes import Clock, FakeOps

DEMO = {"orlando-fl", "miami-fl", "tampa-fl", "vero-beach-fl", "san-jose-ca"}


def reader(slugs: set[str]):  # type: ignore[no-untyped-def]
    async def read() -> set[str]:
        return set(slugs)

    return read


def make_service(
    org: set[str] | None = None,
) -> tuple[SchedulerService, FakeOps, Clock]:
    ops = FakeOps()
    clock = Clock()
    service = SchedulerService(
        ScheduleConfig(), ops, reader(org) if org is not None else None, clock=clock  # type: ignore[arg-type]
    )
    return service, ops, clock


def job_ids(service: SchedulerService) -> set[str]:
    return {job.id for job in service.scheduler.get_jobs()}


@pytest.mark.asyncio
async def test_discovery_is_demo_union_org_markets_with_unsupported_flagged() -> None:
    discovery, org = await discover(
        ScheduleConfig(), reader({"orlando-fl", "naples-fl", "Tampa-FL"})
    )
    assert set(discovery.active) == DEMO
    assert org == {"orlando-fl", "naples-fl", "tampa-fl"}
    assert discovery.unsupported == {"naples-fl": ("org",)}
    assert discovery.active["orlando-fl"].sources == ("demo", "org")
    assert discovery.active["miami-fl"].sources == ("demo",)
    assert discovery.active["san-jose-ca"].timezone == "America/Los_Angeles"
    assert all(0 <= c.stagger_minutes < 30 for c in discovery.active.values())


@pytest.mark.asyncio
async def test_discovery_without_a_reader_is_demo_only() -> None:
    discovery, org = await discover(ScheduleConfig(), None)
    assert set(discovery.active) == DEMO and org == set() and discovery.remote_ok


@pytest.mark.asyncio
async def test_remote_failure_keeps_previous_org_cities_and_reports_it() -> None:
    async def broken() -> set[str]:
        raise ConnectionError("supabase down")

    discovery, org = await discover(ScheduleConfig(), broken, previous_org_slugs={"orlando-fl"})
    assert not discovery.remote_ok
    assert org == {"orlando-fl"}
    assert "orlando-fl" in discovery.active


@pytest.mark.asyncio
async def test_new_never_checked_city_is_warmed_within_one_discovery_pass() -> None:
    service, _, _ = make_service()
    await service._discover()
    ids = job_ids(service)
    for slug in DEMO:
        assert {f"check:{slug}", f"nightly:{slug}", f"warm:{slug}"} <= ids
    warm = service.scheduler.get_job("warm:orlando-fl")
    assert warm is not None and warm.args == ("orlando-fl", "warming")


@pytest.mark.asyncio
async def test_recently_checked_city_is_not_warmed_but_stale_one_is_caught_up() -> None:
    service, ops, clock = make_service()
    ops.freshness_state["orlando-fl"] = (clock.now - timedelta(minutes=30), "ok")
    ops.freshness_state["miami-fl"] = (clock.now - timedelta(hours=5), "ok")
    await service._discover()
    ids = job_ids(service)
    assert "warm:orlando-fl" not in ids
    miami = service.scheduler.get_job("warm:miami-fl")
    assert miami is not None and miami.args == ("miami-fl", "catchup")


@pytest.mark.asyncio
async def test_city_dropping_out_of_org_markets_is_unscheduled_but_demo_cities_stay() -> None:
    service, _, _ = make_service(org={"orlando-fl"})
    await service._discover()
    service.org_reader = reader(set())
    await service._discover()
    assert "check:orlando-fl" in job_ids(service)  # still a demo market

    removed_service, _, _ = make_service()
    await removed_service._discover()
    del removed_service.cities["miami-fl"]  # simulate a city that was active, then vanished
    removed_service.cities["gone-fl"] = removed_service.cities["orlando-fl"]
    await removed_service._discover()
    assert "gone-fl" not in removed_service.cities


@pytest.mark.asyncio
async def test_late_market_is_flagged_locally_and_remotely_then_caught_up() -> None:
    service, ops, clock = make_service()
    await service._discover()
    service.discovered_at.clear()
    ops.freshness_state["orlando-fl"] = (clock.now - timedelta(hours=5), "ok")
    ops.calls.clear()
    await service._late_detector()
    assert ("set_local_status", ("orlando-fl", "late")) in ops.calls
    assert ("mark_status", ("orlando-fl", "late")) in ops.calls
    job = service.scheduler.get_job("catchup:orlando-fl")
    assert job is not None and job.args == ("orlando-fl", "catchup")


@pytest.mark.asyncio
async def test_served_market_is_not_flagged() -> None:
    service, ops, clock = make_service()
    await service._discover()
    service.discovered_at.clear()
    for slug in DEMO:
        ops.freshness_state[slug] = (clock.now - timedelta(minutes=1), "ok")
    ops.calls.clear()
    await service._late_detector()
    assert not any(name in {"set_local_status", "mark_status"} for name, _ in ops.calls)


@pytest.mark.asyncio
async def test_failed_status_is_not_downgraded_to_late() -> None:
    service, ops, clock = make_service()
    await service._discover()
    service.discovered_at.clear()
    ops.freshness_state["orlando-fl"] = (clock.now - timedelta(hours=5), "failed")
    ops.calls.clear()
    await service._late_detector()
    assert ("set_local_status", ("orlando-fl", "late")) not in ops.calls


@pytest.mark.asyncio
async def test_no_catchup_while_backing_off_inflight_or_paused() -> None:
    service, ops, clock = make_service()
    await service._discover()
    service.discovered_at.clear()
    ops.freshness_state["orlando-fl"] = (clock.now - timedelta(hours=5), "failed")
    service.runner.backoff.record_failure(clock.now)
    await service._late_detector()
    assert service.scheduler.get_job("catchup:orlando-fl") is None

    service.runner.backoff.record_success()
    service.runner.inflight.add(("check", "orlando-fl"))
    await service._late_detector()
    assert service.scheduler.get_job("catchup:orlando-fl") is None

    service.runner.inflight.clear()
    service.paused = True
    await service._late_detector()
    assert service.scheduler.get_job("catchup:orlando-fl") is None


@pytest.mark.asyncio
async def test_newly_discovered_city_is_warming_not_late() -> None:
    service, ops, _ = make_service()
    await service._discover()  # discovered_at == now, never checked
    ops.calls.clear()
    await service._late_detector()
    assert not any(name == "mark_status" for name, _ in ops.calls)


@pytest.mark.asyncio
async def test_retry_hook_replaces_a_single_one_shot_job_per_market_and_kind() -> None:
    service, _, clock = make_service()
    service.scheduler.start(paused=True)  # real job store, nothing fires
    try:
        service._schedule_retry("orlando-fl", "check", timedelta(minutes=5))
        service._schedule_retry("orlando-fl", "check", timedelta(minutes=10))
        jobs = [j for j in service.scheduler.get_jobs() if j.id == "retry:check:orlando-fl"]
        assert len(jobs) == 1
        assert jobs[0].trigger.run_date == clock.now + timedelta(minutes=10)  # type: ignore[attr-defined]
        service._schedule_retry("orlando-fl", "refresh", timedelta(minutes=5))
        assert service.scheduler.get_job("retry:refresh:orlando-fl") is not None
    finally:
        service.scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_heartbeat_writes_remote_row_and_retries_publish_debt() -> None:
    service, ops, _ = make_service()
    await service._discover()
    service.runner.publish_debt.add("orlando-fl")
    ops.calls.clear()
    await service._heartbeat()
    assert ("heartbeat", ("scheduler",)) in ops.calls
    assert "publish_full" in ops.names()
    assert service.runner.publish_debt == set()


@pytest.mark.asyncio
async def test_heartbeat_survives_remote_outage() -> None:
    service, ops, _ = make_service()

    async def down(note: str) -> None:
        raise ConnectionError("down")

    ops.heartbeat = down  # type: ignore[method-assign]
    await service._heartbeat()  # must not raise


@pytest.mark.asyncio
async def test_scheduled_jobs_use_the_shared_slot_trigger() -> None:
    from aevorex.scheduler.slots import SlotTrigger

    service, _, _ = make_service()
    await service._discover()
    job = service.scheduler.get_job("check:miami-fl")
    assert job is not None and isinstance(job.trigger, SlotTrigger)
    assert job.trigger.offset_minutes == service.cities["miami-fl"].stagger_minutes


@pytest.mark.asyncio
async def test_post_check_refresh_is_scheduled_as_its_own_job() -> None:
    service, _, clock = make_service()
    service._defer_refresh("orlando-fl", "post_check")
    job = service.scheduler.get_job("post-check:orlando-fl")
    assert job is not None and job.args == ("orlando-fl", "post_check")
    assert service.runner.defer_refresh is not None
