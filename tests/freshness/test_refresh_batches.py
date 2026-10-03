"""Detail refresh works in durable batches: a stop or crash keeps every finished batch."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import URL, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from aevorex.db.models import MarketFreshness, PendingRefresh, Property, utc_now
from aevorex.freshness import check, refresh
from tests.conftest import LocalTestDatabase

SLUG = "vero-beach-fl"


class FakeScraper:
    fetch_metrics = {"attempts": 5, "blocked": 0, "http_405": 0}


class FakeRunner:
    """Stands in for PipelineRunner: 'refreshes' every URL except those in ``skip``."""

    calls: list[list[str]] = []
    skip: set[str] = set()
    explode_on_call: int | None = None

    def __init__(self, scraper: Any, normalizer: Any, max_concurrent_fetches: int = 3) -> None:
        del scraper, normalizer, max_concurrent_fetches

    async def run_scrape(self, session: Any, state: str, urls: list[str]) -> dict[str, Any]:
        del state
        type(self).calls.append(list(urls))
        if type(self).explode_on_call == len(type(self).calls):
            raise asyncio.CancelledError  # what a service stop looks like mid-batch
        for url in urls:
            if url in type(self).skip:
                continue
            prop = (
                await session.execute(select(Property).where(Property.listing_url == url))
            ).scalars().first()
            prop.refreshed_at = utc_now()
        await session.commit()
        return {"stats": {"errors": 0}}


def _engine(database: LocalTestDatabase) -> AsyncEngine:
    return create_async_engine(
        URL.create(
            "postgresql+asyncpg",
            username=database.user,
            password=database.password,
            host=database.host,
            port=database.port,
            database=database.database,
        )
    )


async def _seed(sessions: async_sessionmaker[Any], count: int) -> list[str]:
    urls = [f"https://example.invalid/vb/{uuid4().hex}" for _ in range(count)]
    async with sessions() as session:
        if await session.get(MarketFreshness, SLUG) is None:
            session.add(MarketFreshness(slug=SLUG, city="Vero Beach", state="FL", region_id="18840"))
            await session.flush()
        for url in urls:
            redfin_id = url.rsplit("/", 1)[1]
            session.add(Property(
                id=uuid4(), primary_source="redfin", address=f"{redfin_id[:5]} Test Rd",
                city="Vero Beach", state="FL", zip_code="32960", redfin_id=redfin_id,
                listing_url=url,
            ))
            session.add(PendingRefresh(
                market_slug=SLUG, redfin_id=redfin_id, listing_url=url, reason="new",
                status="pending", next_attempt_at=utc_now() - timedelta(minutes=1),
            ))
        await session.commit()
    return urls


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> Callable[[async_sessionmaker[Any]], None]:
    FakeRunner.calls = []
    FakeRunner.skip = set()
    FakeRunner.explode_on_call = None

    def apply(sessions: async_sessionmaker[Any]) -> None:
        monkeypatch.setattr(refresh, "async_session_maker", sessions)
        monkeypatch.setattr(check, "async_session_maker", sessions)
        monkeypatch.setattr(refresh, "RedfinScraper", FakeScraper)
        monkeypatch.setattr(refresh, "PipelineRunner", FakeRunner)
        monkeypatch.setattr(refresh.settings, "refresh_batch_size", 2)

    return apply


async def _clear_queue(sessions: async_sessionmaker[Any]) -> None:
    """The test database is shared; isolate the queue before a queue-wide run."""
    async with sessions() as session:
        for stale in (await session.execute(select(PendingRefresh))).scalars():
            await session.delete(stale)
        await session.commit()


async def _statuses(sessions: async_sessionmaker[Any], urls: list[str]) -> dict[str, str]:
    async with sessions() as session:
        rows = (
            await session.execute(select(PendingRefresh).where(PendingRefresh.listing_url.in_(urls)))
        ).scalars().all()
    return {str(row.listing_url): str(row.status) for row in rows}


@pytest.mark.database
@pytest.mark.asyncio
async def test_whole_city_refresh_runs_in_batches_and_stamps_the_market(
    aevorex_test_db: LocalTestDatabase, patched: Callable[[async_sessionmaker[Any]], None]
) -> None:
    engine = _engine(aevorex_test_db)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    patched(sessions)
    try:
        await _clear_queue(sessions)
        urls = await _seed(sessions, 5)
        counts = await refresh.run_refresh(market_slug=SLUG)
        assert [len(call) for call in FakeRunner.calls] == [2, 2, 1]
        assert counts["success"] == 5 and counts["failed"] == 0
        assert set((await _statuses(sessions, urls)).values()) == {"succeeded"}
        async with sessions() as session:
            market = await session.get(MarketFreshness, SLUG)
        assert market is not None and market.last_refreshed_at is not None
    finally:
        await engine.dispose()


@pytest.mark.database
@pytest.mark.asyncio
async def test_a_stop_mid_run_keeps_finished_batches_and_orphans_only_the_rest(
    aevorex_test_db: LocalTestDatabase, patched: Callable[[async_sessionmaker[Any]], None]
) -> None:
    engine = _engine(aevorex_test_db)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    patched(sessions)
    try:
        await _clear_queue(sessions)
        urls = await _seed(sessions, 5)
        FakeRunner.explode_on_call = 2  # stop during the second batch
        with pytest.raises(asyncio.CancelledError):
            await refresh.run_refresh(pending_only=True)
        states = await _statuses(sessions, urls)
        assert sorted(states.values()) == ["processing"] * 3 + ["succeeded"] * 2
        # ...and the scheduler's recovery path returns the orphans to the queue.
        from aevorex.scheduler import ops

        recovered_ops = ops.EngineOperations()
        original = ops.async_session_maker
        ops.async_session_maker = sessions
        try:
            await recovered_ops.recover_queue()
        finally:
            ops.async_session_maker = original
        assert sorted((await _statuses(sessions, urls)).values()) == ["pending"] * 3 + ["succeeded"] * 2
    finally:
        await engine.dispose()


@pytest.mark.database
@pytest.mark.asyncio
async def test_unrefreshed_url_fails_with_backoff_and_does_not_stamp_the_market(
    aevorex_test_db: LocalTestDatabase, patched: Callable[[async_sessionmaker[Any]], None]
) -> None:
    engine = _engine(aevorex_test_db)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    patched(sessions)
    try:
        async with sessions() as session:
            if (market := await session.get(MarketFreshness, SLUG)) is not None:
                market.last_refreshed_at = None
                await session.commit()
        await _clear_queue(sessions)
        urls = await _seed(sessions, 3)
        FakeRunner.skip = {urls[1]}
        counts = await refresh.run_refresh(pending_only=True)
        assert counts["failed"] == 1
        states = await _statuses(sessions, urls)
        assert states[urls[1]] == "failed"
        assert states[urls[0]] == states[urls[2]] == "succeeded"
        async with sessions() as session:
            row = (
                await session.execute(select(PendingRefresh).where(PendingRefresh.listing_url == urls[1]))
            ).scalars().one()
            assert row.attempts == 1 and row.next_attempt_at > utc_now()
    finally:
        await engine.dispose()
