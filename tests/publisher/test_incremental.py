"""Step 4: the publisher sends only what changed, only when it changed."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text, update

from aevorex.db.models import ChangeEvent, MarketFreshness, Property, utc_now
from tests.publisher.conftest import SLUG, Rig, seed_market

pytestmark = pytest.mark.database

# The only remote writes a no-change push may make: the freshness columns and a heartbeat.
FRESHNESS_ONLY = {("update", "serving.markets"), ("insert", "serving.heartbeats")}


async def _count(rig: Rig, sql: str) -> int:
    async with rig.engine.connect() as connection:
        return int(await connection.scalar(text(sql)) or 0)


@pytest.mark.asyncio
async def test_unchanged_pushes_write_only_freshness_and_heartbeat(rig: Rig) -> None:
    await seed_market(rig)
    publisher = rig.publisher()

    first = await publisher.push(SLUG)
    assert first.counts["properties"] == 9
    assert first.counts["scores"] == 45  # nine properties x five lenses
    assert first.rows_written > 0

    rig.clear()
    second = await publisher.push(SLUG)
    rig.clear()
    third = await publisher.push(SLUG)

    for result in (second, third):
        assert result.rows_written == 0
        assert result.counts["unchanged_properties"] == 9
        assert result.counts["properties"] == 0
        assert result.counts["scores"] == 0
        assert result.counts["change_events"] == 0
    assert rig.write_tables() <= FRESHNESS_ONLY
    assert await _count(rig, "select count(*) from serving.properties") == 9


@pytest.mark.asyncio
async def test_only_the_changed_property_is_rewritten(rig: Rig) -> None:
    ids = await seed_market(rig)
    publisher = rig.publisher()
    await publisher.push(SLUG)

    async with rig.sessions() as session:
        await session.execute(update(Property).where(Property.id == ids[0]).values(price=1_234_567))
        await session.commit()
    rig.clear()
    result = await publisher.push(SLUG)

    assert result.counts["properties"] == 1
    assert result.counts["scores"] == 0
    assert result.counts["unchanged_properties"] == 8
    assert ("insert", "serving.scores") not in rig.write_tables()
    assert ("delete", "serving.images") not in rig.write_tables()
    assert await _count(
        rig, f"select count(*) from serving.properties where price = 1234567 and id = '{ids[0]}'"
    ) == 1


@pytest.mark.asyncio
async def test_volatile_stamps_alone_do_not_trigger_a_write(rig: Rig) -> None:
    ids = await seed_market(rig)
    publisher = rig.publisher()
    await publisher.push(SLUG)

    async with rig.sessions() as session:  # what a nightly refresh does to a quiet listing
        await session.execute(
            update(Property).where(Property.id.in_(ids)).values(
                last_seen_at=utc_now(), refreshed_at=utc_now()
            )
        )
        # A days-on-market tick on a listing outside the demo's top eight (the demo
        # snapshot legitimately shows dom for those, so it is excluded here).
        await session.execute(update(Property).where(Property.id == ids[8]).values(days_on_market=99))
        await session.commit()
    rig.clear()
    result = await publisher.push(SLUG)
    assert result.rows_written == 0
    assert rig.write_tables() <= FRESHNESS_ONLY


@pytest.mark.asyncio
async def test_missing_remote_rows_are_restored_and_departures_removed(rig: Rig) -> None:
    ids = await seed_market(rig)
    publisher = rig.publisher()
    await publisher.push(SLUG)

    async with rig.engine.begin() as connection:  # someone deleted a row from the cache
        await connection.execute(text("delete from serving.properties where id = :id"), {"id": ids[1]})
    async with rig.sessions() as session:  # and a listing left the keep-set locally
        await session.execute(
            text("update listing_presence set is_delisted = true where property_id = :id"),
            {"id": ids[2]},
        )
        await session.execute(
            update(Property).where(Property.id == ids[2]).values(
                delisted_at=utc_now() - timedelta(days=30)
            )
        )
        await session.commit()
    result = await publisher.push(SLUG)

    assert result.counts["properties"] == 1  # restored
    assert result.counts["pruned"] == 1  # departed
    assert await _count(rig, "select count(*) from serving.properties") == 8
    assert await _count(
        rig, f"select count(*) from serving.properties where id = '{ids[1]}'"
    ) == 1
    assert await _count(
        rig, f"select count(*) from serving.properties where id = '{ids[2]}'"
    ) == 0


@pytest.mark.asyncio
async def test_change_events_use_the_push_watermark(rig: Rig) -> None:
    ids = await seed_market(rig)
    publisher = rig.publisher()
    await publisher.push(SLUG)  # watermark is now set

    def event(observed_at, kind="price_cut"):  # type: ignore[no-untyped-def]
        return ChangeEvent(
            property_id=ids[0], market_slug=SLUG, redfin_id="e6-0", kind=kind,
            detail={}, observed_at=observed_at,
        )

    async with rig.sessions() as session:
        session.add_all([event(utc_now() - timedelta(hours=3)), event(utc_now(), "status")])
        await session.commit()
    result = await publisher.push(SLUG)

    assert result.counts["change_events"] == 1
    assert await _count(rig, "select count(*) from serving.change_events") == 1
    assert await _count(rig, "select count(*) from serving.change_events where kind = 'status'") == 1

    rig.clear()
    again = await publisher.push(SLUG)
    # Re-read events inside the safety overlap are ON CONFLICT DO NOTHING: no tuple changes.
    assert again.counts["change_events"] == 0
    assert again.rows_written == 0
    assert await _count(rig, "select count(*) from serving.change_events") == 1


@pytest.mark.asyncio
async def test_append_only_tables_are_pruned_remotely(rig: Rig) -> None:
    await seed_market(rig)
    publisher = rig.publisher()
    await publisher.push(SLUG)
    async with rig.engine.begin() as connection:
        await connection.execute(text(
            "insert into serving.heartbeats (at, note) values "
            "(now() - interval '8 days', 'old'), (now() - interval '6 days', 'recent')"
        ))
        await connection.execute(text(
            "insert into serving.market_daily (market_id, day, listings_active, tier_counts) "
            "select id, current_date - 93, 1, '{}'::jsonb from serving.markets where slug = :s "
            "union all select id, current_date - 85, 1, '{}'::jsonb from serving.markets where slug = :s"
        ), {"s": SLUG})
        await connection.execute(text(
            "insert into serving.runs (id, market_id, kind, trigger, started_at, status, counts) "
            "select gen_random_uuid(), id, 'publisher', 'test', now() - interval '31 days', "
            "'succeeded', '{}'::jsonb from serving.markets where slug = :s "
            "union all select gen_random_uuid(), id, 'publisher', 'test', now() - interval '29 days', "
            "'succeeded', '{}'::jsonb from serving.markets where slug = :s"
        ), {"s": SLUG})
    await publisher.push(SLUG)

    assert await _count(rig, "select count(*) from serving.heartbeats where note in ('old')") == 0
    assert await _count(rig, "select count(*) from serving.heartbeats where note = 'recent'") == 1
    assert await _count(rig, "select count(*) from serving.market_daily where day < current_date - 92") == 0
    assert await _count(rig, "select count(*) from serving.market_daily where day = current_date - 85") == 1
    assert await _count(rig, "select count(*) from serving.runs where started_at < now() - interval '30 days'") == 0
    assert await _count(rig, "select count(*) from serving.runs where started_at < now() - interval '28 days'") >= 1


@pytest.mark.asyncio
async def test_freshness_still_advances_on_a_no_change_push(rig: Rig) -> None:
    await seed_market(rig)
    publisher = rig.publisher()
    await publisher.push(SLUG)
    later = utc_now() + timedelta(minutes=5)
    async with rig.sessions() as session:
        row = await session.get(MarketFreshness, SLUG)
        assert row is not None
        row.last_checked_at = later
        await session.commit()
    await publisher.push(SLUG)
    assert await _count(
        rig, "select count(*) from serving.markets where slug = 'orlando-fl' "
        f"and last_checked_at >= '{later.isoformat()}+00'"
    ) == 1
