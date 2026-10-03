"""The long-running scheduler service (``aevoraex-scheduler`` under NSSM)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from aevorex.alerts.brief import BriefSweep, run_brief_sweep
from aevorex.config import settings
from aevorex.scheduler import locks
from aevorex.scheduler.cities import ActiveCity, OrgMarketReader, discover
from aevorex.scheduler.config import ScheduleConfig, load_config
from aevorex.scheduler.drift import DriftReading, check_clock
from aevorex.scheduler.jobs import JobRunner, utcnow
from aevorex.scheduler.late import evaluate_lateness
from aevorex.scheduler.logs import configure_logging
from aevorex.scheduler.observability import (
    Healthchecks,
    init_sentry,
    report_warning,
)
from aevorex.scheduler.ops import EngineOperations, sweep_orphaned_runs
from aevorex.scheduler.slots import CHECK, REFRESH, SlotTrigger, previous_slot

LOGGER = logging.getLogger("aevorex.scheduler")
BriefSweeper = Callable[[datetime, Mapping[str, int]], Awaitable[BriefSweep]]
SHUTDOWN_GRACE_SECONDS = 10


class SchedulerService:
    """APScheduler wiring: slot jobs per active city plus the housekeeping loops."""

    def __init__(
        self,
        config: ScheduleConfig,
        ops: EngineOperations,
        org_reader: OrgMarketReader | None,
        *,
        clock: Any = utcnow,
        paused: bool = False,
        brief_sweeper: BriefSweeper | None = None,
    ) -> None:
        self.config = config
        self.ops = ops
        self.org_reader = org_reader
        self.clock = clock
        self.paused = paused
        self.brief_sweeper = brief_sweeper
        self.cities: dict[str, ActiveCity] = {}
        self.org_slugs: set[str] = set()
        self.unsupported: dict[str, tuple[str, ...]] = {}
        self.discovered_at: dict[str, datetime] = {}
        self.drift: DriftReading | None = None
        self.healthchecks = Healthchecks()
        self.scheduler = AsyncIOScheduler(
            timezone=UTC,
            job_defaults={
                "coalesce": True,  # slots missed while asleep/offline collapse into one run
                "max_instances": 1,
                "misfire_grace_time": config.service.misfire_grace_minutes * 60,
            },
        )
        self.runner = JobRunner(
            config,
            ops,
            lambda: self.cities,
            clock=clock,
            healthchecks=self.healthchecks,
            runs_url=settings.healthchecks_runs_url,
            retry_hook=self._schedule_retry,
            defer_refresh=self._defer_refresh,
            paused=lambda: self.paused,
        )

    # ------------------------------------------------------------------ job registration
    def _add_date_job(self, func: Any, run_at: datetime, job_id: str, *args: Any) -> None:
        self.scheduler.add_job(
            func,
            trigger=DateTrigger(run_date=run_at, timezone=UTC),
            args=list(args),
            id=job_id,
            replace_existing=True,
        )

    def _schedule_retry(self, slug: str, kind: str, delay: timedelta) -> None:
        """One-shot retry at the end of a backoff (replaces any earlier retry)."""
        run_at = self.clock() + delay
        func = self.runner.check_cycle if kind == "check" else self.runner.refresh_cycle
        self._add_date_job(func, run_at, f"retry:{kind}:{slug}", slug, "retry")
        LOGGER.info("Retry scheduled: market=%s kind=%s at=%s", slug, kind, run_at.isoformat())

    def _defer_refresh(self, slug: str, trigger: str) -> None:
        """Run the post-check detail refresh as its own job (see ``JobRunner.check_cycle``)."""
        self._add_date_job(
            self.runner.refresh_cycle,
            self.clock() + timedelta(seconds=1),
            f"post-check:{slug}",
            slug,
            trigger,
        )

    def _register_city(self, city: ActiveCity) -> None:
        schedule = self.config.for_market(city.slug)
        self.scheduler.add_job(
            self.runner.check_cycle,
            trigger=SlotTrigger(CHECK, schedule, city.timezone, city.stagger_minutes),
            args=[city.slug, "scheduled"],
            id=f"check:{city.slug}",
            replace_existing=True,
        )
        if schedule.nightly_refresh is not None:
            self.scheduler.add_job(
                self.runner.nightly_cycle,
                trigger=SlotTrigger(REFRESH, schedule, city.timezone, city.stagger_minutes),
                args=[city.slug, "nightly"],
                id=f"nightly:{city.slug}",
                replace_existing=True,
            )

    def _unregister_city(self, slug: str) -> None:
        for job_id in (
            f"check:{slug}", f"nightly:{slug}", f"warm:{slug}", f"catchup:{slug}",
            f"retry:check:{slug}", f"retry:refresh:{slug}", f"post-check:{slug}",
        ):
            if self.scheduler.get_job(job_id) is not None:
                self.scheduler.remove_job(job_id)

    def _register_housekeeping(self) -> None:
        service = self.config.service
        for func, minutes, job_id in (
            (self._discover, service.discovery_minutes, "discovery"),
            (self._late_detector, service.late_detector_minutes, "late-detector"),
            (self._heartbeat, service.heartbeat_minutes, "heartbeat"),
            (self._clock_check, service.clock_check_minutes, "clock-check"),
            (self.runner.drain_queue, service.discovery_minutes, "queue-drain"),
        ):
            self.scheduler.add_job(
                func, "interval", minutes=minutes, id=job_id, replace_existing=True,
                next_run_time=self.clock() + timedelta(seconds=15),
            )
        if self.brief_sweeper is not None:
            self.scheduler.add_job(
                self._brief_sweep, "interval", minutes=service.brief_sweep_minutes,
                id="brief-sweep", replace_existing=True,
                next_run_time=self.clock() + timedelta(seconds=45),
            )
        finalize = service.nightly_finalize
        self.scheduler.add_job(
            self.runner.finalize,
            trigger=CronTrigger(
                hour=finalize.hour,
                minute=finalize.minute,
                timezone=ZoneInfo(service.nightly_finalize_tz),
            ),
            id="nightly-finalize",
            replace_existing=True,
        )

    # --------------------------------------------------------------------------- discovery
    async def _discover(self) -> None:
        discovery, self.org_slugs = await discover(
            self.config, self.org_reader, previous_org_slugs=self.org_slugs
        )
        self.unsupported = discovery.unsupported
        now = self.clock()
        added = sorted(discovery.active.keys() - self.cities.keys())
        removed = sorted(self.cities.keys() - discovery.active.keys())
        self.cities = discovery.active
        for slug in removed:
            self._unregister_city(slug)
            LOGGER.info("Market no longer active: market=%s", slug)
        for index, slug in enumerate(added):
            city = self.cities[slug]
            self.discovered_at[slug] = now
            self._register_city(city)
            last_checked, _status = await self.ops.freshness(slug)
            cadence = timedelta(minutes=self.config.for_market(slug).check_cadence_minutes)
            warming = last_checked is None
            stale = (
                not warming
                and now - _aware(last_checked) > cadence  # type: ignore[arg-type]
            )
            if warming or stale:
                trigger = "warming" if warming else "catchup"
                self._add_date_job(
                    self.runner.check_cycle, now + timedelta(seconds=1 + index),
                    f"warm:{slug}", slug, trigger,
                )
            LOGGER.info(
                "Market active: market=%s sources=%s tz=%s stagger_minutes=%s start=%s",
                slug, ",".join(city.sources), city.timezone, city.stagger_minutes,
                "warming" if warming else "catchup" if stale else "none",
            )

    # ------------------------------------------------------------------- housekeeping jobs
    async def _late_detector(self) -> None:
        now = self.clock()
        grace = timedelta(minutes=self.config.service.late_grace_minutes)
        for slug, city in list(self.cities.items()):
            schedule = self.config.for_market(slug)
            due = previous_slot(
                CHECK, schedule, ZoneInfo(city.timezone), city.stagger_minutes, now
            )
            last_checked, status = await self.ops.freshness(slug)
            verdict = evaluate_lateness(
                now, last_checked, due, grace, discovered_at=self.discovered_at.get(slug)
            )
            if not verdict.late:
                continue
            if status not in {"late", "failed"}:
                LOGGER.error(
                    "Market is late: market=%s overdue_minutes=%.0f", slug, verdict.overdue_minutes
                )
                report_warning("market_late", slug)
                await self.ops.set_local_status(slug, "late")
                try:
                    await self.ops.mark_status(slug, "late", None)
                except Exception as exc:
                    LOGGER.warning(
                        "Remote late flag failed: market=%s error_class=%s",
                        slug, type(exc).__name__,
                    )
                    self.runner.publish_debt.add(slug)
            if (
                ("check", slug) not in self.runner.inflight
                and not self.runner.backoff.active(now)
                and not self.paused
            ):
                self._add_date_job(
                    self.runner.check_cycle, now + timedelta(seconds=1),
                    f"catchup:{slug}", slug, "catchup",
                )

    async def _heartbeat(self) -> None:
        body = json.dumps(
            {
                "markets": len(self.cities),
                "inflight": len(self.runner.inflight),
                "backoff_failures": self.runner.backoff.failures,
                "publish_debt": len(self.runner.publish_debt),
                "paused": self.paused,
                "clock": self.drift.status if self.drift else "unchecked",
            },
            sort_keys=True,
        )
        failing = self.drift is not None and self.drift.status == "fail"
        await self.healthchecks.ping(
            settings.healthchecks_scheduler_url, status="fail" if failing else "", body=body
        )
        try:
            await self.ops.heartbeat("scheduler")
        except Exception as exc:
            LOGGER.warning("Remote heartbeat failed: error_class=%s", type(exc).__name__)
        if self.runner.publish_debt and not self.paused:
            flushed = await self.runner.flush_publish_debt()
            LOGGER.info("Publish debt retried: flushed=%s remaining=%s",
                        flushed, len(self.runner.publish_debt))

    async def _brief_sweep(self) -> None:
        """Queue every morning brief that is due (07:00-11:00 org-local); idempotent."""
        if self.brief_sweeper is None or self.paused:
            return
        try:
            sweep = await self.brief_sweeper(self.clock(), self.config.service.brief_counts)
        except Exception as exc:
            LOGGER.warning("Brief sweep failed: error_class=%s", type(exc).__name__)
            report_warning("brief_sweep_failed")
            return
        if sweep.enqueued or sweep.failed or sweep.too_late:
            LOGGER.info("Brief sweep: %s", json.dumps(sweep.as_counts(), sort_keys=True))

    async def _clock_check(self) -> None:
        service = self.config.service
        self.drift = await check_clock(service.clock_warn_seconds, service.clock_fail_seconds)
        offset = self.drift.offset_seconds
        message = (
            f"Clock drift: status={self.drift.status} source={self.drift.source} "
            f"offset_seconds={offset:.2f}" if offset is not None
            else f"Clock drift: status={self.drift.status} source={self.drift.source}"
        )
        if self.drift.status == "fail":
            LOGGER.error(message)
            report_warning("clock_drift")
        elif self.drift.status == "warn":
            LOGGER.warning(message)
        else:
            LOGGER.info(message)

    # --------------------------------------------------------------------------- lifecycle
    async def run(self, stop: asyncio.Event) -> dict[str, int]:
        """Run until ``stop`` is set. Holds the single-instance lock throughout."""
        try:
            async with locks.advisory_lock(locks.SERVICE, wait=False):
                swept = await sweep_orphaned_runs(
                    max_age=timedelta(
                        minutes=max(self.config.service.job_timeout_minutes.values()) + 10
                    ),
                    current_pid=os.getpid(),
                )
                if swept:
                    LOGGER.warning("Failed orphaned ledger rows: count=%s", swept)
                self.scheduler.start()
                self._register_housekeeping()
                await self._discover()
                await self._clock_check()
                LOGGER.info(
                    "Scheduler started: markets=%s paused=%s unsupported=%s",
                    ",".join(sorted(self.cities)), self.paused, ",".join(sorted(self.unsupported)),
                )
                await stop.wait()
                LOGGER.info("Scheduler stopping")
                self.scheduler.shutdown(wait=False)
                await _drain(SHUTDOWN_GRACE_SECONDS)
        except locks.LockBusy:
            LOGGER.error("Another scheduler instance holds the service lock; exiting")
            raise RuntimeError("SchedulerAlreadyRunning") from None
        finally:
            await self.ops.close()
        return {"markets": len(self.cities)}


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _drain(grace_seconds: float) -> None:
    """Give in-flight jobs a moment to finish, then cancel them (their ledger rows fail)."""
    current = asyncio.current_task()
    tasks = [task for task in asyncio.all_tasks() if task is not current]
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    def handler(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        loop.call_soon_threadsafe(stop.set)

    # Windows: NSSM stops a console service with Ctrl-C (SIGINT) / Ctrl-Break (SIGBREAK).
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, handler)


async def run_service() -> dict[str, int]:
    """Entry used by ``scheduler run`` and ``python -m aevorex.scheduler``."""
    configure_logging(
        "logs/scheduler.jsonl", level=settings.log_level, console_level="WARNING"
    )
    init_sentry(settings.sentry_dsn, settings.sentry_environment)
    config = load_config(settings.scheduler_config_path)

    from aevorex.publisher.remote import publisher_engine
    from aevorex.scheduler.cities import remote_org_market_reader

    reader: OrgMarketReader | None = None
    sweeper: BriefSweeper | None = None
    if settings.supabase_direct_connection_url is not None:
        remote = publisher_engine()
        reader = remote_org_market_reader(remote)

        async def sweeper(now: datetime, counts: Mapping[str, int]) -> BriefSweep:
            return await run_brief_sweep(remote, now, counts_by_plan=counts)

    else:
        LOGGER.warning("SUPABASE_DIRECT_CONNECTION_URL unset: demo markets only, nothing publishes")

    service = SchedulerService(
        config, EngineOperations(), reader, paused=settings.scheduler_paused,
        brief_sweeper=sweeper,
    )
    stop = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop)
    return await service.run(stop)


def main() -> None:
    """``python -m aevorex.scheduler`` - NSSM runs ``main.py scheduler run`` instead."""
    from aevorex.db import close_engine

    async def entry() -> None:
        try:
            await run_service()
        finally:
            await close_engine()

    asyncio.run(entry())
