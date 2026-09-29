"""
Regression tests for the ingestion bugs found in the 2026-08-18 data audit.

Each test names the specific wrong number it prevents from coming back.
"""

from datetime import datetime

from aevorex.db.deduplicator import Deduplicator
from aevorex.db.event_types import INCREASED, PRICE_CHANGED, REDUCED
from aevorex.db.models import Property
from aevorex.normalizers.redfin import RedfinNormalizer


def ev(event, day, price=None, event_type=None):
    return {
        "event": event,
        "event_type": event_type,
        "event_date": datetime(2026, 1, day),
        "price": price,
    }


# --------------------------------------------- rental / sale price mixing


def test_price_change_after_a_rental_event_is_not_measured_against_rent():
    # The bug: one running `previous_price` across both markets compared a
    # $4,800 rent against a $700,000 list price and called it `increased`.
    rows = RedfinNormalizer._resolve_price_change_directions([
        ev("Listed", 1, 700_000, "listed"),
        ev("Listed for Rent", 2, 4_800, "listed_for_rent"),
        ev("Rental Removed", 3, None, "rental_removed"),
        ev("Listed", 4, 700_000, "listed"),
        ev("Price Changed", 5, 675_000, PRICE_CHANGED),
    ])

    price_change = next(r for r in rows if r["event"] == "Price Changed")
    assert price_change["event_type"] == REDUCED
    assert price_change["is_rental_event"] is False


def test_a_rent_change_inside_a_rental_cycle_is_tagged_as_rental():
    rows = RedfinNormalizer._resolve_price_change_directions([
        ev("Listed", 1, 700_000, "listed"),
        ev("Listed for Rent", 2, 4_800, "listed_for_rent"),
        ev("Price Changed", 3, 4_500, PRICE_CHANGED),
    ])

    rent_change = next(r for r in rows if r["event"] == "Price Changed")
    assert rent_change["is_rental_event"] is True, "must not pollute sale-price math"
    assert rent_change["event_type"] == REDUCED, "still a real rent cut, measured against the rent"


def test_rent_and_sale_chains_are_tracked_independently():
    rows = RedfinNormalizer._resolve_price_change_directions([
        ev("Listed", 1, 500_000, "listed"),
        ev("Listed for Rent", 2, 3_000, "listed_for_rent"),
        ev("Price Changed", 3, 3_400, PRICE_CHANGED),   # rent went up
        ev("Listed", 4, 500_000, "listed"),
        ev("Price Changed", 5, 460_000, PRICE_CHANGED),  # price came down
    ])

    by_day = {r["event_date"].day: r for r in rows}
    assert by_day[3]["event_type"] == INCREASED and by_day[3]["is_rental_event"] is True
    assert by_day[5]["event_type"] == REDUCED and by_day[5]["is_rental_event"] is False


def test_unresolvable_price_change_stays_directionless():
    rows = RedfinNormalizer._resolve_price_change_directions([ev("Price Changed", 1, 400_000, PRICE_CHANGED)])

    assert rows[0]["event_type"] == PRICE_CHANGED, "never assume a cut with nothing to compare against"


# ------------------------------------------------------------ price sanity


def test_int32_max_sentinel_is_rejected():
    # Two live events carry exactly this against a $330,000 house; taken at
    # face value it reads as a 650,000% price increase.
    assert RedfinNormalizer._sane_price(2_147_483_647) is None


def test_implausible_and_nonpositive_prices_are_rejected():
    assert RedfinNormalizer._sane_price(0) is None
    assert RedfinNormalizer._sane_price(-5) is None
    assert RedfinNormalizer._sane_price(900_000_000) is None


def test_real_prices_survive_including_the_largest_in_the_live_set():
    assert RedfinNormalizer._sane_price(35_995_000) == 35_995_000
    assert RedfinNormalizer._sane_price("450,000") == 450_000


# ------------------------------------------------------- assessed value sum


def test_assessed_value_needs_both_components():
    rows = RedfinNormalizer._extract_tax_history({"tax_history": [
        {"tax_year": 2025, "tax_annual": 6976.88,
         "tax_able_land_value": None, "tax_able_improvement_value": 445_000},
    ]})

    # The bug: this recorded 445,000 as the whole assessment, understated by
    # the entire land component (20-40% of value in Florida).
    assert rows[0]["assessed_value"] is None
    assert rows[0]["improvement_value"] == 445_000
    assert rows[0]["land_value"] is None


def test_assessed_value_is_summed_when_both_components_are_present():
    rows = RedfinNormalizer._extract_tax_history({"tax_history": [
        {"tax_year": 2025, "tax_annual": 10_200,
         "tax_able_land_value": 204_000, "tax_able_improvement_value": 351_660},
    ]})

    assert rows[0]["assessed_value"] == 555_660


# ------------------------------------------------------- placeholder prices


def test_foreclosure_deposit_is_flagged_as_a_placeholder():
    is_placeholder, flags = RedfinNormalizer._detect_placeholder_price(
        price=5_000, sqft=1_956, avm_value=None,
        tax_history=[{"tax_year": 2025, "assessed_value": 555_660}],
        is_auction=True,
    )

    assert is_placeholder is True
    assert "price_under_10pct_of_assessed_value" in flags
    assert "price_is_auction_deposit_or_opening_bid" in flags


def test_a_genuine_bargain_is_not_flagged():
    # A real fire-sale teardown at 35% of assessed value must survive — this
    # guard exists to catch $5,000 on a $556,000 parcel, not deep discounts.
    is_placeholder, _ = RedfinNormalizer._detect_placeholder_price(
        price=90_000, sqft=1_100, avm_value=250_000,
        tax_history=[{"tax_year": 2025, "assessed_value": 255_000}],
        is_auction=False,
    )

    assert is_placeholder is False


def test_unverifiable_price_is_flagged_but_not_condemned():
    is_placeholder, flags = RedfinNormalizer._detect_placeholder_price(
        price=5_000, sqft=None, avm_value=None, tax_history=[], is_auction=False,
    )

    assert is_placeholder is False, "no reference value means unknown, not wrong"
    assert "price_not_verifiable_no_reference_value" in flags


def test_missing_price_yields_none():
    assert RedfinNormalizer._detect_placeholder_price(
        price=None, sqft=1000, avm_value=None, tax_history=[], is_auction=False,
    ) == (None, [])


# ------------------------------------------------- source count / variance


def _prop(**kw):
    return Property(address="1 Main St", city="Orlando", state="FL",
                    zip_code="32801", primary_source="redfin", **kw)


def test_source_count_is_derived_from_platform_ids_not_rescrapes():
    # The bug: `min(source_count + 1, 3)` on every upsert meant a single-source
    # database reported 1,190 properties with three-source confirmation.
    assert Deduplicator._count_sources(_prop(redfin_id="1")) == 1
    assert Deduplicator._count_sources(_prop(redfin_id="1", zillow_id="2")) == 2
    assert Deduplicator._count_sources(_prop(redfin_id="1", zillow_id="2", realtor_id="3")) == 3


def test_variance_is_null_for_a_single_source_not_zero():
    # 0.0 asserts "every source agrees", which was being claimed across a
    # database that only ever had one source.
    assert Deduplicator.calculate_price_variance([500_000]) is None
    assert Deduplicator.calculate_price_variance([]) is None
    assert Deduplicator.calculate_price_variance([500_000, 500_000]) == 0.0
    assert Deduplicator.calculate_price_variance([500_000, 525_000]) == 5.0


def test_primary_source_follows_priority_not_scrape_order():
    assert Deduplicator._pick_primary_source("redfin", "zillow") == "redfin"
    assert Deduplicator._pick_primary_source("redfin", "realtor") == "realtor"
    assert Deduplicator._pick_primary_source("zillow", "redfin") == "redfin"
    assert Deduplicator._pick_primary_source(None, "zillow") == "zillow"


def test_source_prices_map_survives_junk():
    assert Deduplicator._source_prices(
        {"source_prices": {"redfin": "630000", "bogus": 1, "zillow": None, "realtor": 0}}
    ) == {"redfin": 630_000}
    assert Deduplicator._source_prices(None) == {}
    assert Deduplicator._source_prices({"source_prices": "not a dict"}) == {}
