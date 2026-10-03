"""Shape and behaviour of the read-only web views (serving.v_*)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import asyncpg
import pytest

from tests.alerts.seed import World, drop_world, seed_world
from tests.conftest import LocalTestDatabase

pytestmark = pytest.mark.database


def test_views_expose_wide_scores_latest_change_and_hide_delisted(
    aevorex_test_db: LocalTestDatabase,
) -> None:
    async def go() -> None:
        connection = await aevorex_test_db.connect()
        world: World = await seed_world(connection)
        try:
            first, second = world.orlando_props
            await connection.execute(
                "insert into serving.scores (property_id, lens, score, grade, percentile, "
                "confidence, tier, version, computed_at, breakdown) values "
                "($1,'fix_flip',71,'B',96,0.9,'top','v3', now(), $2::jsonb)",
                first, json.dumps({"components": []}),
            )
            for kind, when in (("new", datetime(2026, 10, 1, tzinfo=UTC)), ("price_cut", datetime(2026, 10, 2, tzinfo=UTC))):
                await connection.execute(
                    "insert into serving.change_events (id, property_id, market_id, kind, detail, "
                    "observed_at) values (gen_random_uuid(), $1, $2, $3, '{}'::jsonb, $4)",
                    first, world.orlando_id, kind, when,
                )
            await connection.execute(
                "update serving.properties set delisted_at = now() where id = $1", second
            )
            rows = await connection.fetch(
                "select * from serving.v_shortlist where market_slug = $1", world.orlando
            )
            assert [r["property_id"] for r in rows] == [first]  # delisted row is gone
            row = rows[0]
            assert row["latest_change_kind"] == "price_cut"
            assert (row["motivated_seller_percentile"], row["motivated_seller_tier"]) == (85, "strong")
            assert (row["fix_flip_grade"], row["fix_flip_tier"]) == ("B", "top")
            assert row["buy_hold_score"] is None  # unscored lens stays null, never invented
            for lens in ("motivated_seller", "fix_flip", "buy_hold", "str", "airbnb"):
                for suffix in ("score", "grade", "percentile", "tier"):
                    assert f"{lens}_{suffix}" in row

            prop = await connection.fetchrow(
                "select * from serving.v_property where property_id = $1", first
            )
            assert prop is not None
            scores = json.loads(prop["scores"])
            assert set(scores) == {"motivated_seller", "fix_flip"}
            assert scores["fix_flip"]["breakdown"] == {"components": []}
            assert prop["market_slug"] == world.orlando and prop["market_tz"]

            agent = await connection.fetchrow(
                "select * from serving.v_agent where property_id = $1", first
            )
            assert agent is not None and agent["listing_broker"] == "Smith Realty"
            public = await connection.fetchrow(
                "select * from serving.v_market_public where slug = $1", world.orlando
            )
            assert public is not None
            assert "region_id" not in public and "source_status" not in public
        finally:
            await drop_world(connection, world)
            await connection.close()

    asyncio.run(go())


def test_views_are_security_invoker(aevorex_test_db: LocalTestDatabase) -> None:
    async def go() -> None:
        connection: asyncpg.Connection = await aevorex_test_db.connect()
        try:
            rows = await connection.fetch(
                "select c.relname, coalesce(c.reloptions::text, '') as options "
                "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                "where n.nspname = 'serving' and c.relkind = 'v'"
            )
            assert {r["relname"] for r in rows} == {
                "v_market_public", "v_shortlist", "v_property", "v_agent"
            }
            assert all("security_invoker=true" in r["options"] for r in rows)
        finally:
            await connection.close()

    asyncio.run(go())
