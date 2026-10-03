"""The additive publisher helpers the scheduler relies on (light freshness, status, heartbeat)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import URL, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aevorex.db.models import MarketFreshness
from aevorex.publisher.service import Publisher
from tests.conftest import LocalTestDatabase

SLUG = "orlando-fl"


def _engine(database: LocalTestDatabase):  # type: ignore[no-untyped-def]
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


@pytest.mark.database
@pytest.mark.asyncio
async def test_light_push_status_and_heartbeat(aevorex_test_db: LocalTestDatabase) -> None:
    engine = _engine(aevorex_test_db)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 10, 3, 15, 0, tzinfo=UTC)
    publisher = Publisher(local_sessions=sessions, remote_engine=engine, now=now)
    try:
        async with sessions() as session:
            existing = await session.get(MarketFreshness, SLUG)
            if existing is None:
                session.add(MarketFreshness(slug=SLUG, city="Orlando", state="FL", region_id="13655"))
            await session.flush()
            row = await session.get(MarketFreshness, SLUG)
            assert row is not None
            row.last_checked_at = datetime(2026, 10, 3, 14, 55)
            row.next_check_at = datetime(2026, 10, 3, 17, 14)
            row.check_status = "ok"
            row.listings_active = 1234
            row.changed_last_check = 7
            await session.commit()

        # Market not on the remote yet: the caller must fall back to a full push.
        await publisher.store.initialize()
        async with publisher.store.transaction() as remote:
            markets = publisher.store.table("markets")
            from sqlalchemy import delete

            await remote.execute(delete(markets).where(markets.c.slug == SLUG))
        assert await publisher.push_freshness(SLUG) is False
        assert await publisher.mark_status(SLUG, "late") is False

        market_id = uuid4()
        async with publisher.store.transaction() as remote:
            await publisher.store.upsert(
                remote, "markets",
                [{"id": market_id, "city": "Orlando", "state": "FL", "region_id": "13655",
                  "slug": SLUG, "tz": "America/New_York", "check_status": "warming"}],
                conflict=("slug",),
            )

        assert await publisher.push_freshness(SLUG) is True
        markets = publisher.store.table("markets")
        async with engine.connect() as connection:
            remote_row = (
                await connection.execute(select(markets).where(markets.c.slug == SLUG))
            ).mappings().one()
        assert remote_row["check_status"] == "ok"
        assert remote_row["listings_active"] == 1234
        assert remote_row["changed_last_check"] == 7
        assert remote_row["last_checked_at"] == datetime(2026, 10, 3, 14, 55, tzinfo=UTC)
        assert remote_row["next_check_at"] == datetime(2026, 10, 3, 17, 14, tzinfo=UTC)

        assert await publisher.mark_status(SLUG, "late") is True
        async with engine.connect() as connection:
            status = await connection.scalar(select(markets.c.check_status).where(markets.c.slug == SLUG))
            listings = await connection.scalar(
                select(markets.c.listings_active).where(markets.c.slug == SLUG)
            )
        assert status == "late"
        assert listings == 1234  # mark_status never touches freshness counters

        with pytest.raises(ValueError):
            await publisher.mark_status(SLUG, "healthy")

        beats = publisher.store.table("heartbeats")
        async with engine.connect() as connection:
            before = await connection.scalar(select(func.count()).select_from(beats))
        async with publisher.store.transaction() as remote:
            await remote.execute(
                beats.insert().values(at=datetime(2026, 9, 1, tzinfo=UTC), note="ancient")
            )
        await publisher.heartbeat("scheduler")
        async with engine.connect() as connection:
            after = await connection.scalar(select(func.count()).select_from(beats))
            ancient = await connection.scalar(
                select(func.count()).select_from(beats).where(beats.c.note == "ancient")
            )
        assert after == (before or 0) + 1  # +1 new beat; the 30-day-old row was pruned
        assert ancient == 0
    finally:
        await publisher.close()
