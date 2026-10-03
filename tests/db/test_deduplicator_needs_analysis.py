"""Re-upserting identical data must not re-queue a property for analysis."""

from __future__ import annotations

import copy

import pytest
from sqlalchemy import URL, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aevorex.db.deduplicator import Deduplicator
from aevorex.db.models import Property
from tests.conftest import LocalTestDatabase

RECORD = {
    "redfin_id": "dedup-needs-1",
    "address": "1 Test Way",
    "city": "Orlando",
    "state": "FL",
    "zip_code": "32801",
    "price": 300_000,
    "bedrooms": 3,
    "bathrooms": 2.0,
    "sqft": 1500,
    "listing_status": "Active",
    "meta": {"note": "x"},
}


def _engine(db: LocalTestDatabase):
    return create_async_engine(
        URL.create(
            "postgresql+asyncpg", username=db.user, password=db.password,
            host=db.host, port=db.port, database=db.database,
        )
    )


@pytest.mark.database
@pytest.mark.asyncio
async def test_identical_reupsert_leaves_needs_analysis_false(
    aevorex_test_db: LocalTestDatabase,
) -> None:
    engine = _engine(aevorex_test_db)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as session:
            prop, is_new = await Deduplicator.upsert(session, "redfin", copy.deepcopy(RECORD))
            assert is_new and prop.needs_analysis is True
            await session.execute(
                update(Property).where(Property.id == prop.id).values(needs_analysis=False)
            )
            await session.commit()
            first_seen = prop.last_seen_at

        async with sessions() as session:
            same, is_new = await Deduplicator.upsert(session, "redfin", copy.deepcopy(RECORD))
            await session.commit()
            assert not is_new
            assert same.needs_analysis is False
            # Still stamped: presence is exactly what last_seen_at records.
            assert same.last_seen_at >= first_seen

        async with sessions() as session:
            changed = copy.deepcopy(RECORD)
            changed["price"] = 275_000
            moved, _ = await Deduplicator.upsert(session, "redfin", changed)
            await session.commit()
            assert moved.needs_analysis is True
    finally:
        await engine.dispose()
