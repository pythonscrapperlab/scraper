"""Snapshot sanity guard: implausibly small snapshots are rejected, real shrinkage is not."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import URL, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aevorex.db.models import ChangeEvent, ListingPresence, MarketFreshness
from aevorex.freshness.check import SnapshotRejected, apply_snapshot, snapshot_is_plausible
from aevorex.freshness.market import Market
from aevorex.freshness.types import SearchListing
from tests.conftest import LocalTestDatabase


def _listings(count: int) -> list[SearchListing]:
    return [
        SearchListing(
            redfin_id=f"guard-{i}",
            listing_url=f"https://example.invalid/home/{i}",
            price=250_000 + i,
            listing_status="Active",
            dom=3,
            listed_at=datetime(2026, 9, 27),
        )
        for i in range(count)
    ]


def test_ratio_rules() -> None:
    assert snapshot_is_plausible(0, 0, 0.7) == (True, None)  # nothing to compare
    assert snapshot_is_plausible(100, 0, 0.7) == (False, 0.0)  # empty page 1
    assert snapshot_is_plausible(100, 69, 0.7)[0] is False
    assert snapshot_is_plausible(100, 70, 0.7)[0] is True  # boundary is inclusive
    assert snapshot_is_plausible(100, 150, 0.7)[0] is True  # growth is fine
    assert snapshot_is_plausible(100, 50, 0.4)[0] is True  # configurable


async def _seed_and_try(
    db: LocalTestDatabase, slug: str, first: int, second: int
) -> tuple[dict[str, int] | None, MarketFreshness, list[ListingPresence], int]:
    engine = create_async_engine(
        URL.create(
            "postgresql+asyncpg", username=db.user, password=db.password,
            host=db.host, port=db.port, database=db.database,
        )
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    market = Market(slug, "Guard", "FL", "7")
    try:
        async with sessions() as session:
            await apply_snapshot(session, market, _listings(first))
            await session.commit()
            checked_before = (await session.get(MarketFreshness, slug)).last_checked_at
        counts: dict[str, int] | None = None
        async with sessions() as session:
            try:
                counts = await apply_snapshot(session, market, _listings(second))
                await session.commit()
            except SnapshotRejected:
                await session.rollback()
        async with sessions() as session:
            row = await session.get(MarketFreshness, slug)
            presence = list(
                (await session.execute(
                    select(ListingPresence).where(ListingPresence.market_slug == slug)
                )).scalars()
            )
            events = len(
                (await session.execute(
                    select(ChangeEvent).where(ChangeEvent.market_slug == slug)
                )).scalars().all()
            )
        assert row is not None
        row.checked_before = checked_before  # type: ignore[attr-defined]
        return counts, row, presence, events
    finally:
        await engine.dispose()


@pytest.mark.database
@pytest.mark.asyncio
async def test_empty_page_one_is_rejected(aevorex_test_db: LocalTestDatabase) -> None:
    counts, row, presence, _ = await _seed_and_try(aevorex_test_db, "guard-empty-fl", 20, 0)
    assert counts is None
    assert row.last_checked_at == row.checked_before  # type: ignore[attr-defined]
    assert row.listings_active == 20
    assert all(p.absence_count == 0 and not p.is_delisted for p in presence)


@pytest.mark.database
@pytest.mark.asyncio
async def test_short_middle_page_is_rejected(aevorex_test_db: LocalTestDatabase) -> None:
    # 100 listings previously; one 40-home page lost from the middle -> 60%.
    counts, row, presence, _ = await _seed_and_try(aevorex_test_db, "guard-short-fl", 100, 60)
    assert counts is None
    assert row.last_checked_at == row.checked_before  # type: ignore[attr-defined]
    assert row.listings_active == 100
    assert all(p.absence_count == 0 for p in presence)


@pytest.mark.database
@pytest.mark.asyncio
async def test_genuine_twenty_percent_shrink_is_accepted(
    aevorex_test_db: LocalTestDatabase,
) -> None:
    counts, row, presence, _ = await _seed_and_try(aevorex_test_db, "guard-real-fl", 100, 80)
    assert counts is not None
    assert row.listings_active == 80
    assert row.last_checked_at > row.checked_before  # type: ignore[attr-defined]
    assert sum(1 for p in presence if p.absence_count == 1) == 20
