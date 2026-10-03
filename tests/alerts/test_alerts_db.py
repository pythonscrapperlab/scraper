"""Database-backed alert enqueue and morning-brief sweep against the disposable test DB."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from aevorex.alerts.alerts import Candidate, enqueue_threshold_alerts, market_score_states
from aevorex.alerts.brief import run_brief_sweep
from aevorex.alerts.rules import ScoreState
from tests.alerts.seed import World, drop_world, seed_world
from tests.conftest import LocalTestDatabase

pytestmark = pytest.mark.database

Body = Callable[[AsyncEngine, World], Awaitable[None]]


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def run(db: LocalTestDatabase, body: Body) -> None:
    async def go() -> None:
        engine = create_async_engine(URL.create(
            "postgresql+asyncpg", username=db.user, password=db.password, host=db.host,
            port=db.port, database=db.database,
        ), connect_args={"statement_cache_size": 0})
        connection = await db.connect()
        world = await seed_world(connection)
        try:
            await body(engine, world)
        finally:
            await engine.dispose()
            await drop_world(connection, world)
            await connection.close()

    asyncio.run(go())


def state(percentile: float) -> ScoreState:
    return ScoreState(
        percentile, "top" if percentile >= 95 else "strong" if percentile >= 80 else "rest"
    )


def candidate(
    world: World, slug_props: tuple[object, ...], index: int, old: float | None, new: float
) -> Candidate:
    return Candidate(
        property_id=str(slug_props[index]), lens="motivated_seller",
        new=state(new), old=None if old is None else state(old),
        score=70.0, grade="B", computed_at=utc(2026, 10, 3, 17, 0),
        address=f"{index} Seed Street", price=300_000,
    )


async def counts(engine: AsyncEngine, world: World) -> tuple[int, int]:
    async with engine.connect() as c:
        events = await c.scalar(text(
            "select count(*) from app.alert_events where org_id = :o and dedupe_key is not null"
        ), {"o": world.starter_org})
        mails = await c.scalar(text(
            "select count(*) from app.email_queue where org_id = :o and template = 'threshold_alert'"
        ), {"o": world.starter_org})
    return int(events or 0), int(mails or 0)


def test_alert_enqueue_is_atomic_idempotent_and_baseline_silent(
    aevorex_test_db: LocalTestDatabase,
) -> None:
    async def body(engine: AsyncEngine, world: World) -> None:
        async with engine.begin() as c:
            await c.execute(text(
                "insert into app.thresholds (org_id, lens, tier) values (:o, 'motivated_seller', 'top')"
            ), {"o": world.starter_org})
        now = utc(2026, 10, 3, 18, 0)  # 14:00 New York
        cands = [candidate(world, world.orlando_props, 0, 90, 96),
                 candidate(world, world.orlando_props, 1, 96, 97)]  # second was already top

        async with engine.begin() as c:
            baseline = await enqueue_threshold_alerts(
                c, world.orlando, cands, now, baseline=True
            )
        assert baseline.events == 0 and baseline.baseline_suppressed == 2
        assert await counts(engine, world) == (0, 0)

        async with engine.begin() as c:
            first = await enqueue_threshold_alerts(c, world.orlando, cands, now, baseline=False)
        assert (first.events, first.emails) == (1, 1)
        assert await counts(engine, world) == (1, 1)

        async with engine.begin() as c:  # identical push: nothing new
            again = await enqueue_threshold_alerts(c, world.orlando, cands, now, baseline=False)
        assert (again.events, again.emails) == (0, 0)
        assert await counts(engine, world) == (1, 1)

        async with engine.connect() as c:
            row = (await c.execute(text(
                "select payload, send_after from app.email_queue "
                "where org_id = :o and template = 'threshold_alert'"
            ), {"o": world.starter_org})).one()
        assert row.payload["total"] == 1
        assert row.send_after == now  # 14:00 local is outside quiet hours

    run(aevorex_test_db, body)


def test_alert_email_is_deferred_through_quiet_hours(aevorex_test_db: LocalTestDatabase) -> None:
    async def body(engine: AsyncEngine, world: World) -> None:
        async with engine.begin() as c:
            await c.execute(text(
                "insert into app.thresholds (org_id, lens, min_percentile) "
                "values (:o, 'motivated_seller', 80)"
            ), {"o": world.starter_org})
        night = utc(2026, 10, 4, 3, 0)  # 23:00 New York
        async with engine.begin() as c:
            await enqueue_threshold_alerts(
                c, world.orlando, [candidate(world, world.orlando_props, 0, 50, 85)], night,
                baseline=False,
            )
        async with engine.connect() as c:
            release = await c.scalar(text(
                "select send_after from app.email_queue "
                "where org_id = :o and template = 'threshold_alert'"
            ), {"o": world.starter_org})
        assert release == utc(2026, 10, 4, 11, 0)  # 07:00 New York

    run(aevorex_test_db, body)


def test_disabled_notification_channels_are_honoured(aevorex_test_db: LocalTestDatabase) -> None:
    async def body(engine: AsyncEngine, world: World) -> None:
        async with engine.begin() as c:
            await c.execute(text(
                "insert into app.thresholds (org_id, lens, tier) values (:o, 'motivated_seller', 'strong')"
            ), {"o": world.starter_org})
            await c.execute(text(
                "insert into app.notification_settings (org_id, user_id, email, in_app) "
                "values (:o, :u, false, true)"
            ), {"o": world.starter_org, "u": world.starter_user})
        async with engine.begin() as c:
            result = await enqueue_threshold_alerts(
                c, world.orlando, [candidate(world, world.orlando_props, 0, 50, 85)],
                utc(2026, 10, 3, 18, 0), baseline=False,
            )
        assert (result.events, result.emails) == (1, 0)

    run(aevorex_test_db, body)


def test_market_score_states_reads_the_cache(aevorex_test_db: LocalTestDatabase) -> None:
    async def body(engine: AsyncEngine, world: World) -> None:
        async with engine.connect() as c:
            states = await market_score_states(c, world.orlando_id)
        assert set(states) == {(str(p), "motivated_seller") for p in world.orlando_props}
        assert all(s.tier == "strong" for s in states.values())

    run(aevorex_test_db, body)


def test_brief_sweep_window_idempotency_and_payload(aevorex_test_db: LocalTestDatabase) -> None:
    async def body(engine: AsyncEngine, world: World) -> None:
        async with engine.begin() as c:
            await c.execute(text(
                "insert into serving.change_events (id, property_id, market_id, kind, detail, "
                "observed_at) values (gen_random_uuid(), :p, :m, 'price_cut', '{}'::jsonb, :t)"
            ), {"p": world.orlando_props[0], "m": world.orlando_id, "t": utc(2026, 10, 3, 6, 0)})

        early = await run_brief_sweep(engine, utc(2026, 10, 3, 10, 0))  # 06:00 New York
        assert early.enqueued == 0 and early.not_due >= 2

        now = utc(2026, 10, 3, 11, 20)  # 07:20 New York: the laptop was a little late
        sweep = await run_brief_sweep(engine, now, counts_by_plan={"starter": 1})
        assert sweep.enqueued == 2 and sweep.failed == 0
        again = await run_brief_sweep(engine, now, counts_by_plan={"starter": 1})
        assert again.enqueued == 0 and again.already_queued == 2

        late = await run_brief_sweep(engine, utc(2026, 10, 3, 16, 0))  # noon: too late
        assert late.too_late >= 2

        async with engine.connect() as c:
            rows = (await c.execute(text(
                "select org_id, payload, send_after, dedupe_key from app.email_queue "
                "where template = 'morning_brief' and dedupe_key is not null "
                "and org_id = any(:orgs)"
            ), {"orgs": [world.starter_org, world.pro_org]})).all()
        by_org = {r.org_id: r for r in rows}
        starter = by_org[world.starter_org].payload
        assert starter["lens"] == "motivated_seller" and len(starter["top"]) == 1  # plan N override
        assert starter["top"][0]["percentile"] == 85.0
        assert starter["changes_24h"]["counts"] == {"price_cut": 1}
        assert starter["freshness"]["check_status"] == "ok" and starter["freshness"]["note"] is None
        assert len(by_org[world.pro_org].payload["top"]) == 2  # pro N=10, only two listings
        assert by_org[world.starter_org].send_after == now
        assert world.orlando in by_org[world.starter_org].dedupe_key

    run(aevorex_test_db, body)
