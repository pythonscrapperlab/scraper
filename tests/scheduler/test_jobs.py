"""The stage chain: ordering, backoff, concurrency lanes, publish debt, nightly finalize."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from pydantic import SecretStr

from aevorex.scheduler.config import ScheduleConfig
from aevorex.scheduler.jobs import JobRunner
from tests.scheduler.fakes import (
    MIAMI,
    ORLANDO,
    SAN_JOSE,
    Clock,
    FakeHealthchecks,
    FakeLocks,
    FakeOps,
)

CITIES = {c.slug: c for c in (ORLANDO, MIAMI, SAN_JOSE)}


def make_runner(
    ops: FakeOps | None = None,
    *,
    config: ScheduleConfig | None = None,
    paused: bool = False,
    wait: bool = True,
) -> tuple[JobRunner, FakeOps, FakeLocks, Clock, list[tuple[str, str, timedelta]]]:
    ops = ops or FakeOps()
    fake_locks = FakeLocks()
    clock = Clock()
    retries: list[tuple[str, str, timedelta]] = []
    runner = JobRunner(
        config or ScheduleConfig(),
        ops,
        lambda: CITIES,
        clock=clock,
        lock_factory=fake_locks,
        healthchecks=FakeHealthchecks(),
        runs_url=SecretStr("https://hc.invalid/runs"),
        retry_hook=lambda slug, kind, delay: retries.append((slug, kind, delay)),
        paused=lambda: paused,
        wait_for_locks=wait,
        poll_seconds=0.001,
    )
    return runner, ops, fake_locks, clock, retries


@pytest.mark.asyncio
async def test_quiet_check_pushes_freshness_only() -> None:
    runner, ops, _, _, _ = make_runner()
    result = await runner.check_cycle("orlando-fl")
    assert result.status == "ok"
    assert ops.names() == ["check", "set_next_check", "publish_freshness"]


@pytest.mark.asyncio
async def test_next_check_is_the_true_next_slot_not_now_plus_cadence() -> None:
    runner, ops, _, clock, _ = make_runner()
    await runner.check_cycle("orlando-fl")
    (_, (slug, next_check)) = next(c for c in ops.calls if c[0] == "set_next_check")
    assert slug == "orlando-fl"
    assert next_check > clock.now
    # 15:00Z is 11:00 EDT; stagger 14 -> the next slot is 11:14 EDT = 15:14Z
    assert next_check.isoformat() == "2026-10-03T15:14:00+00:00"


@pytest.mark.asyncio
async def test_changed_check_runs_refresh_analyze_then_full_publish_in_order() -> None:
    ops = FakeOps()
    ops.results["check"] = {"listings": 10, "events": 2, "changed": 2, "queued": 2}
    runner, ops, _, _, _ = make_runner(ops)
    result = await runner.check_cycle("orlando-fl")
    assert result.status == "ok"
    assert ops.names() == [
        "check", "set_next_check", "publish_freshness", "due_refresh_slugs",
        "recover_queue", "refresh_pending", "analyze", "publish_full",
    ]
    cadence = next(args for name, args in ops.calls if name == "publish_full")[2]
    assert cadence == 120


@pytest.mark.asyncio
async def test_unpublished_market_gets_a_full_push_instead_of_a_light_one() -> None:
    ops = FakeOps()
    ops.light_pushed = False
    runner, ops, _, _, _ = make_runner(ops)
    await runner.check_cycle("orlando-fl")
    assert ops.names()[-1] == "publish_full"


@pytest.mark.asyncio
async def test_check_failure_backs_off_marks_failed_and_schedules_retry() -> None:
    ops = FakeOps()
    ops.errors["check"] = lambda: RuntimeError("SearchFetchError")
    runner, ops, _, clock, retries = make_runner(ops)
    result = await runner.check_cycle("orlando-fl")
    assert result.status == "failed"
    assert ("mark_status", ("orlando-fl", "failed")) in ops.calls
    assert retries == [("orlando-fl", "check", timedelta(minutes=5))]
    assert "publish_freshness" not in ops.names()

    skipped = await runner.check_cycle("miami-fl")  # backoff is per source, not per market
    assert skipped.status == "skipped" and skipped.reason == "backoff"
    assert ops.names().count("check") == 1

    clock.now += timedelta(minutes=5)
    retried = await runner.check_cycle("orlando-fl", "retry")
    assert retried.status == "failed"
    assert retries[-1][2] == timedelta(minutes=10)


@pytest.mark.asyncio
async def test_success_after_failures_resets_backoff() -> None:
    ops = FakeOps()
    ops.errors["check"] = lambda: RuntimeError("x")
    runner, ops, _, clock, retries = make_runner(ops)
    await runner.check_cycle("orlando-fl")
    del ops.errors["check"]
    clock.now += timedelta(minutes=6)
    assert (await runner.check_cycle("orlando-fl", "retry")).status == "ok"
    assert runner.backoff.failures == 0
    ops.errors["check"] = lambda: RuntimeError("x")
    await runner.check_cycle("orlando-fl")
    assert retries[-1][2] == timedelta(minutes=5)


@pytest.mark.asyncio
async def test_run_now_bypasses_backoff_and_pause() -> None:
    ops = FakeOps()
    ops.errors["check"] = lambda: RuntimeError("x")
    runner, ops, _, _, _ = make_runner(ops, paused=True)
    assert (await runner.check_cycle("orlando-fl")).reason == "paused"
    assert ops.names() == []
    runner.backoff.record_failure(runner.clock())
    forced = await runner.check_cycle("orlando-fl", "run_now")
    assert forced.status == "failed"
    assert ops.names().count("check") == 1


@pytest.mark.asyncio
async def test_blocked_refresh_counts_as_source_failure_but_still_analyzes_successes() -> None:
    ops = FakeOps()
    ops.results["check"] = {"queued": 4, "changed": 4}
    ops.results["refresh_pending"] = {"queued": 4, "success": 2, "failed": 2, "block_rate": 0.5}
    runner, ops, _, _, retries = make_runner(ops)
    result = await runner.check_cycle("orlando-fl")
    assert result.status == "partial" and result.reason == "source_blocked"
    assert runner.backoff.failures == 1
    assert retries[-1][1] == "refresh"
    assert "analyze" in ops.names() and "publish_full" in ops.names()


@pytest.mark.asyncio
async def test_all_detail_fetches_failing_is_a_source_failure_without_publish() -> None:
    ops = FakeOps()
    ops.results["check"] = {"queued": 4, "changed": 4}
    ops.results["refresh_pending"] = {"queued": 4, "success": 0, "failed": 4, "block_rate": 0.0}
    ops.results["analyze"] = {"changed": 0}
    runner, ops, _, _, _ = make_runner(ops)
    await runner.check_cycle("orlando-fl")
    assert runner.backoff.failures == 1
    assert "publish_full" not in ops.names()


@pytest.mark.asyncio
async def test_publish_failure_does_not_stop_scraping_and_is_retried_later() -> None:
    ops = FakeOps()
    ops.results["check"] = {"queued": 1, "changed": 1}
    ops.errors["publish_full"] = lambda: ConnectionError("down")
    runner, ops, _, _, _ = make_runner(ops)
    result = await runner.check_cycle("orlando-fl")
    assert "orlando-fl" in runner.publish_debt
    assert result.stages["publish"]["ok"] is False
    assert runner.backoff.failures == 0  # Supabase being down is not a Redfin problem
    del ops.errors["publish_full"]
    assert await runner.flush_publish_debt() == 1
    assert runner.publish_debt == set()


@pytest.mark.asyncio
async def test_light_publish_failure_is_remembered_not_raised() -> None:
    ops = FakeOps()
    ops.errors["publish_freshness"] = lambda: ConnectionError("down")
    runner, _, _, _, _ = make_runner(ops)
    assert (await runner.check_cycle("orlando-fl")).status == "ok"
    assert "orlando-fl" in runner.publish_debt


@pytest.mark.asyncio
async def test_stage_timeout_is_a_failure_with_its_class() -> None:
    ops = FakeOps()
    ops.delay["check"] = 0.2
    config = ScheduleConfig()
    config = replace(
        config, service=replace(config.service, job_timeout_minutes={"check": 0, "refresh": 1})
    )
    runner, _, _, _, _ = make_runner(ops, config=config)
    result = await runner.check_cycle("orlando-fl")
    assert result.status == "failed" and result.reason == "TimeoutError"


@pytest.mark.asyncio
async def test_one_check_at_a_time_but_check_and_refresh_may_overlap() -> None:
    ops = FakeOps()
    ops.delay["check"] = 0.03
    ops.delay["refresh_city"] = 0.03
    runner, ops, fake_locks, _, _ = make_runner(ops)
    await asyncio.gather(
        runner.check_cycle("orlando-fl"),
        runner.check_cycle("miami-fl"),
        runner.check_cycle("san-jose-ca"),
        runner.nightly_cycle("orlando-fl"),
        runner.nightly_cycle("miami-fl"),
    )
    assert ops.names().count("check") == 3
    assert fake_locks.max_held["check"] == 1
    assert fake_locks.max_held["refresh"] == 1
    assert ops.max_active["check"] == 1
    assert ops.max_active["refresh_city"] == 1


@pytest.mark.asyncio
async def test_duplicate_trigger_for_same_market_is_skipped_while_inflight() -> None:
    ops = FakeOps()
    ops.delay["check"] = 0.05
    runner, ops, _, _, _ = make_runner(ops)
    first, second = await asyncio.gather(
        runner.check_cycle("orlando-fl"), runner.check_cycle("orlando-fl", "catchup")
    )
    assert sorted([first.status, second.status]) == ["ok", "skipped"]
    assert ops.names().count("check") == 1


@pytest.mark.asyncio
async def test_busy_lane_with_no_wait_skips_instead_of_blocking() -> None:
    runner, ops, fake_locks, _, _ = make_runner(wait=False)
    async with fake_locks("check"):
        result = await runner.check_cycle("orlando-fl")
    assert result.status == "skipped" and result.reason == "lock_busy"
    assert ops.names() == []


@pytest.mark.asyncio
async def test_unknown_or_inactive_market_is_skipped() -> None:
    runner, ops, _, _, _ = make_runner()
    assert (await runner.check_cycle("nowhere-xx")).reason == "not_active"
    assert (await runner.nightly_cycle("nowhere-xx")).reason == "not_active"
    assert ops.names() == []


@pytest.mark.asyncio
async def test_recovered_orphans_do_not_block_the_refresh() -> None:
    ops = FakeOps()
    ops.recovered = 7
    ops.results["check"] = {"queued": 1, "changed": 1}
    runner, ops, _, _, _ = make_runner(ops)
    assert (await runner.check_cycle("orlando-fl")).status == "ok"


@pytest.mark.asyncio
async def test_nightly_refreshes_whole_city_then_analyzes_and_publishes() -> None:
    runner, ops, _, _, _ = make_runner()
    result = await runner.nightly_cycle("orlando-fl")
    assert result.status == "ok"
    assert ops.names() == ["recover_queue", "refresh_city", "analyze", "publish_full"]
    assert runner.nightly_inflight == set()


@pytest.mark.asyncio
async def test_nightly_failure_backs_off_and_still_releases_the_inflight_marker() -> None:
    ops = FakeOps()
    ops.errors["refresh_city"] = lambda: RuntimeError("boom")
    runner, _, _, _, retries = make_runner(ops)
    result = await runner.nightly_cycle("orlando-fl")
    assert result.status == "failed"
    assert runner.nightly_inflight == set()
    assert retries == [("orlando-fl", "refresh", timedelta(minutes=5))]


@pytest.mark.asyncio
async def test_finalize_runs_market_stats_once_per_interval_then_analyzes() -> None:
    runner, ops, _, clock, _ = make_runner()
    ops.stats_at = clock.now - timedelta(hours=25)
    result = await runner.finalize()
    assert result.status == "ok"
    assert ops.names()[:2] == ["market_stats", "analyze"]
    assert ops.names().count("publish_full") == len(CITIES)  # analyze changed rows -> push all

    runner, ops, _, clock, _ = make_runner()
    ops.stats_at = clock.now - timedelta(hours=3)
    ops.results["analyze"] = {"changed": 0}
    await runner.finalize()
    assert ops.names() == ["analyze"]  # stats fresh, nothing changed, nothing pushed


@pytest.mark.asyncio
async def test_finalize_waits_for_inflight_nightlies_then_reports_overrun_if_they_never_end() -> None:
    config = ScheduleConfig()
    config = replace(
        config,
        service=replace(config.service, nightly_finalize_max_wait_minutes=0),
    )
    runner, ops, _, _, _ = make_runner(config=config)
    runner.nightly_inflight.add("orlando-fl")
    result = await runner.finalize()
    assert result.status == "partial" and result.reason == "nightly_overrun"
    assert "analyze" in ops.names()


@pytest.mark.asyncio
async def test_run_now_jobs_route_to_the_right_operation() -> None:
    runner, ops, _, _, _ = make_runner()
    for job, expected in (
        ("refresh", "refresh_pending"),
        ("analyze", "analyze"),
        ("market_stats", "market_stats"),
        ("publish", "publish_full"),
    ):
        ops.calls.clear()
        slug = "*" if job in {"analyze", "market_stats"} else "orlando-fl"
        result = await runner.run_now(slug, job)
        assert result.status == "ok", job
        assert expected in ops.names()
    with pytest.raises(ValueError):
        await runner.run_now("orlando-fl", "bogus")


@pytest.mark.asyncio
async def test_run_pings_start_then_success_with_numeric_counts_only() -> None:
    ops = FakeOps()
    ops.results["check"] = {"listings": 5, "note": "https://leak.invalid/addr", "queued": 0}
    runner, _, _, _, _ = make_runner(ops)
    await runner.check_cycle("orlando-fl")
    pings = runner.healthchecks.pings  # type: ignore[attr-defined]
    assert pings[0] == ("start", None)
    status, body = pings[1]
    assert status == "" and body is not None
    assert "leak" not in body and '"listings": 5' in body


@pytest.mark.asyncio
async def test_failure_pings_fail() -> None:
    ops = FakeOps()
    ops.errors["check"] = lambda: RuntimeError("x")
    runner, _, _, _, _ = make_runner(ops)
    await runner.check_cycle("orlando-fl")
    assert [s for s, _ in runner.healthchecks.pings] == ["start", "fail"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_queue_wide_refresh_publishes_every_market_whose_rows_it_settled() -> None:
    ops = FakeOps()
    ops.results["check"] = {"queued": 1, "changed": 1}
    ops.due = {"miami-fl": 5, "san-jose-ca": 2, "naples-fl": 9}  # naples is not active
    runner, ops, _, _, _ = make_runner(ops)
    await runner.check_cycle("orlando-fl")
    published = [args[0] for name, args in ops.calls if name == "publish_full"]
    assert published == ["miami-fl", "orlando-fl", "san-jose-ca"]


@pytest.mark.asyncio
async def test_drain_picks_the_busiest_active_market_and_publishes_all_affected() -> None:
    ops = FakeOps()
    ops.due = {"miami-fl": 30, "orlando-fl": 4, "naples-fl": 99}
    runner, ops, _, _, _ = make_runner(ops)
    result = await runner.drain_queue()
    assert result is not None and result.status == "ok"
    assert result.slug == "miami-fl" and result.kind == "refresh"
    assert ("refresh_pending", ("miami-fl", "queue_drain")) in ops.calls
    assert [a[0] for n, a in ops.calls if n == "publish_full"] == ["miami-fl", "orlando-fl"]


@pytest.mark.asyncio
async def test_drain_is_a_no_op_when_nothing_is_waiting_or_it_would_collide() -> None:
    runner, ops, _, _, _ = make_runner()
    assert await runner.drain_queue() is None  # nothing due
    assert ops.names() == ["due_refresh_slugs"]

    ops.due = {"miami-fl": 3}
    runner.refresh_active = 1
    ops.calls.clear()
    assert await runner.drain_queue() is None  # a refresh is already running
    assert ops.names() == []
    runner.refresh_active = 0

    runner.backoff.record_failure(runner.clock())
    assert await runner.drain_queue() is None  # Redfin is cooling off
    runner.backoff.record_success()

    ops.due = {"naples-fl": 3}
    assert await runner.drain_queue() is None  # only inactive markets have rows


@pytest.mark.asyncio
async def test_drain_respects_pause() -> None:
    ops = FakeOps()
    ops.due = {"miami-fl": 3}
    runner, ops, _, _, _ = make_runner(ops, paused=True)
    assert await runner.drain_queue() is None
    assert ops.names() == []


@pytest.mark.asyncio
async def test_service_mode_detaches_the_post_check_refresh_so_the_check_job_ends() -> None:
    ops = FakeOps()
    ops.results["check"] = {"queued": 4, "changed": 4}
    runner, ops, _, _, _ = make_runner(ops)
    deferred: list[tuple[str, str]] = []
    runner.defer_refresh = lambda slug, trigger: deferred.append((slug, trigger))
    result = await runner.check_cycle("orlando-fl")
    assert result.status == "ok"
    assert deferred == [("orlando-fl", "post_check")]
    assert result.stages["refresh"] == {"deferred": True}
    assert not {"refresh_pending", "analyze", "publish_full"} & set(ops.names())
    assert ("check", "orlando-fl") not in runner.inflight  # the next slot is free to run


@pytest.mark.asyncio
async def test_run_now_check_still_runs_the_whole_chain_inline() -> None:
    ops = FakeOps()
    ops.results["check"] = {"queued": 4, "changed": 4}
    runner, ops, _, _, _ = make_runner(ops)
    runner.defer_refresh = lambda slug, trigger: pytest.fail("run-now must not detach")
    await runner.check_cycle("orlando-fl", "run_now")
    assert "refresh_pending" in ops.names() and "publish_full" in ops.names()


@pytest.mark.asyncio
async def test_a_long_refresh_does_not_block_the_next_check_slot() -> None:
    ops = FakeOps()
    ops.results["check"] = {"queued": 4, "changed": 4}
    ops.delay["refresh_pending"] = 0.2
    runner, ops, _, _, _ = make_runner(ops)
    refreshes: list[asyncio.Task[object]] = []
    runner.defer_refresh = lambda slug, trigger: refreshes.append(
        asyncio.ensure_future(runner.refresh_cycle(slug, trigger))
    )
    first = await runner.check_cycle("orlando-fl")
    second = await runner.check_cycle("orlando-fl")  # next slot while the refresh is running
    assert first.status == "ok" and second.status == "ok"
    assert ops.names().count("check") == 2
    await asyncio.gather(*refreshes)
