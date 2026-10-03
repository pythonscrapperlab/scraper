"""Seed a small two-org world into the (disposable) aevorex_test database."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID, uuid4

import asyncpg

LENSES = ("motivated_seller", "fix_flip", "buy_hold", "str", "airbnb")


@dataclass(frozen=True)
class World:
    suffix: str
    orlando: str
    tampa: str
    orlando_id: UUID
    tampa_id: UUID
    starter_org: UUID
    pro_org: UUID
    starter_user: UUID
    pro_user: UUID
    orlando_props: tuple[UUID, ...]
    tampa_props: tuple[UUID, ...]


async def seed_world(connection: asyncpg.Connection) -> World:
    suffix = uuid4().hex[:8]
    world = World(
        suffix=suffix,
        orlando=f"orlando-{suffix}", tampa=f"tampa-{suffix}",
        orlando_id=uuid4(), tampa_id=uuid4(),
        starter_org=uuid4(), pro_org=uuid4(),
        starter_user=uuid4(), pro_user=uuid4(),
        orlando_props=(uuid4(), uuid4()), tampa_props=(uuid4(), uuid4()),
    )
    for market_id, slug, city, demo in (
        (world.orlando_id, world.orlando, "Orlando", True),
        (world.tampa_id, world.tampa, "Tampa", False),
    ):
        await connection.execute(
            "insert into serving.markets (id, city, state, region_id, slug, tz, is_demo, "
            "check_status, listings_active) values ($1,$2,'FL','1',$3,'America/New_York',$4,'ok',2)",
            market_id, city, slug, demo,
        )
        await connection.execute(
            "insert into serving.demo_snapshots (market_slug, lens, payload, generated_at) "
            "values ($1,'motivated_seller','{}'::jsonb, now())", slug,
        )
    for market_id, props in ((world.orlando_id, world.orlando_props),
                             (world.tampa_id, world.tampa_props)):
        for index, property_id in enumerate(props):
            await connection.execute(
                "insert into serving.properties (id, market_id, address, city, state, zip, price, "
                "listing_status_normalized) values ($1,$2,$3,'X','FL','32801',300000,'active')",
                property_id, market_id, f"{index} Seed Street",
            )
            await connection.execute(
                "insert into serving.agents (property_id, listing_agent, listing_broker) "
                "values ($1,'Agent Smith','Smith Realty')", property_id,
            )
            await connection.execute(
                "insert into serving.scores (property_id, lens, score, grade, percentile, "
                "confidence, tier, version, computed_at) values ($1,'motivated_seller',60,'C',"
                "$2,0.8,'strong','v3', now())", property_id, 85.0 - index,
            )
    for user in (world.starter_user, world.pro_user):
        await connection.execute("insert into auth.users (id) values ($1)", user)
    for org, plan, user, slug in (
        (world.starter_org, "starter", world.starter_user, world.orlando),
        (world.pro_org, "pro", world.pro_user, world.tampa),
    ):
        await connection.execute(
            "insert into app.orgs (id, name, plan) values ($1,$2,$3)", org, f"{plan}-{suffix}", plan
        )
        await connection.execute(
            "insert into app.org_members (org_id, user_id, role) values ($1,$2,'owner')", org, user
        )
        await connection.execute(
            "insert into app.org_markets (org_id, market_slug) values ($1,$2)", org, slug
        )
        await connection.execute(
            "insert into app.alert_events (org_id, property_id, market_slug, lens, tier, payload) "
            "values ($1,$2,$3,'motivated_seller','strong',$4::jsonb)",
            org, world.orlando_props[0], slug, json.dumps({}),
        )
        await connection.execute(
            "insert into app.email_queue (org_id, user_id, template) values ($1,$2,'morning_brief')",
            org, user,
        )
    return world


async def drop_world(connection: asyncpg.Connection, world: World) -> None:
    await connection.execute(
        "delete from app.orgs where id = any($1::uuid[])", [world.starter_org, world.pro_org]
    )
    await connection.execute(
        "delete from auth.users where id = any($1::uuid[])", [world.starter_user, world.pro_user]
    )
    await connection.execute(
        "delete from serving.demo_snapshots where market_slug = any($1::text[])",
        [world.orlando, world.tampa],
    )
    await connection.execute(
        "delete from serving.markets where id = any($1::uuid[])", [world.orlando_id, world.tampa_id]
    )
