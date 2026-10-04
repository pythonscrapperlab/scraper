"""Step 6: hard size guard. >= 420 MiB: no children. >= 450 MiB: freshness only."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from aevorex.db.models import MarketFreshness, utc_now
from aevorex.publisher.service import Publisher
from aevorex.publisher.types import classify_size
from tests.publisher.conftest import SLUG, Rig, seed_market

pytestmark = pytest.mark.database

MIB = 1024 * 1024
CHILD_TABLES = (
    "valuation", "agents", "images", "comps", "history", "features", "neighbourhood", "tax_history",
)


def _pin_size(publisher: Publisher, size_bytes: int) -> None:
    async def fixed(_connection: object) -> int:
        return size_bytes

    publisher.store.database_size = fixed  # type: ignore[method-assign]


def _record_pings(publisher: Publisher) -> list[bool]:
    pings: list[bool] = []

    async def ping(*, failed: bool) -> None:
        pings.append(failed)

    publisher._ping_healthcheck = ping  # type: ignore[method-assign]
    return pings


async def _count(rig: Rig, table: str) -> int:
    async with rig.engine.connect() as connection:
        return int((await connection.scalar(text(f"select count(*) from serving.{table}"))) or 0)


async def _children(rig: Rig) -> int:
    return sum([await _count(rig, name) for name in CHILD_TABLES])


@pytest.mark.parametrize(
    ("mib", "action", "children", "scores"),
    [
        (419.99, "warn", True, True),
        (420, "stop_new_cities", False, True),
        (449.99, "stop_new_cities", False, True),
        (450, "page", False, False),
    ],
)
def test_guard_flags_at_the_boundaries(mib: float, action: str, children: bool, scores: bool) -> None:
    guard = classify_size(round(mib * MIB))
    assert guard.action == action
    assert guard.allow_children is children
    assert guard.allow_scores is scores


@pytest.mark.asyncio
async def test_just_below_420_publishes_everything(rig: Rig) -> None:
    await seed_market(rig)
    publisher = rig.publisher()
    _pin_size(publisher, round(419.99 * MIB))
    pings = _record_pings(publisher)

    result = await publisher.push(SLUG)

    assert result.status == "succeeded"
    assert await _count(rig, "properties") == 9
    assert await _count(rig, "scores") == 45
    assert await _children(rig) > 0
    assert pings == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize("mib", [420, 449.99])
async def test_420_and_above_publishes_freshness_and_scores_but_no_children(
    rig: Rig, mib: float
) -> None:
    await seed_market(rig)
    publisher = rig.publisher()
    _pin_size(publisher, round(mib * MIB))
    pings = _record_pings(publisher)
    # A brand-new market is refused at >= 420 MiB (existing behaviour).
    with pytest.raises(RuntimeError, match="SupabaseSizeGuardNewMarket"):
        await publisher.push(SLUG)

    # Establish the market below the line, then cross it.
    _pin_size(publisher, 100 * MIB)
    first = await publisher.push(SLUG)
    assert first.status == "succeeded"
    before_children = await _children(rig)
    assert before_children > 0

    # A property changes after the market crossed the line: its row and scores still go out.
    async with rig.sessions() as session:
        await session.execute(text("update properties set price = 999999 where redfin_id = 'e6-0'"))
        await session.commit()
    _pin_size(publisher, round(mib * MIB))
    pings.clear()
    rig.clear()
    result = await publisher.push(SLUG)

    assert result.status == "partial"
    assert result.size_action == "stop_new_cities"
    assert result.counts["properties"] == 1
    assert all(result.counts[name] == 0 for name in CHILD_TABLES)
    assert ("insert", "serving.history") not in rig.write_tables()
    assert ("delete", "serving.images") not in rig.write_tables()
    assert await _children(rig) == before_children
    assert await _count(rig, "change_events") == 0
    assert pings == [True]  # the owner is paged


@pytest.mark.asyncio
async def test_children_are_not_written_when_the_database_grows_mid_push(rig: Rig) -> None:
    await seed_market(rig)
    publisher = rig.publisher()
    sizes = iter([100 * MIB, 421 * MIB])  # first read is fine; the re-read before children is over

    async def growing(_connection: object) -> int:
        return next(sizes, 421 * MIB)

    publisher.store.database_size = growing  # type: ignore[method-assign]
    result = await publisher.push(SLUG)

    assert await _count(rig, "properties") == 9
    assert await _children(rig) == 0
    assert result.counts["children_withheld"] == 1


@pytest.mark.asyncio
async def test_450_and_above_writes_freshness_only(rig: Rig) -> None:
    ids = await seed_market(rig)
    publisher = rig.publisher()
    _pin_size(publisher, 100 * MIB)
    await publisher.push(SLUG)
    properties_before = await _count(rig, "properties")

    async with rig.sessions() as session:
        await session.execute(text("update properties set price = 888888 where redfin_id = 'e6-1'"))
        row = await session.get(MarketFreshness, SLUG)
        assert row is not None
        row.last_checked_at = utc_now() + timedelta(minutes=7)
        await session.commit()

    _pin_size(publisher, 450 * MIB)
    pings = _record_pings(publisher)
    rig.clear()
    result = await publisher.push(SLUG)

    assert result.status == "partial"
    assert result.counts["publish_withheld"] == 1
    assert result.counts["properties"] == 0 and result.counts["scores"] == 0
    assert rig.write_tables() <= {("update", "serving.markets"), ("insert", "serving.heartbeats"),
                                  ("insert", "serving.runs")}
    assert await _count(rig, "properties") == properties_before
    async with rig.engine.connect() as connection:
        price = await connection.scalar(
            text("select price from serving.properties where id = :id"), {"id": ids[1]}
        )
        checked = await connection.scalar(
            text("select last_checked_at from serving.markets where slug = 'orlando-fl'")
        )
    assert price != 888888  # stale, but honest: freshness says when it was last checked
    assert checked is not None
    assert pings == [True]
