"""Operations the scheduler drives, bound to the existing freshness/publisher packages.

Each stage writes one PII-free ``runs`` row through :func:`aevorex.run_tracking.tracked`
(kinds: ``check``, ``refresh``, ``analyze``, ``market_stats``, ``publish``). Nothing here
changes scoring: valuation and scoring are only ever invoked through the E2 entry points.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import and_, func, or_, select, update

from aevorex.db.models import MarketFreshness, PendingRefresh, Run, utc_now
from aevorex.db.session import async_session_maker
from aevorex.run_tracking import JsonValue, tracked

LOGGER = logging.getLogger(__name__)
Counts = Mapping[str, Any]
PublisherFactory = Callable[[], Any]


class Operations(Protocol):
    """Everything :class:`~aevorex.scheduler.jobs.JobRunner` needs from the engine."""

    async def check(self, slug: str, trigger: str) -> Counts: ...
    async def recover_queue(self) -> int: ...
    async def due_refresh_slugs(self) -> dict[str, int]: ...
    async def refresh_pending(self, slug: str, trigger: str) -> Counts: ...
    async def refresh_city(self, slug: str, trigger: str) -> Counts: ...
    async def market_stats(self, trigger: str) -> Counts: ...
    async def analyze(self, trigger: str) -> Counts: ...
    async def publish_full(self, slug: str, trigger: str, cadence_minutes: int) -> Counts: ...
    async def publish_freshness(self, slug: str) -> bool: ...
    async def mark_status(
        self, slug: str, status: str, next_check_at: datetime | None
    ) -> bool: ...
    async def set_next_check(self, slug: str, next_check_at: datetime) -> None: ...
    async def set_local_status(self, slug: str, status: str) -> None: ...
    async def freshness(self, slug: str) -> tuple[datetime | None, str | None]: ...
    async def last_market_stats_at(self) -> datetime | None: ...
    async def heartbeat(self, note: str) -> None: ...


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


class EngineOperations:
    """Production implementation."""

    def __init__(self, publisher_factory: PublisherFactory | None = None) -> None:
        self._publisher_factory = publisher_factory
        self._publisher: Any = None

    # --- publisher -------------------------------------------------------------------
    def _get_publisher(self) -> Any:
        if self._publisher is None:
            if self._publisher_factory is not None:
                self._publisher = self._publisher_factory()
            else:
                from aevorex.publisher import Publisher

                self._publisher = Publisher()
        return self._publisher

    async def _reset_publisher(self) -> None:
        """Drop the cached remote engine after a failure so the next call reconnects."""
        publisher, self._publisher = self._publisher, None
        if publisher is not None:
            try:
                await publisher.close()
            except Exception as exc:
                LOGGER.warning("Publisher close failed: error_class=%s", type(exc).__name__)

    async def close(self) -> None:
        await self._reset_publisher()

    async def publish_full(self, slug: str, trigger: str, cadence_minutes: int) -> Counts:
        async def operation() -> dict[str, JsonValue]:
            try:
                result = await self._get_publisher().push(
                    slug, trigger=trigger, cadence_minutes=cadence_minutes
                )
            except BaseException:
                await self._reset_publisher()
                raise
            counts: dict[str, JsonValue] = dict(result.telemetry())
            counts["partial"] = result.status == "partial"
            return counts

        return await tracked("publish", {"city": slug, "trigger": trigger}, operation)

    async def publish_freshness(self, slug: str) -> bool:
        try:
            return bool(await self._get_publisher().push_freshness(slug))
        except BaseException:
            await self._reset_publisher()
            raise

    async def mark_status(
        self, slug: str, status: str, next_check_at: datetime | None
    ) -> bool:
        try:
            return bool(
                await self._get_publisher().mark_status(slug, status, next_check_at=next_check_at)
            )
        except BaseException:
            await self._reset_publisher()
            raise

    async def heartbeat(self, note: str) -> None:
        try:
            await self._get_publisher().heartbeat(note)
        except BaseException:
            await self._reset_publisher()
            raise

    # --- freshness / analysis --------------------------------------------------------
    async def check(self, slug: str, trigger: str) -> Counts:
        from aevorex.freshness.check import run_check

        return await tracked("check", {"city": slug, "trigger": trigger}, lambda: run_check(slug))

    async def recover_queue(self) -> int:
        """Return orphaned ``processing`` detail rows to ``pending``.

        Only called while holding the refresh lane, i.e. when nothing can legitimately be
        mid-flight: a killed service or run-now would otherwise strand its claimed rows.
        """
        async with async_session_maker() as session:
            result = await session.execute(
                update(PendingRefresh)
                .where(PendingRefresh.status == "processing")
                .values(status="pending", next_attempt_at=utc_now())
            )
            await session.commit()
            return int(getattr(result, "rowcount", 0) or 0)

    async def due_refresh_slugs(self) -> dict[str, int]:
        """Markets with detail rows waiting (due, or orphaned in ``processing``) and counts."""
        now = utc_now()
        async with async_session_maker() as session:
            rows = (
                await session.execute(
                    select(PendingRefresh.market_slug, func.count())
                    .where(
                        or_(
                            PendingRefresh.status == "processing",
                            and_(
                                PendingRefresh.status.in_(("pending", "failed")),
                                PendingRefresh.next_attempt_at <= now,
                            ),
                        )
                    )
                    .group_by(PendingRefresh.market_slug)
                )
            ).all()
        return {str(slug): int(count) for slug, count in rows}

    async def refresh_pending(self, slug: str, trigger: str) -> Counts:
        from aevorex.freshness.refresh import run_refresh

        scope: dict[str, JsonValue] = {"city": slug, "trigger": trigger, "pending": True}
        return await tracked("refresh", scope, lambda: run_refresh(pending_only=True))

    async def refresh_city(self, slug: str, trigger: str) -> Counts:
        from aevorex.freshness.refresh import run_refresh

        scope: dict[str, JsonValue] = {"city": slug, "trigger": trigger, "pending": False}
        return await tracked("refresh", scope, lambda: run_refresh(market_slug=slug))

    async def market_stats(self, trigger: str) -> Counts:
        from aevorex.market.stats import build_market_stats

        async def operation() -> dict[str, Any]:
            async with async_session_maker() as session:
                return dict(await build_market_stats(session))

        return await tracked("market_stats", {"trigger": trigger}, operation)

    async def analyze(self, trigger: str) -> Counts:
        from aevorex.freshness.analyze import run_changed_analysis

        scope: dict[str, JsonValue] = {"trigger": trigger, "changed": True}
        return await tracked("analyze", scope, lambda: run_changed_analysis(nightly=False))

    # --- local freshness state -------------------------------------------------------
    async def set_next_check(self, slug: str, next_check_at: datetime) -> None:
        async with async_session_maker() as session:
            await session.execute(
                update(MarketFreshness)
                .where(MarketFreshness.slug == slug)
                .values(next_check_at=_naive_utc(next_check_at))
            )
            await session.commit()

    async def freshness(self, slug: str) -> tuple[datetime | None, str | None]:
        async with async_session_maker() as session:
            row = await session.get(MarketFreshness, slug)
            if row is None:
                return None, None
            item: Any = row
            return item.last_checked_at, str(item.check_status)

    async def set_local_status(self, slug: str, status: str) -> None:
        async with async_session_maker() as session:
            await session.execute(
                update(MarketFreshness)
                .where(MarketFreshness.slug == slug)
                .values(check_status=status)
            )
            await session.commit()

    async def last_market_stats_at(self) -> datetime | None:
        async with async_session_maker() as session:
            value = await session.scalar(
                select(Run.finished_at)
                .where(
                    Run.kind.in_(("market_stats", "market-stats")),
                    Run.status == "succeeded",
                )
                .order_by(Run.finished_at.desc())
                .limit(1)
            )
        return value


async def sweep_orphaned_runs(*, max_age: timedelta, current_pid: int | None = None) -> int:
    """Fail ledger rows left ``running`` by a killed process.

    Called at service start while holding the single-instance lock, so any ``scheduler``
    row except this process's own (matched by the ``pid`` recorded in its scope) is from a
    dead predecessor; other kinds are failed only once older than ``max_age`` (a concurrent
    ``run-now`` may legitimately still be working).
    """
    now = utc_now()
    count = 0
    async with async_session_maker() as session:
        rows = (
            await session.execute(select(Run).where(Run.status == "running"))
        ).scalars().all()
        for row in rows:
            item: Any = row
            own = current_pid is not None and (item.scope or {}).get("pid") == current_pid
            stale = (item.kind == "scheduler" and not own) or (
                item.kind != "scheduler" and (now - item.started_at) > max_age
            )
            if not stale:
                continue
            item.status = "failed"
            item.finished_at = now
            item.error_class = "ServiceInterrupted"
            item.duration_s = max(0.0, (now - item.started_at).total_seconds())
            count += 1
        await session.commit()
    return count


AsyncOp = Callable[[], Awaitable[Counts]]
