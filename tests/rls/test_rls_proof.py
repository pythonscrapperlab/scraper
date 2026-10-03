"""RLS proof (AGENTS.md 6.3) by role switching inside rolled-back transactions.

This exercises the real policies and grants from ``supabase/migrations`` on the disposable
local database. ``tests/rls/test_live_supabase.py`` repeats the key assertions against the real
project through the Supabase admin API when ``SUPABASE_SERVICE_ROLE_KEY`` is configured.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import asyncpg
import pytest

from tests.alerts.seed import World, drop_world, seed_world
from tests.conftest import LocalTestDatabase

pytestmark = pytest.mark.database

Body = Callable[[asyncpg.Connection, World], Awaitable[None]]


@asynccontextmanager
async def as_role(
    connection: asyncpg.Connection, role: str, user: UUID | None = None
) -> AsyncIterator[asyncpg.Connection]:
    """Run statements as a Supabase role; always rolled back."""
    tx = connection.transaction()
    await tx.start()
    try:
        await connection.execute(f"set local role {role}")
        if user is not None:
            await connection.execute(
                "select set_config('request.jwt.claim.sub', $1, true)", str(user)
            )
        yield connection
    finally:
        await tx.rollback()


async def _with_world(db: LocalTestDatabase, body: Body) -> None:
    connection = await db.connect()
    world = await seed_world(connection)
    try:
        await body(connection, world)
    finally:
        await drop_world(connection, world)
        await connection.close()


def run(db: LocalTestDatabase, body: Body) -> None:
    asyncio.run(_with_world(db, body))


async def denied(connection: asyncpg.Connection, sql: str, *args: Any) -> bool:
    """True if the statement fails with insufficient_privilege (inside a savepoint)."""
    savepoint = connection.transaction()
    await savepoint.start()
    try:
        await connection.fetch(sql, *args)
    except asyncpg.InsufficientPrivilegeError:
        await savepoint.rollback()
        return True
    await savepoint.rollback()
    return False


def test_anon_reads_only_snapshots_and_public_market_columns(
    aevorex_test_db: LocalTestDatabase,
) -> None:
    async def body(connection: asyncpg.Connection, world: World) -> None:
        async with as_role(connection, "anon") as c:
            snaps = await c.fetch(
                "select market_slug from serving.demo_snapshots where market_slug = any($1)",
                [world.orlando, world.tampa],
            )
            assert {r["market_slug"] for r in snaps} == {world.orlando, world.tampa}
            markets = await c.fetch(
                "select slug from serving.v_market_public where slug = any($1)",
                [world.orlando, world.tampa],
            )
            assert {r["slug"] for r in markets} == {world.orlando, world.tampa}
            # Non-public market columns are not readable, directly or through SELECT *.
            assert await denied(c, "select region_id from serving.markets")
            assert await denied(c, "select source_status from serving.markets")
            assert await denied(c, "select * from serving.markets")
            for table in ("properties", "scores", "valuation", "agents", "images", "comps",
                          "history", "features", "neighbourhood", "tax_history",
                          "change_events", "runs", "market_daily", "heartbeats"):
                assert await denied(c, f"select 1 from serving.{table}"), table
            for view in ("v_shortlist", "v_property", "v_agent"):
                assert await denied(c, f"select 1 from serving.{view}"), view
            for table in ("orgs", "org_members", "org_markets", "thresholds",
                          "notification_settings", "alert_events", "email_queue"):
                assert await denied(c, f"select 1 from app.{table}"), table

    run(aevorex_test_db, body)


def test_starter_cannot_read_agents_and_sees_only_its_markets(
    aevorex_test_db: LocalTestDatabase,
) -> None:
    async def body(connection: asyncpg.Connection, world: World) -> None:
        async with as_role(connection, "authenticated", world.starter_user) as c:
            assert await c.fetchval(
                "select count(*) from serving.agents where property_id = any($1)",
                list(world.orlando_props),
            ) == 0
            assert await c.fetchval(
                "select count(*) from serving.v_agent where market_slug = $1", world.orlando
            ) == 0
            # Same market, everything else the Starter plan includes is readable.
            assert await c.fetchval(
                "select count(*) from serving.properties where market_id = $1", world.orlando_id
            ) == 2
            assert await c.fetchval(
                "select count(*) from serving.v_shortlist where market_slug = $1", world.orlando
            ) == 2
            assert await c.fetchval(
                "select count(*) from serving.scores where property_id = any($1)",
                list(world.orlando_props),
            ) == 2

    run(aevorex_test_db, body)


def test_pro_reads_agents_for_its_own_market_only(aevorex_test_db: LocalTestDatabase) -> None:
    async def body(connection: asyncpg.Connection, world: World) -> None:
        async with as_role(connection, "authenticated", world.pro_user) as c:
            rows = await c.fetch(
                "select property_id, listing_agent from serving.v_agent "
                "where market_slug = any($1)",
                [world.orlando, world.tampa],
            )
            assert {r["property_id"] for r in rows} == set(world.tampa_props)
            assert await c.fetchval(
                "select count(*) from serving.agents where property_id = any($1)",
                list(world.orlando_props),
            ) == 0

    run(aevorex_test_db, body)


def test_users_cannot_read_other_orgs_markets(aevorex_test_db: LocalTestDatabase) -> None:
    async def body(connection: asyncpg.Connection, world: World) -> None:
        async with as_role(connection, "authenticated", world.starter_user) as c:
            slugs = {r["slug"] for r in await c.fetch(
                "select slug from serving.markets where slug = any($1)",
                [world.orlando, world.tampa])}
            assert slugs == {world.orlando}
            for table in ("properties", "change_events", "market_daily", "runs"):
                assert await c.fetchval(
                    f"select count(*) from serving.{table} where market_id = $1", world.tampa_id
                ) == 0, table
            assert await c.fetchval(
                "select count(*) from serving.scores where property_id = any($1)",
                list(world.tampa_props),
            ) == 0
            assert await c.fetchval(
                "select count(*) from serving.v_property where market_slug = $1", world.tampa
            ) == 0
            # app.* is org-scoped as well.
            assert await c.fetchval(
                "select count(*) from app.alert_events where org_id = $1", world.pro_org
            ) == 0
            assert await c.fetchval(
                "select count(*) from app.orgs where id = $1", world.pro_org
            ) == 0
            assert await denied(c, "select 1 from app.email_queue")

    run(aevorex_test_db, body)


def test_disabled_org_market_hides_data(aevorex_test_db: LocalTestDatabase) -> None:
    async def body(connection: asyncpg.Connection, world: World) -> None:
        await connection.execute(
            "update app.org_markets set enabled = false where org_id = $1", world.starter_org
        )
        async with as_role(connection, "authenticated", world.starter_user) as c:
            assert await c.fetchval(
                "select count(*) from serving.properties where market_id = $1", world.orlando_id
            ) == 0

    run(aevorex_test_db, body)


def test_only_service_role_writes_serving(aevorex_test_db: LocalTestDatabase) -> None:
    async def body(connection: asyncpg.Connection, world: World) -> None:
        insert = "insert into serving.heartbeats (at, note) values (now(), 'rls-proof') returning id"
        for role, user in (("anon", None), ("authenticated", world.pro_user)):
            async with as_role(connection, role, user) as c:
                assert await denied(c, insert), role
                assert await denied(c, "delete from serving.properties"), role
                assert await denied(c, "update serving.scores set score = 99"), role
                assert await denied(
                    c, "insert into app.email_queue (org_id, template) values ($1,'x')",
                    world.pro_org,
                ), role
        async with as_role(connection, "service_role") as c:
            assert await c.fetchval(insert) is not None
            assert await c.fetchval("select count(*) from serving.scores") >= 4
            assert await c.fetchval(
                "select count(*) from app.email_queue where org_id = $1", world.pro_org
            ) == 1

    run(aevorex_test_db, body)


def test_member_of_starter_and_pro_orgs_gets_agents_only_where_pro(
    aevorex_test_db: LocalTestDatabase,
) -> None:
    """Plan gating is per subscribed market, not per user: a dual-org user is not upgraded."""

    async def body(connection: asyncpg.Connection, world: World) -> None:
        await connection.execute(
            "insert into app.org_members (org_id, user_id, role) values ($1,$2,'member')",
            world.starter_org, world.pro_user,
        )
        await connection.execute(
            "insert into app.org_markets (org_id, market_slug) values ($1,$2)",
            world.pro_org, world.orlando,
        )
        async with as_role(connection, "authenticated", world.pro_user) as c:
            visible = {r["slug"] for r in await c.fetch(
                "select slug from serving.markets where slug = any($1)",
                [world.orlando, world.tampa])}
            assert visible == {world.orlando, world.tampa}
            agents = {r["market_slug"] for r in await c.fetch(
                "select market_slug from serving.v_agent where market_slug = any($1)",
                [world.orlando, world.tampa])}
            # The Pro org subscribes to both markets here, so both expose agents.
            assert agents == {world.orlando, world.tampa}
        await connection.execute(
            "delete from app.org_markets where org_id = $1 and market_slug = $2",
            world.pro_org, world.orlando,
        )
        async with as_role(connection, "authenticated", world.pro_user) as c:
            agents = {r["market_slug"] for r in await c.fetch(
                "select market_slug from serving.v_agent where market_slug = any($1)",
                [world.orlando, world.tampa])}
            # Orlando is now reachable only through the Starter org: no agents there.
            assert agents == {world.tampa}
            assert await c.fetchval(
                "select count(*) from serving.properties where market_id = $1", world.orlando_id
            ) == 2

    run(aevorex_test_db, body)
