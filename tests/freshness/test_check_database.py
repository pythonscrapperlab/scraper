"""Database proofs for idempotent checks and absence confirmation."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import URL, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aevorex.db.models import ChangeEvent, ListingPresence, PendingRefresh
from aevorex.freshness.check import apply_snapshot
from aevorex.freshness.market import Market
from aevorex.freshness.types import SearchListing
from tests.conftest import LocalTestDatabase


def _listing(redfin_id: str = "fresh-1") -> SearchListing:
    return SearchListing(
        redfin_id=redfin_id,
        listing_url=f"https://example.invalid/home/{redfin_id}",
        price=250_000,
        listing_status="Active",
        dom=3,
        listed_at=datetime(2026, 9, 27),
    )


def _session_factory(database: LocalTestDatabase) -> tuple[object, async_sessionmaker]:
    engine = create_async_engine(
        URL.create(
            "postgresql+asyncpg",
            username=database.user,
            password=database.password,
            host=database.host,
            port=database.port,
            database=database.database,
        )
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.mark.database
@pytest.mark.asyncio
async def test_check_is_idempotent(aevorex_test_db: LocalTestDatabase) -> None:
    engine, sessions = _session_factory(aevorex_test_db)
    market = Market("idempotent-fl", "Idempotent", "FL", "1")
    try:
        async with sessions() as session:
            first = await apply_snapshot(session, market, [_listing()])
            await session.commit()
            second = await apply_snapshot(session, market, [_listing()])
            await session.commit()
            events = await session.scalar(
                select(func.count()).select_from(ChangeEvent).where(
                    ChangeEvent.market_slug == market.slug
                )
            )
            queued = await session.scalar(
                select(func.count()).select_from(PendingRefresh).where(
                    PendingRefresh.market_slug == market.slug
                )
            )
        assert first["events"] == 1
        assert second["events"] == 0
        assert events == 1
        assert queued == 1
    finally:
        await engine.dispose()  # type: ignore[attr-defined]


@pytest.mark.database
@pytest.mark.asyncio
async def test_second_absence_confirms_delisted(
    aevorex_test_db: LocalTestDatabase,
) -> None:
    engine, sessions = _session_factory(aevorex_test_db)
    market = Market("absence-fl", "Absence", "FL", "2")
    try:
        async with sessions() as session:
            # Five listings, then one disappears: 80% of the previous snapshot is
            # a plausible shrink (an empty snapshot would be rejected by the guard).
            others = [_listing(f"present-{i}") for i in range(4)]
            await apply_snapshot(session, market, [_listing("absent-1"), *others])
            await session.commit()
            first = await apply_snapshot(session, market, others)
            await session.commit()
            second = await apply_snapshot(session, market, others)
            await session.commit()
            presence = (
                await session.execute(
                    select(ListingPresence).where(
                        ListingPresence.market_slug == market.slug,
                        ListingPresence.redfin_id == "absent-1",
                    )
                )
            ).scalar_one()
        assert first["delisted"] == 0
        assert second["delisted"] == 1
        assert presence.absence_count == 2
        assert presence.is_delisted is True
    finally:
        await engine.dispose()  # type: ignore[attr-defined]
