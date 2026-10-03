"""The scheduled stage chain: check -> refresh -> analyze -> publish.

Each public coroutine (``check_cycle``, ``nightly_cycle``, ``finalize``) is what APScheduler,
``scheduler run-now`` and the tests all call, so there is exactly one implementation of
the chain. Redfin-facing failures share one :class:`SourceBackoff`; publisher outages never
block scraping - the market is remembered in ``publish_debt`` and re-pushed on heartbeat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import SecretStr

from aevorex.scheduler import locks
from aevorex.scheduler.backoff import SourceBackoff
from aevorex.scheduler.cities import ActiveCity
from aevorex.scheduler.config import ScheduleConfig
from aevorex.scheduler.late import aware_utc
from aevorex.scheduler.observability import Healthchecks, report_failure, report_warning
from aevorex.scheduler.ops import Operations
from aevorex.scheduler.slots import CHECK, next_slot

LOGGER = logging.getLogger(__name__)
LockFactory = Callable[..., AbstractAsyncContextManager[None]]
RetryHook = Callable[[str, str, timedelta], None]
DeferHook = Callable[[str, str], None]


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class StageOutcome:
    """Result of one lane-locked stage."""

    ok: bool
    counts: dict[str, Any] = field(default_factory=dict)
    error_class: str | None = None
    skipped: str | None = None


@dataclass
class CycleResult:
    """What a cycle did, for run-now output and tests. Never contains listing data."""

    slug: str
    kind: str
    status: str  # ok | failed | partial | skipped
    reason: str | None = None
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)


def _numeric(counts: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in counts.items()
        if isinstance(value, bool | int | float)
    }


class JobRunner:
    """Executes scheduled chains with locking, backoff, pings and error reporting."""

    def __init__(
        self,
        config: ScheduleConfig,
        ops: Operations,
        cities: Callable[[], Mapping[str, ActiveCity]],
        *,
        clock: Callable[[], datetime] = utcnow,
        lock_factory: LockFactory = locks.advisory_lock,
        healthchecks: Healthchecks | None = None,
        runs_url: SecretStr | None = None,
        retry_hook: RetryHook | None = None,
        defer_refresh: DeferHook | None = None,
        paused: Callable[[], bool] = lambda: False,
        wait_for_locks: bool = True,
        poll_seconds: float = 30.0,
    ) -> None:
        self.config = config
        self.ops = ops
        self.cities = cities
        self.clock = clock
        self.lock_factory = lock_factory
        self.healthchecks = healthchecks or Healthchecks()
        self.runs_url = runs_url
        self.retry_hook = retry_hook
        self.defer_refresh = defer_refresh
        self.paused = paused
        self.wait_for_locks = wait_for_locks
        self.poll_seconds = poll_seconds
        service = config.service
        self.backoff = SourceBackoff(service.backoff_base_minutes, service.backoff_cap_minutes)
        self.inflight: set[tuple[str, str]] = set()
        self.nightly_inflight: set[str] = set()
        self.publish_debt: set[str] = set()
        self.refresh_active = 0

    # ------------------------------------------------------------------ stage plumbing
    async def _ping(self, status: str, run_id: UUID, body: str | None = None) -> None:
        await self.healthchecks.ping(self.runs_url, status=status, run_id=run_id, body=body)

    async def _stage(
        self,
        name: str,
        slug: str,
        lane: str,
        operation: Callable[[], Awaitable[Mapping[str, Any]]],
    ) -> StageOutcome:
        run_id = uuid4()
        timeout = self.config.service.job_timeout_minutes.get(lane, 60) * 60
        await self._ping("start", run_id)
        started = time.monotonic()
        try:
            async with self.lock_factory(lane, wait=self.wait_for_locks):
                started = time.monotonic()
                counts = await asyncio.wait_for(operation(), timeout)
        except locks.LockBusy:
            LOGGER.info("Stage skipped, lane busy: stage=%s market=%s lane=%s", name, slug, lane)
            return StageOutcome(False, skipped="lock_busy")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_class = type(exc).__name__
            LOGGER.error("Stage failed: stage=%s market=%s error_class=%s", name, slug, error_class)
            report_failure(name, error_class, slug)
            await self._ping("fail", run_id)
            return StageOutcome(False, error_class=error_class)
        numeric = _numeric(counts)
        LOGGER.info(
            "Stage complete: stage=%s market=%s seconds=%.1f counts=%s",
            name, slug, time.monotonic() - started, json.dumps(numeric, sort_keys=True),
        )
        await self._ping("", run_id, json.dumps(numeric, sort_keys=True))
        return StageOutcome(True, numeric)

    def _retry(self, slug: str, kind: str, delay: timedelta) -> None:
        if self.retry_hook is not None:
            self.retry_hook(slug, kind, delay)

    def _city(self, slug: str) -> ActiveCity | None:
        return self.cities().get(slug)

    def _record_source_failure(self, slug: str, kind: str, reason: str) -> timedelta:
        delay = self.backoff.record_failure(self.clock())
        LOGGER.warning(
            "Redfin backoff: market=%s kind=%s reason=%s failures=%s delay_minutes=%s",
            slug, kind, reason, self.backoff.failures, int(delay.total_seconds() // 60),
        )
        self._retry(slug, kind, delay)
        return delay

    # ------------------------------------------------------------------------- publish
    async def _publish_full(self, slug: str, trigger: str, result: CycleResult) -> bool:
        cadence = self.config.for_market(slug).check_cadence_minutes
        outcome = await self._stage(
            "publish", slug, locks.PUBLISH,
            lambda: self.ops.publish_full(slug, trigger, cadence),
        )
        result.stages["publish"] = {"ok": outcome.ok, **outcome.counts}
        if outcome.ok:
            self.publish_debt.discard(slug)
            if outcome.counts.get("partial"):
                result.status = "partial"
            return True
        self.publish_debt.add(slug)
        return False

    async def _publish_light(self, slug: str, trigger: str, result: CycleResult) -> None:
        """Push freshness only; a never-published market gets a full push instead."""
        try:
            pushed = await self.ops.publish_freshness(slug)
        except Exception as exc:
            LOGGER.warning(
                "Freshness push failed: market=%s error_class=%s", slug, type(exc).__name__
            )
            self.publish_debt.add(slug)
            result.stages["publish_freshness"] = {"ok": False}
            return
        result.stages["publish_freshness"] = {"ok": pushed}
        if not pushed:
            await self._publish_full(slug, trigger, result)

    async def flush_publish_debt(self) -> int:
        """Retry full pushes that failed earlier (called from the heartbeat)."""
        flushed = 0
        for slug in sorted(self.publish_debt):
            if self.paused():
                break
            result = CycleResult(slug, "publish_retry", "ok")
            if await self._publish_full(slug, "retry", result):
                flushed += 1
        return flushed

    # --------------------------------------------------------------------------- check
    async def check_cycle(self, slug: str, trigger: str = "scheduled") -> CycleResult:
        """Search-level check, then (only if something changed) refresh -> analyze -> push."""
        result = CycleResult(slug, "check", "ok")
        city = self._city(slug)
        if city is None:
            return self._skip(result, "not_active")
        manual = trigger == "run_now"
        if self.paused() and not manual:
            return self._skip(result, "paused")
        key = ("check", slug)
        if key in self.inflight:
            return self._skip(result, "inflight")
        if not manual and self.backoff.active(self.clock()):
            minutes = self.backoff.remaining(self.clock()).total_seconds() / 60
            LOGGER.info("Check deferred by backoff: market=%s remaining_minutes=%.0f", slug, minutes)
            return self._skip(result, "backoff")

        self.inflight.add(key)
        try:
            outcome = await self._stage(
                "check", slug, locks.CHECK, lambda: self.ops.check(slug, trigger)
            )
            result.stages["check"] = {"ok": outcome.ok, **outcome.counts}
            if outcome.skipped:
                return self._skip(result, outcome.skipped)
            if not outcome.ok:
                result.status = "failed"
                result.reason = outcome.error_class
                self._record_source_failure(slug, "check", outcome.error_class or "unknown")
                await self._mark_remote(slug, "failed")
                return result
            self.backoff.record_success()

            next_check = next_slot(
                CHECK,
                self.config.for_market(slug),
                ZoneInfo(city.timezone),
                city.stagger_minutes,
                self.clock(),
            )
            if next_check is not None:
                await self.ops.set_next_check(slug, next_check)
            await self._publish_light(slug, trigger, result)

            if int(outcome.counts.get("queued", 0)) > 0:
                if self.defer_refresh is not None and not manual:
                    # A detail refresh can wait hours for the refresh lane. Hand it to its own
                    # job so this one ends now and the next check slot is not skipped as
                    # "max instances reached".
                    self.defer_refresh(slug, "post_check")
                    result.stages["refresh"] = {"deferred": True}
                else:
                    await self._refresh_analyze_publish(
                        slug, trigger, result, whole_city=False
                    )
            return result
        finally:
            self.inflight.discard(key)

    def _skip(self, result: CycleResult, reason: str) -> CycleResult:
        result.status = "skipped"
        result.reason = reason
        return result

    async def _mark_remote(self, slug: str, status: str) -> None:
        try:
            await self.ops.mark_status(slug, status, None)
        except Exception as exc:
            LOGGER.warning(
                "Remote status update failed: market=%s status=%s error_class=%s",
                slug, status, type(exc).__name__,
            )
            self.publish_debt.add(slug)

    # ---------------------------------------------------------------- refresh chain
    async def _refresh_analyze_publish(
        self, slug: str, trigger: str, result: CycleResult, *, whole_city: bool
    ) -> None:
        self.refresh_active += 1
        try:
            await self._refresh_analyze_publish_inner(slug, trigger, result, whole_city)
        finally:
            self.refresh_active -= 1

    async def _refresh_analyze_publish_inner(
        self, slug: str, trigger: str, result: CycleResult, whole_city: bool
    ) -> None:
        if self.backoff.active(self.clock()):
            result.status = "partial"
            result.reason = "refresh_deferred_by_backoff"
            self._retry(slug, "refresh", self.backoff.remaining(self.clock()))
            return

        # A queue-wide refresh also settles other markets' waiting rows; publish them too.
        affected = {slug}
        if not whole_city:
            try:
                affected |= set(await self.ops.due_refresh_slugs())
            except Exception as exc:
                LOGGER.warning("Queue inspection failed: error_class=%s", type(exc).__name__)

        async def refresh() -> Mapping[str, Any]:
            recovered = await self.ops.recover_queue()
            if recovered:
                LOGGER.warning("Recovered orphaned detail rows: count=%s", recovered)
            if whole_city:
                return await self.ops.refresh_city(slug, trigger)
            return await self.ops.refresh_pending(slug, trigger)

        refreshed = await self._stage("refresh", slug, locks.REFRESH, refresh)
        result.stages["refresh"] = {"ok": refreshed.ok, **refreshed.counts}
        if refreshed.skipped:
            result.status = "partial"
            result.reason = refreshed.skipped
            return
        if not refreshed.ok:
            result.status = "failed"
            result.reason = refreshed.error_class
            self._record_source_failure(slug, "refresh", refreshed.error_class or "unknown")
            return
        self._judge_refresh(slug, refreshed.counts, result)

        analyzed = await self._stage(
            "analyze", slug, locks.ANALYZE, lambda: self.ops.analyze(trigger)
        )
        result.stages["analyze"] = {"ok": analyzed.ok, **analyzed.counts}
        if not analyzed.ok:
            result.status = "failed"
            result.reason = analyzed.error_class
            return
        if int(refreshed.counts.get("success", 0)) > 0 or int(analyzed.counts.get("changed", 0)) > 0:
            for market in sorted(affected & set(self.cities())):
                await self._publish_full(market, trigger, result)

    def _judge_refresh(self, slug: str, counts: Mapping[str, Any], result: CycleResult) -> None:
        """A refresh that mostly bounced off Redfin counts as a source failure."""
        queued = int(counts.get("queued", 0))
        success = int(counts.get("success", 0))
        block_rate = float(counts.get("block_rate", 0.0))
        blocked_out = queued > 0 and success == 0
        if blocked_out or block_rate >= self.config.service.block_rate_backoff:
            self._record_source_failure(
                slug, "refresh", "blocked" if block_rate else "no_successes"
            )
            result.status = "partial"
            result.reason = "source_blocked"
        else:
            self.backoff.record_success()

    async def refresh_cycle(self, slug: str, trigger: str = "retry") -> CycleResult:
        """Consume the durable detail queue (backoff retries); then analyze and publish."""
        result = CycleResult(slug, "refresh", "ok")
        if self._city(slug) is None:
            return self._skip(result, "not_active")
        if self.paused():
            return self._skip(result, "paused")
        key = ("refresh", slug)
        if key in self.inflight:
            return self._skip(result, "inflight")
        self.inflight.add(key)
        try:
            await self._refresh_analyze_publish(slug, trigger, result, whole_city=False)
            return result
        finally:
            self.inflight.discard(key)

    async def drain_queue(self) -> CycleResult | None:
        """Work off detail rows nobody is waiting on (after a restart, or failed-row retries)."""
        if self.paused() or self.refresh_active or self.backoff.active(self.clock()):
            return None
        try:
            due = await self.ops.due_refresh_slugs()
        except Exception as exc:
            LOGGER.warning("Queue inspection failed: error_class=%s", type(exc).__name__)
            return None
        active = {slug: count for slug, count in due.items() if slug in self.cities()}
        if not active:
            return None
        slug = max(sorted(active), key=lambda market: active[market])
        LOGGER.info("Draining detail queue: waiting=%s", sum(due.values()))
        return await self.refresh_cycle(slug, "queue_drain")

    # ----------------------------------------------------------------------- nightly
    async def nightly_cycle(self, slug: str, trigger: str = "nightly") -> CycleResult:
        """Full detail refresh of one city, then analyze and a full push."""
        result = CycleResult(slug, "nightly", "ok")
        if self._city(slug) is None:
            return self._skip(result, "not_active")
        if self.paused():
            return self._skip(result, "paused")
        key = ("refresh", slug)
        if key in self.inflight:
            return self._skip(result, "inflight")
        self.inflight.add(key)
        self.nightly_inflight.add(slug)
        try:
            await self._refresh_analyze_publish(slug, trigger, result, whole_city=True)
            if "refresh" in result.stages and result.stages["refresh"].get("ok"):
                counts = result.stages["refresh"]
                LOGGER.info(
                    "Nightly refresh summary: market=%s queued=%s success=%s failed=%s",
                    slug, counts.get("queued"), counts.get("success"), counts.get("failed"),
                )
            return result
        finally:
            self.inflight.discard(key)
            self.nightly_inflight.discard(slug)

    async def finalize(self, trigger: str = "nightly") -> CycleResult:
        """Once per night: market-stats after the nightly refreshes, then a catch-up analyze."""
        result = CycleResult("*", "finalize", "ok")
        if self.paused():
            return self._skip(result, "paused")
        service = self.config.service
        deadline = time.monotonic() + service.nightly_finalize_max_wait_minutes * 60
        while self.nightly_inflight and time.monotonic() < deadline:
            await asyncio.sleep(self.poll_seconds)
        if self.nightly_inflight:
            LOGGER.error(
                "Nightly refresh overran its window: unfinished_markets=%s",
                ",".join(sorted(self.nightly_inflight)),
            )
            report_warning("nightly_overrun")
            result.status = "partial"
            result.reason = "nightly_overrun"

        last = aware_utc(await self.ops.last_market_stats_at())
        interval = timedelta(hours=service.market_stats_min_interval_hours)
        if last is None or self.clock() - last >= interval:
            stats = await self._stage(
                "market_stats", "*", locks.ANALYZE, lambda: self.ops.market_stats(trigger)
            )
            result.stages["market_stats"] = {"ok": stats.ok, **stats.counts}
            if not stats.ok:
                result.status = "failed"
                result.reason = stats.error_class
                return result
        analyzed = await self._stage(
            "analyze", "*", locks.ANALYZE, lambda: self.ops.analyze(trigger)
        )
        result.stages["analyze"] = {"ok": analyzed.ok, **analyzed.counts}
        if analyzed.ok and int(analyzed.counts.get("changed", 0)) > 0:
            for slug in sorted(self.cities()):
                await self._publish_full(slug, trigger, result)
        elif not analyzed.ok:
            result.status = "failed"
            result.reason = analyzed.error_class
        return result

    # ----------------------------------------------------------------------- run-now
    async def run_now(self, slug: str, job: str) -> CycleResult:
        """Run one chain immediately (the CLI uses ``wait_for_locks=False``)."""
        if job == "check":
            return await self.check_cycle(slug, "run_now")
        if job == "nightly":
            return await self.nightly_cycle(slug, "run_now")
        result = CycleResult(slug, job, "ok")
        if job not in {"analyze", "market_stats"} and self._city(slug) is None:
            return self._skip(result, "not_active")
        if job == "refresh":
            outcome = await self._stage(
                "refresh", slug, locks.REFRESH, lambda: self.ops.refresh_pending(slug, "run_now")
            )
        elif job == "analyze":
            outcome = await self._stage(
                "analyze", slug, locks.ANALYZE, lambda: self.ops.analyze("run_now")
            )
        elif job == "market_stats":
            outcome = await self._stage(
                "market_stats", slug, locks.ANALYZE, lambda: self.ops.market_stats("run_now")
            )
        elif job == "publish":
            await self._publish_full(slug, "run_now", result)
            return result
        else:
            raise ValueError("Unknown run-now job")
        result.stages[job] = {"ok": outcome.ok, **outcome.counts}
        if outcome.skipped:
            return self._skip(result, outcome.skipped)
        if not outcome.ok:
            result.status = "failed"
            result.reason = outcome.error_class
        return result
