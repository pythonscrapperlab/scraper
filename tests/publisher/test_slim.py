"""Step 5: only what the web renders reaches the cloud."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text, update

from aevorex.db.models import (
    PriceHistory,
    Property,
    PropertyComp,
    PropertyFeature,
    PropertyImage,
    TaxHistory,
    utc_now,
)
from aevorex.publisher.markets import market_definition
from aevorex.publisher.service import BREAKDOWN_FIELDS
from aevorex.publisher.slim import (
    FEATURE_NAMES,
    MAX_COMPS,
    MAX_HISTORY_EVENTS,
    MAX_TAX_YEARS,
    curated_features,
    latest_comps,
    recent_history,
    recent_tax_years,
    slim_breakdown,
)
from aevorex.publisher.types import DELISTED_RETENTION_DAYS, retention_cutoff
from aevorex.scoring.recompose import recompose
from tests.publisher.conftest import SLUG, Rig, seed_market

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)


def test_feature_allowlist_is_at_most_25_and_leaks_nothing_else() -> None:
    assert len(FEATURE_NAMES) <= 25
    raw = {
        "Style": "Townhouse", "Garage Spaces": "2", "FIREPLACE_YN": "Has Fireplace",
        "Directions": "Turn left at the clubhouse; owner lives at 5 Elm", "Attribution Contact": "555-0100",
        "VIRTUAL_TOUR_URLUNBRANDED": "https://tour.example.net/x", "Universal Property Id": "abc",
        "Pool Features": "None", "Property Condition": "Resale", "APN": "1-2-3",
    }
    features = curated_features({"heating": "Central", "roof": "Shingle", "sewer": ""}, raw)
    assert set(features) <= set(FEATURE_NAMES)
    assert features == {
        "heating": "Central", "roof": "Shingle", "style": "Townhouse", "garage_spaces": "2",
        "fireplace": "Has Fireplace", "property_condition": "Resale",
    }
    assert "pool" not in features  # the literal string "None" is not a feature
    for forbidden in ("Directions", "Attribution Contact", "VIRTUAL_TOUR_URLUNBRANDED", "APN"):
        assert forbidden not in features
        assert all(forbidden not in str(value) for value in features.values())


def test_feature_values_are_capped() -> None:
    features = curated_features({"interior_features": "x " * 500}, {})
    assert len(features["interior_features"]) <= 160


def test_history_keeps_last_15_within_7_years_and_never_rentals() -> None:
    events = [
        SimpleNamespace(id=uuid4(), event_date=NOW - timedelta(days=30 * i), is_rental_event=False)
        for i in range(1, 40)
    ]
    events.append(SimpleNamespace(id=uuid4(), event_date=NOW, is_rental_event=True))
    kept = recent_history(events, NOW)
    assert len(kept) == MAX_HISTORY_EVENTS
    assert kept[0].event_date > kept[-1].event_date  # newest first
    assert all(not event.is_rental_event for event in kept)

    old = [SimpleNamespace(id=uuid4(), event_date=NOW - timedelta(days=365 * y), is_rental_event=False)
           for y in (1, 3, 6, 8, 12)]
    assert [round((NOW - e.event_date).days / 365) for e in recent_history(old, NOW)] == [1, 3, 6]


def test_tax_history_keeps_five_latest_years() -> None:
    rows = [SimpleNamespace(tax_year=year) for year in range(2010, 2026)]
    assert [r.tax_year for r in recent_tax_years(rows)] == [2025, 2024, 2023, 2022, 2021]
    assert len(recent_tax_years(rows)) == MAX_TAX_YEARS


def test_comps_keep_six_most_recent() -> None:
    comps = [
        SimpleNamespace(id=uuid4(), sold_date=datetime(2026, 1, 1) - timedelta(days=10 * i))
        for i in range(12)
    ] + [SimpleNamespace(id=uuid4(), sold_date=None)]
    kept = latest_comps(comps)
    assert len(kept) == MAX_COMPS
    assert all(c.sold_date is not None for c in kept)
    assert kept == sorted(kept, key=lambda c: c.sold_date, reverse=True)


def _breakdown() -> dict:
    return {
        "lens": "fix_flip",
        "components": [
            {"key": "a", "label": "A", "weight": 0.6, "subscore": 70, "available": True, "drivers": [
                {"label": "Max offer", "value": 357, "field": "max_allowable_offer"},
                {"label": "ROI", "value": -3.2, "field": "roi_pct"},
                {"label": "Probate", "value": True, "field": "is_probate_or_estate"},
                {"label": "Missing thing", "value": None, "field": "price"},
            ]},
            {"key": "b", "label": "B", "weight": 0.4, "subscore": 40, "available": True, "drivers": []},
        ],
        "adjustments": [
            {"type": "multiplier", "label": "Vacant", "value": 1.08, "applied": True},
            {"type": "confidence_shrink", "label": "Confidence", "value": 0.5, "applied": True, "floor": 0.3},
        ],
    }


def test_slim_breakdown_drops_null_drivers_and_unused_fields_and_still_recomposes() -> None:
    original = _breakdown()
    snapshot = copy.deepcopy(original)
    slim = slim_breakdown(original, BREAKDOWN_FIELDS)
    assert original == snapshot  # input untouched
    drivers = slim["components"][0]["drivers"]
    assert [d["label"] for d in drivers] == ["Max offer", "ROI", "Probate"]  # null driver gone
    assert drivers[0]["field"] == "max_allowable_offer"  # a valuation column the web holds
    assert drivers[2]["field"] == "is_probate_or_estate"  # a flag the web holds
    assert "field" not in drivers[1]  # roi_pct is not a served column
    assert slim["components"][1]["drivers"] == []
    assert recompose(slim) == recompose(original)


# ---- the SQL retention path: one source of truth -----------------------------------------


def test_retention_is_seven_days_exact_boundary() -> None:
    assert DELISTED_RETENTION_DAYS == 7
    cutoff = retention_cutoff(NOW)
    assert cutoff == datetime(2026, 9, 27, 12)
    assert not (cutoff - timedelta(microseconds=1) >= cutoff)
    assert cutoff >= cutoff


@pytest.mark.database
@pytest.mark.asyncio
async def test_load_local_keeps_delisted_through_day_seven(rig: Rig) -> None:
    ids = await seed_market(rig)
    cutoff = retention_cutoff(NOW)
    async with rig.sessions() as session:
        for property_id, delisted_at in (
            (ids[0], cutoff),                                   # exactly 7 days: kept
            (ids[1], cutoff - timedelta(seconds=1)),            # just past: pruned
            (ids[2], cutoff + timedelta(days=3)),               # 4 days ago: kept
            (ids[3], utc_now() - timedelta(days=30)),           # old retention window: now pruned
        ):
            await session.execute(
                text("update listing_presence set is_delisted = true where property_id = :id"),
                {"id": property_id},
            )
            await session.execute(
                update(Property).where(Property.id == property_id).values(delisted_at=delisted_at)
            )
        await session.commit()

    publisher = rig.publisher(now=NOW)
    properties, _ = await publisher._load_local(market_definition(SLUG))
    loaded = {item.id for item in properties}
    assert ids[0] in loaded and ids[2] in loaded
    assert ids[1] not in loaded and ids[3] not in loaded
    assert len(loaded) == 9 - 2


# ---- what actually lands in the cache ----------------------------------------------------


@pytest.mark.database
@pytest.mark.asyncio
async def test_remote_payload_is_slim_and_redacted(rig: Rig) -> None:
    ids = await seed_market(rig)
    async with rig.sessions() as session:
        pid = ids[0]
        session.add(PropertyFeature(
            property_id=pid, heating="Central", roof="Tile",
            raw_amenities={"Style": "Single Family", "Directions": "Gate code 4411", "Attribution Contact": "Bob 407-555-0100"},
        ))
        for index in range(30):
            session.add(PriceHistory(
                property_id=pid, source="redfin", event_type="sold", event="Sold", price=100_000 + index,
                event_date=NOW.replace(tzinfo=None) - timedelta(days=40 * index),
                event_source="MLS", is_rental_event=False,
            ))
        for year in range(2014, 2026):
            session.add(TaxHistory(property_id=pid, source="redfin", tax_year=year, tax_amount=1000 + year))
        for index in range(10):
            session.add(PropertyComp(
                property_id=pid, source="redfin", comp_address=f"{index} Comp St", price=200_000,
                sold_date=datetime(2026, 1, 1) - timedelta(days=index),
            ))
        for index in range(20):
            session.add(PropertyImage(property_id=pid, source="redfin", url=f"https://img.example.invalid/{index}.jpg", sort_order=index))
        await session.execute(
            update(Property).where(Property.id == pid).values(
                description="Lovely. " * 200 + " Call Bob at 407-555-0100.",
                ai_summary="Bright rooms. Contact Dana Lee at dana@example.com.",
            )
        )
        await session.commit()

    result = await rig.publisher(now=NOW).push(SLUG)
    assert result.status == "succeeded"

    async with rig.engine.connect() as connection:
        async def one(sql: str):  # type: ignore[no-untyped-def]
            return (await connection.execute(text(sql), {"id": pid})).all()

        description, summary = (await one(
            "select description, ai_summary from serving.properties where id = :id"))[0]
        assert len(description) <= 600 and "555" not in description and "Bob" not in description
        assert "dana@" not in summary and "Dana" not in summary
        features = (await one("select features from serving.features where property_id = :id"))[0][0]
        assert features == {"heating": "Central", "roof": "Tile", "style": "Single Family"}
        assert (await one("select count(*) from serving.history where property_id = :id"))[0][0] == 15
        assert (await one("select count(*) from serving.tax_history where property_id = :id"))[0][0] == 5
        assert (await one("select count(*) from serving.comps where property_id = :id"))[0][0] == 6
        assert (await one("select count(*) from serving.images where property_id = :id"))[0][0] == 12
        assert (await one("select min(tax_year) from serving.tax_history where property_id = :id"))[0][0] == 2021
