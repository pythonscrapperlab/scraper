"""Database proofs: org-market SQL, advisory locks, queue recovery and ledger sweeping."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import URL, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from aevorex.db.models import MarketFreshness, PendingRefresh, Run, utc_now
from aevorex.scheduler import locks, ops
from aevorex.scheduler.cities import remote_org_market_reader
from tests.conftest import LocalTestDatabase


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


@pytest.mark.database
@pytest.mark.asyncio
async def test_org_market_sql_returns_distinct_enabled_slugs(
    aevorex_test_db: LocalTestDatabase,
) -> None:
    connection = await aevorex_test_db.connect()
    first, second = uuid4(), uuid4()
    try:
        await connection.execute(
            "insert into app.orgs (id, name) values ($1, 'a'), ($2, 'b')", first, second
        )
        await connection.execute(
            "insert into app.org_markets (org_id, market_slug, enabled) values "
            "($1,'orlando-fl',true),($2,'orlando-fl',true),"
            "($1,'miami-fl',false),($2,'Tampa-FL',true)",
            first, second,
        )
        engine = _engine(aevorex_test_db)
        try:
            slugs = await remote_org_market_reader(engine)()
        finally:
            await engine.dispose()
        assert slugs == {"orlando-fl", "tampa-fl"}
    finally:
        await connection.execute("delete from app.orgs where id in ($1,$2)", first, second)
        await connection.close()


@pytest.mark.database
@pytest.mark.asyncio
async def test_advisory_lock_is_exclusive_across_connections_and_releases(
    aevorex_test_db: LocalTestDatabase,
) -> None:
    holder, rival = _engine(aevorex_test_db), _engine(aevorex_test_db)
    try:
        async with locks.advisory_lock(locks.CHECK, wait=False, engine=holder):
            with pytest.raises(locks.LockBusy):
                async with locks.advisory_lock(locks.CHECK, wait=False, engine=rival):
                    pytest.fail("second holder must not enter")
            with pytest.raises(locks.LockBusy):
                async with locks.advisory_lock(
                    locks.CHECK, wait=True, poll_seconds=0.01, timeout_seconds=0.05, engine=rival
                ):
                    pytest.fail("timed wait must give up")
            async with locks.advisory_lock(locks.REFRESH, wait=False, engine=rival):
                pass  # a different lane is independent: 1 check + 1 refresh may overlap
        async with locks.advisory_lock(locks.CHECK, wait=False, engine=rival):
            pass  # released when the holder exited
    finally:
        await holder.dispose()
        await rival.dispose()


@pytest.mark.database
@pytest.mark.asyncio
async def test_advisory_lock_is_released_when_the_body_raises(
    aevorex_test_db: LocalTestDatabase,
) -> None:
    engine = _engine(aevorex_test_db)
    try:
        with pytest.raises(RuntimeError):
            async with locks.advisory_lock(locks.PUBLISH, wait=False, engine=engine):
                raise RuntimeError("boom")
        async with locks.advisory_lock(locks.PUBLISH, wait=False, engine=engine):
            pass
    finally:
        await engine.dispose()


@pytest.mark.database
@pytest.mark.asyncio
async def test_queue_recovery_ledger_sweep_and_local_state(
    aevorex_test_db: LocalTestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(aevorex_test_db)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(ops, "async_session_maker", sessions)
    slug = f"recovery-{uuid4().hex[:6]}-fl"
    now = utc_now()
    stuck, done = uuid4(), uuid4()
    old_check, fresh_check, service_row, ok_row, own_row = (
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    )
    try:
        async with sessions() as session:
            session.add(
                MarketFreshness(slug=slug, city="Recovery", state="FL", region_id="1",
                                last_checked_at=now - timedelta(minutes=3), check_status="ok")
            )
            await session.flush()
            for row_id, status in ((stuck, "processing"), (done, "succeeded")):
                session.add(PendingRefresh(
                    id=row_id, market_slug=slug, redfin_id=str(row_id),
                    listing_url="https://example.invalid/x", reason="new", status=status,
                ))
            session.add_all([
                Run(id=old_check, kind="check", scope={}, status="running",
                    started_at=now - timedelta(hours=6)),
                Run(id=fresh_check, kind="check", scope={}, status="running",
                    started_at=now - timedelta(minutes=1)),
                Run(id=service_row, kind="scheduler", scope={"pid": 1}, status="running",
                    started_at=now - timedelta(minutes=1)),
                Run(id=own_row, kind="scheduler", scope={"pid": 4242}, status="running",
                    started_at=now - timedelta(seconds=5)),
                Run(id=ok_row, kind="market_stats", scope={}, status="succeeded",
                    started_at=now - timedelta(minutes=9), finished_at=now - timedelta(minutes=8)),
            ])
            await session.commit()

        engine_ops = ops.EngineOperations()
        assert await engine_ops.recover_queue() >= 1
        assert await engine_ops.last_market_stats_at() is not None
        await engine_ops.set_next_check(slug, now + timedelta(hours=2))
        await engine_ops.set_local_status(slug, "late")
        last_checked, status = await engine_ops.freshness(slug)
        assert status == "late" and last_checked is not None
        assert await engine_ops.freshness("no-such-market") == (None, None)

        swept = await ops.sweep_orphaned_runs(max_age=timedelta(hours=4), current_pid=4242)
        assert swept >= 2

        async with sessions() as session:
            states = {
                r.id: r.status
                for r in (await session.execute(select(Run))).scalars()
                if r.id in {old_check, fresh_check, service_row, ok_row, own_row}
            }
            queue = {
                r.id: r.status
                for r in (await session.execute(
                    select(PendingRefresh).where(PendingRefresh.market_slug == slug)
                )).scalars()
            }
            errors = {
                r.id: r.error_class
                for r in (await session.execute(select(Run))).scalars()
                if r.id in {old_check, service_row}
            }
        assert queue == {stuck: "pending", done: "succeeded"}
        assert states[old_check] == "failed" and states[service_row] == "failed"
        assert states[fresh_check] == "running"  # a concurrent run-now may still be alive
        assert states[ok_row] == "succeeded"
        assert states[own_row] == "running"  # the live service's own ledger row is never swept
        assert set(errors.values()) == {"ServiceInterrupted"}
    finally:
        await engine.dispose()


@pytest.mark.database
@pytest.mark.asyncio
async def test_due_refresh_slugs_counts_due_failed_and_orphaned_rows_only(
    aevorex_test_db: LocalTestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(aevorex_test_db)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(ops, "async_session_maker", sessions)
    slug = f"due-{uuid4().hex[:6]}-fl"
    now = utc_now()
    try:
        async with sessions() as session:
            session.add(MarketFreshness(slug=slug, city="Due", state="FL", region_id="1"))
            await session.flush()
            for status, offset in (
                ("pending", -1), ("pending", -1), ("failed", -5),  # due
                ("pending", +60), ("failed", +60),  # waiting out a backoff
                ("processing", 0),  # orphan or in-flight
                ("succeeded", -1),
            ):
                session.add(PendingRefresh(
                    id=uuid4(), market_slug=slug, redfin_id=str(uuid4()),
                    listing_url="https://example.invalid/x", reason="new", status=status,
                    next_attempt_at=now + timedelta(minutes=offset),
                ))
            await session.commit()
        assert (await ops.EngineOperations().due_refresh_slugs())[slug] == 4
    finally:
        await engine.dispose()
