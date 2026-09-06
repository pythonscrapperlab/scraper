"""
Ingestion-completeness tests for the Redfin normalizer.

These lock down the fields that were being silently dropped between the
scraper and the normalized tables: unpriced listing events, the
`offer_insights` block, the real MLS status, `ai_summary`, and the
Transport stop list. Every parser here handles human-readable marketing
copy, so the "malformed input returns None instead of raising" cases matter
as much as the happy paths — one reworded sentence must never cost us a
whole property.

No DB required: normalize() is pure, so these run on the dict it returns.
"""

import asyncio
from datetime import datetime, timezone

from aevorex.db.event_types import (
    COMING_SOON,
    CONTINGENT,
    DELISTED,
    INCREASED,
    LISTED,
    PENDING,
    PRICE_CHANGED,
    REDUCED,
    RELISTED,
    REMOVED,
    SOLD,
)
from aevorex.normalizers.redfin import RedfinNormalizer


def run(coro):
    return asyncio.run(coro)


def normalize(**overrides):
    return run(RedfinNormalizer().normalize(make_payload(**overrides)))


def make_payload(**overrides):
    """A minimally valid parsed-Redfin payload, shaped like scrapers/redfin.py emits."""
    payload = {
        "redfin_id": "home/12345678",
        "street_address": "123 Main St",
        "city": "Orlando",
        "state": "FL",
        "zip_code": "32801",
        "list_price": 425_000,
        "beds": 3,
        "baths": 2.0,
        "sqft": 1500,
        # schema.org offers.availability — the useless one
        "listing_status": "InStock",
        # the real MLS status
        "status": "Active",
        "basic_info": {"apn": "30-22-01-9438-00-150", "propertyLastUpdatedDate": 1779919037923},
        "event_history": [],
    }
    payload.update(overrides)
    return payload


def event(event_name, date, price=None, source="Stellar MLS as Distributed by MLS Grid", source_id="O6424408"):
    return {
        "event": event_name,
        "date": datetime(*date, tzinfo=timezone.utc),
        "price": price,
        "source": source,
        "source_id": source_id,
    }


def by_event(price_history, name):
    return [row for row in price_history if row["event"] == name]


# ------------------------------------------------------------------
# Fix 1 — unpriced events
# ------------------------------------------------------------------


def test_events_with_no_price_are_persisted():
    # The five event types Redfin never prices. Every one of these was being
    # discarded before, taking the expired-listing and relisted signals with it.
    normalized = normalize(event_history=[
        event("Listed", (2026, 1, 5), price=450_000),
        event("Listing Removed", (2026, 3, 1)),
        event("Pending", (2026, 2, 20)),
        event("Relisted", (2026, 4, 1)),
        event("Contingent", (2026, 2, 25)),
        event("Delisted", (2026, 5, 1)),
        event("Coming Soon", (2026, 1, 1)),
    ])

    price_history = normalized["price_history"]
    assert len(price_history) == 7, "no event may be dropped for lacking a price"

    unpriced = {row["event"]: row for row in price_history if row["price"] is None}
    assert set(unpriced) == {"Listing Removed", "Pending", "Relisted", "Contingent", "Delisted", "Coming Soon"}

    assert unpriced["Listing Removed"]["event_type"] == REMOVED
    assert unpriced["Pending"]["event_type"] == PENDING
    assert unpriced["Relisted"]["event_type"] == RELISTED
    assert unpriced["Contingent"]["event_type"] == CONTINGENT
    assert unpriced["Delisted"]["event_type"] == DELISTED
    assert unpriced["Coming Soon"]["event_type"] == COMING_SOON


def test_raw_event_wording_is_preserved_verbatim():
    normalized = normalize(event_history=[
        event("Sold (MLS)", (2025, 2, 12), price=390_000),
        event("Sold (Public Records)", (2018, 8, 20), price=282_500),
    ])

    wording = {row["event"] for row in normalized["price_history"]}
    assert wording == {"Sold (MLS)", "Sold (Public Records)"}
    assert all(row["event_type"] == SOLD for row in normalized["price_history"])


def test_event_source_and_source_event_id_are_captured():
    normalized = normalize(event_history=[
        event("Listed", (2026, 1, 5), price=450_000, source="Beaches MLS", source_id="A10884991"),
    ])

    row = normalized["price_history"][0]
    assert row["source_event_id"] == "A10884991"
    assert row["event_source"] == "Beaches MLS"
    # `source` stays the platform, not the feed — it's what the pipeline keys on.
    assert row["source"] == "redfin"


def test_undated_event_is_skipped_not_stored_with_a_fake_date():
    normalized = normalize(event_history=[
        event("Listed", (2026, 1, 5), price=450_000),
        {"event": "Pending", "date": None, "price": None, "source": "Beaches MLS", "source_id": "X1"},
    ])

    assert len(normalized["price_history"]) == 1
    assert normalized["price_history"][0]["event"] == "Listed"


def test_price_changed_direction_is_inferred_from_the_previous_price():
    normalized = normalize(event_history=[
        event("Price Changed", (2026, 4, 1), price=390_000),
        event("Price Changed", (2026, 3, 1), price=440_000),
        event("Listed", (2026, 1, 5), price=425_000),
    ])

    directions = {row["event_date"]: row["event_type"] for row in normalized["price_history"]}
    assert directions[datetime(2026, 3, 1)] == INCREASED  # 425k -> 440k
    assert directions[datetime(2026, 4, 1)] == REDUCED  # 440k -> 390k


def test_price_changed_with_no_earlier_price_is_not_assumed_to_be_a_cut():
    normalized = normalize(event_history=[
        event("Price Changed", (2026, 4, 1), price=390_000),
    ])

    assert normalized["price_history"][0]["event_type"] == PRICE_CHANGED


def test_reductions_are_counted_from_the_normalized_type():
    normalized = normalize(event_history=[
        event("Listed", (2026, 1, 5), price=450_000),
        event("Price Changed", (2026, 2, 5), price=440_000),
        event("Price Changed", (2026, 3, 5), price=425_000),
    ])

    assert sum(1 for row in normalized["price_history"] if row["event_type"] == REDUCED) == 2


def test_event_history_junk_entries_do_not_break_the_property():
    normalized = normalize(event_history=[
        "not a dict",
        None,
        event("Listed", (2026, 1, 5), price=450_000),
    ])

    assert normalized is not None
    assert len(normalized["price_history"]) == 1


# ------------------------------------------------------------------
# Fix 2 — offer_insights
# ------------------------------------------------------------------


OFFER_INSIGHTS = [
    {
        "header": "LIST PRICE",
        "title": "$1,195,000",
        "copy": "Median list price in this area is $690,286. (42% lower than this home)",
    },
    {
        "header": "PRICE DROP",
        "title": "1 price drop",
        "copy": "17% of homes in this area have had a price drop in the past month.",
    },
    {"header": "SALE-TO-LIST", "title": "96.0%", "copy": "Most homes are selling 4% below list price."},
    {"header": "DAYS ON MARKET", "title": "6 days", "copy": "The average home goes pending in 76 days."},
]


def test_offer_insights_are_parsed_into_columns():
    normalized = normalize(offer_insights=OFFER_INSIGHTS)

    assert normalized["price_drop_count"] == 1
    assert normalized["area_price_drop_pct"] == 17.0
    assert normalized["sale_to_list_pct"] == 96.0
    assert normalized["area_avg_days_to_pending"] == 76
    assert normalized["area_median_list_price"] == 690_286


def test_zero_price_drops_is_stored_as_zero_not_null():
    # "0 price drops" is a real answer ("this seller has never cut"), which is
    # different from "we don't know" — the plural also has to parse.
    normalized = normalize(offer_insights=[
        {"header": "PRICE DROP", "title": "0 price drops", "copy": "14% of homes in this area have had a price drop in the past month."},
    ])

    assert normalized["price_drop_count"] == 0


def test_raw_offer_insight_copy_is_kept_in_meta():
    normalized = normalize(offer_insights=OFFER_INSIGHTS)

    headers = [block["header"] for block in normalized["meta"]["offer_insights"]]
    assert headers == ["LIST PRICE", "PRICE DROP", "SALE-TO-LIST", "DAYS ON MARKET"]


def test_offer_insight_parsers_return_none_on_malformed_input():
    normalizer = RedfinNormalizer
    junk = [None, "", "   ", "n/a", "no drops recorded", [], {}, object()]

    for value in junk:
        assert normalizer._parse_drop_count(value) is None
        assert normalizer._parse_leading_percent(value) is None
        assert normalizer._parse_days(value) is None
        assert normalizer._parse_dollar_amount(value) is None
        assert normalizer._parse_percent(value) is None


def test_offer_insights_reworded_by_the_source_yields_none_not_an_exception():
    normalized = normalize(offer_insights=[
        {"header": "PRICE DROP", "title": "no price changes yet", "copy": "Homes here rarely see reductions."},
        {"header": "SALE-TO-LIST", "title": "about list price", "copy": ""},
        {"header": "DAYS ON MARKET", "title": "", "copy": "Homes go pending quickly."},
        {"header": "LIST PRICE", "title": "", "copy": "Median list price in this area is unavailable."},
        {"header": "BRAND NEW BLOCK", "title": "?", "copy": "?"},
    ])

    assert normalized is not None
    assert normalized["price_drop_count"] is None
    assert normalized["area_price_drop_pct"] is None
    assert normalized["sale_to_list_pct"] is None
    assert normalized["area_avg_days_to_pending"] is None
    assert normalized["area_median_list_price"] is None


def test_offer_insights_of_the_wrong_shape_are_survivable():
    for value in ("unexpected string", {"header": "PRICE DROP"}, [None, 7, "x"], 42):
        normalized = normalize(offer_insights=value)
        assert normalized is not None
        assert normalized["price_drop_count"] is None


def test_missing_offer_insights_leaves_every_column_null():
    normalized = normalize()

    for field in RedfinNormalizer.OFFER_INSIGHT_FIELDS:
        assert normalized[field] is None


# ------------------------------------------------------------------
# Fix 3 — real listing status
# ------------------------------------------------------------------


def test_real_mls_status_is_persisted_not_the_schema_org_value():
    normalized = normalize(status="Active", listing_status="InStock")

    assert normalized["listing_status"] == "Active"
    assert normalized["listing_status_normalized"] == "active"
    assert normalized["availability_status"] == "InStock"


def test_coming_soon_survives_as_a_pre_market_signal():
    normalized = normalize(status="Coming Soon")

    assert normalized["listing_status"] == "Coming Soon"
    assert normalized["listing_status_normalized"] == "coming_soon"


def test_malformed_source_spacing_is_kept_raw_and_normalized_separately():
    # Redfin ships this with the space missing after "Contingent-". Cleaning
    # it in place would put a value in the DB that no longer matches the
    # source payload, so the raw string stays and the slug carries the meaning.
    normalized = normalize(status="Contingent- Accepting Backups")

    assert normalized["listing_status"] == "Contingent- Accepting Backups"
    assert normalized["listing_status_normalized"] == "contingent"


def test_status_missing_leaves_listing_status_null_rather_than_instock():
    normalized = normalize(status=None, listing_status="InStock")

    assert normalized["listing_status"] is None
    assert normalized["listing_status_normalized"] is None
    assert normalized["availability_status"] == "InStock"


def test_unknown_status_wording_keeps_the_raw_value():
    normalized = normalize(status="Temporarily Off Market Pending Repairs")

    assert normalized["listing_status"] == "Temporarily Off Market Pending Repairs"
    # "pending" matches — the slug is a best-effort read of the raw string,
    # which is exactly why the raw string is kept alongside it.
    assert normalized["listing_status_normalized"] == "pending"


# ------------------------------------------------------------------
# Fix 4 — ai_summary
# ------------------------------------------------------------------


AI_SUMMARY = (
    "- Screened-in pool overlooks Tequesta Country Club golf course with sweeping fairway views.\n"
    "- One-story CBS home with three bedrooms and three bathrooms.\n"
    "- Metal roof, hurricane-impact windows, and new 5-ton A/C installed 2024."
)


def test_ai_summary_is_persisted_verbatim():
    normalized = normalize(ai_summary=AI_SUMMARY)

    assert normalized["ai_summary"] == AI_SUMMARY
    assert normalized["ai_summary"].count("\n") == 2, "bullet structure must survive intact"


def test_missing_ai_summary_is_null_not_empty_string():
    assert normalize(ai_summary="")["ai_summary"] is None
    assert normalize()["ai_summary"] is None


# ------------------------------------------------------------------
# Fix 5 — Transport
# ------------------------------------------------------------------


def test_transport_list_from_the_fixed_scraper():
    normalized = normalize(Transport=["Gardens Mall", "PGA BLVD at TOYS R US"])

    assert [row["stop_name"] for row in normalized["transport_stops"]] == [
        "Gardens Mall",
        "PGA BLVD at TOYS R US",
    ]


def test_transport_legacy_python_set_repr_still_parses():
    legacy = "{'PGA BLVD at HARBOUR FINANCE CTR', 'GARDENS MALL TRM at SEARS TRM', 'Gardens Mall'}"

    stops = {row["stop_name"] for row in normalize(Transport=legacy)["transport_stops"]}

    assert stops == {
        "PGA BLVD at HARBOUR FINANCE CTR",
        "GARDENS MALL TRM at SEARS TRM",
        "Gardens Mall",
    }


def test_transport_legacy_repr_with_an_apostrophe_does_not_lose_every_stop():
    # literal_eval can't read this; the string-splitting fallback still
    # recovers all three stops rather than returning nothing.
    legacy = "{'JOE'S DINER at MAIN ST', 'Gardens Mall', 'PGA BLVD at TOYS R US'}"

    stops = [row["stop_name"] for row in normalize(Transport=legacy)["transport_stops"]]

    assert len(stops) == 3
    assert "Gardens Mall" in stops
    assert any("MAIN ST" in stop for stop in stops)


def test_transport_duplicates_collapse_and_junk_is_ignored():
    normalized = normalize(Transport=["Gardens Mall", "Gardens Mall", None, 7, "  "])

    assert [row["stop_name"] for row in normalized["transport_stops"]] == ["Gardens Mall"]


def test_transport_missing_or_unreadable_yields_no_stops():
    assert normalize()["transport_stops"] == []
    assert normalize(Transport="")["transport_stops"] == []
    assert normalize(Transport=12345)["transport_stops"] == []


# ------------------------------------------------------------------
# Smaller issues — state casing, listing freshness
# ------------------------------------------------------------------


def test_state_is_uppercased_on_write():
    assert normalize(state="fl")["state"] == "FL"
    assert normalize(state="Fl")["state"] == "FL"
    assert normalize(state=" fl ")["state"] == "FL"


def test_source_updated_at_parsed_from_epoch_milliseconds():
    normalized = normalize(basic_info={"propertyLastUpdatedDate": 1779919037923})

    assert normalized["source_updated_at"] == datetime(2026, 5, 27, 21, 57, 17, 923000)


def test_source_updated_at_absent_is_null():
    assert normalize(basic_info={})["source_updated_at"] is None


# ------------------------------------------------------------------
# Guardrails on existing behavior
# ------------------------------------------------------------------


def test_missing_required_address_fields_still_returns_none():
    assert run(RedfinNormalizer().normalize({"redfin_id": "x", "city": "Orlando"})) is None


def test_points_of_interest_category_is_always_none_because_the_source_has_no_category():
    normalized = normalize(places=[{"name": "Publix", "popularity": 0.8, "distance": 0.4}])

    assert normalized["pois"][0]["category"] is None
    assert normalized["pois"][0]["name"] == "Publix"
