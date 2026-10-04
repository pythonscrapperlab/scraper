from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import URL, func, select
from sqlalchemy.ext.asyncio import create_async_engine

from aevorex.db.models import MarketFreshness, Property, PropertyAnalysis, PropertyImage
from aevorex.publisher.remote import RemoteStore
from aevorex.publisher.service import Publisher, apply_rejections
from aevorex.publisher.snapshots import build_demo_snapshot
from aevorex.publisher.types import MarketDefinition, PublishResult, classify_size


@pytest.mark.parametrize(
    ("megabytes", "action"),
    [
        (349.999, "ok"),
        (350, "warn"),
        (419.999, "warn"),
        (420, "stop_new_cities"),
        (449.999, "stop_new_cities"),
        (450, "page"),
    ],
)
def test_size_guard_exact_thresholds(megabytes: float, action: str) -> None:
    assert classify_size(round(megabytes * 1024 * 1024)).action == action


def _scored_property(position: int) -> Property:
    score = 90.0 - position
    percentile = 99.0 - position
    breakdown = {
        "components": [
            {"key": "one", "label": "First component", "weight": 0.5, "subscore": score},
            {"key": "two", "label": "Second component", "weight": 0.5, "subscore": score},
            {"key": "three", "label": "Hidden component", "weight": 0.0, "subscore": 0},
        ],
        "adjustments": [],
    }
    item = Property(
        id=uuid4(),
        primary_source="redfin",
        address=f"{position} Demo Street",
        city="Orlando",
        state="FL",
        zip_code="32801",
        price=300_000 + position * 10_000,
        bedrooms=3,
        bathrooms=2,
        sqft=1500,
        days_on_market=position + 1,
        listing_status_normalized="active",
        last_seen_at=datetime(2026, 10, 3),
    )
    item.property_images = [PropertyImage(url="https://example.com/photo.jpg", sort_order=0)]
    item.analysis = PropertyAnalysis(
        property_id=item.id,
        motivated_seller_score=score,
        motivated_seller_grade="A",
        motivated_seller_percentile=percentile,
        motivated_seller_confidence=1.0,
        motivated_seller_factors={"cumulative_price_cut_pct": 10, "price_reduction_count": 2},
        motivated_seller_breakdown=breakdown,
        computed_at=datetime(2026, 10, 3),
    )
    return item


def test_demo_snapshot_has_exact_shape_and_redaction() -> None:
    market = MarketDefinition(
        slug="orlando-fl",
        city="Orlando",
        state="FL",
        region_id="13655",
        timezone="America/New_York",
        is_demo=True,
    )
    freshness = MarketFreshness(
        slug="orlando-fl",
        city="Orlando",
        state="FL",
        region_id="13655",
        check_status="ok",
    )
    payload = build_demo_snapshot(
        market,
        "motivated_seller",
        [_scored_property(position) for position in range(8)],
        freshness,
    )
    assert len(payload["full"]) == 3
    assert len(payload["stubs"]) == 5
    assert set(payload["stubs"][0]) == {"price_band", "dom"}
    assert payload["full"][0]["rank"] == 1
    assert payload["full"][0]["pool_size"] == 8
    assert all(len(row["component_labels"]) == 2 for row in payload["full"])
    serialized = str(payload).casefold()
    for forbidden in ("agent", "broker", "phone", "breakdown"):
        assert forbidden not in serialized


def test_demo_excludes_incomplete_and_inactive_rows_without_changing_pool_rank() -> None:
    market = MarketDefinition(slug="orlando-fl", city="Orlando", state="FL",
                              region_id="13655", timezone="America/New_York", is_demo=True)
    freshness = MarketFreshness(slug=market.slug, check_status="failed")
    rows = [_scored_property(position) for position in range(12)]
    rows[0].listing_status_normalized = "sold"
    rows[1].property_images = []
    rows[2].price = None
    rows[3].days_on_market = None
    payload = build_demo_snapshot(market, "motivated_seller", rows, freshness)
    assert payload["full"][0]["rank"] == 4
    assert payload["full"][0]["pool_size"] == 11
    assert payload["full"][0]["headline"]["value"] == 10
    assert payload["full"][0]["reasons"] == ["2 price cuts in 5 days listed"]


def test_address_formatting_preserves_direction_and_unit() -> None:
    from aevorex.publisher.snapshots import format_address
    assert format_address("2627 s bayshore dr unit 1202") == "2627 S Bayshore Dr #1202"
    assert format_address("1075 nw 100th st") == "1075 NW 100th St"


def test_recompose_rejection_marks_run_partial() -> None:
    item = _scored_property(0)
    item.analysis.motivated_seller_breakdown["components"][0]["subscore"] = 1
    publisher = object.__new__(Publisher)
    rows, rejected = publisher._score_rows([item])
    result = PublishResult(market="orlando-fl")
    apply_rejections(result, rejected)
    assert rows == []
    assert result.status == "partial"
    assert result.telemetry()["rejected_scores"] == 1


@pytest.mark.asyncio
@pytest.mark.database
async def test_remote_batch_upsert_is_idempotent(aevorex_test_db) -> None:
    database = aevorex_test_db
    engine = create_async_engine(
        URL.create(
            "postgresql+asyncpg",
            username=database.user,
            password=database.password,
            host=database.host,
            port=database.port,
            database=database.database,
        )
    )
    store = RemoteStore(engine, batch_size=2)
    await store.initialize()
    market_id = uuid4()
    row = {
        "id": market_id,
        "city": "Orlando",
        "state": "FL",
        "region_id": "13655",
        "slug": "orlando-fl",
        "tz": "America/New_York",
        "zips": ["32801"],
    }
    async with store.transaction() as connection:
        await store.upsert(connection, "markets", [row], conflict=("slug",))
        await store.upsert(connection, "markets", [row], conflict=("slug",))
    async with engine.connect() as connection:
        markets = store.table("markets")
        count = await connection.scalar(
            select(func.count()).where(markets.c.slug == "orlando-fl")
        )
    assert count == 1
    await store.close()


@pytest.mark.asyncio
@pytest.mark.database
async def test_score_previous_values_move_only_when_current_changes(aevorex_test_db) -> None:
    database = aevorex_test_db
    engine = create_async_engine(
        URL.create(
            "postgresql+asyncpg",
            username=database.user,
            password=database.password,
            host=database.host,
            port=database.port,
            database=database.database,
        )
    )
    store = RemoteStore(engine)
    await store.initialize()
    market_id = uuid4()
    property_id = uuid4()
    async with store.transaction() as connection:
        await store.upsert(
            connection,
            "markets",
            [{
                "id": market_id,
                "city": "Orlando",
                "state": "FL",
                "region_id": "13655",
                "slug": "score-history-fl",
                "tz": "America/New_York",
            }],
            conflict=("slug",),
        )
        await store.upsert(
            connection,
            "properties",
            [{
                "id": property_id,
                "market_id": market_id,
                "address": "Test",
                "city": "Orlando",
                "state": "FL",
                "zip": "32801",
            }],
            conflict=("id",),
        )
    base = {
        "property_id": property_id,
        "lens": "motivated_seller",
        "score": 60,
        "grade": "C",
        "percentile": 70,
        "confidence": 1,
        "tier": "rest",
        "prev_score": None,
        "prev_percentile": None,
        "version": "v3",
        "computed_at": datetime(2026, 10, 3, tzinfo=UTC),
    }
    async with store.transaction() as connection:
        await store.upsert(
            connection,
            "scores",
            [base],
            conflict=("property_id", "lens"),
            preserve_previous_scores=True,
        )
        await store.upsert(
            connection,
            "scores",
            [base],
            conflict=("property_id", "lens"),
            preserve_previous_scores=True,
        )
        changed = {**base, "score": 65, "grade": "B", "percentile": 82, "tier": "strong"}
        await store.upsert(
            connection,
            "scores",
            [changed],
            conflict=("property_id", "lens"),
            preserve_previous_scores=True,
        )
        stale = {**base, "lens": "fix_flip"}
        await store.upsert(
            connection,
            "scores",
            [stale],
            conflict=("property_id", "lens"),
            preserve_previous_scores=True,
        )
        assert await store.delete_market_scores_not_in(
            connection,
            market_id,
            [(property_id, "motivated_seller")],
        ) == 1
        await store.upsert(
            connection,
            "scores",
            [changed],
            conflict=("property_id", "lens"),
            preserve_previous_scores=True,
        )
    async with engine.connect() as connection:
        scores = store.table("scores")
        row = (
            await connection.execute(
                select(
                    scores.c.score,
                    scores.c.percentile,
                    scores.c.prev_score,
                    scores.c.prev_percentile,
                ).where(scores.c.property_id == property_id)
            )
        ).one()
    assert tuple(row) == (65, 82, 60, 70)
    async with engine.connect() as connection:
        assert await connection.scalar(select(func.count()).select_from(scores)) == 1
    await store.close()
