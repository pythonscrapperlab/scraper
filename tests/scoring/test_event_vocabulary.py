"""
The bridge between what Redfin writes and what the scorers look for.

Persisting "Relisted" rows is only half the fix: the motivated-seller
scorer matched `event == "relisted"` against Redfin's "Relisted", which is
False forever. These tests pin the canonical vocabulary and prove the
scorers read both the new `event_type` slug and legacy rows that only have
the raw wording.
"""

from datetime import datetime

from aevorex.db.event_types import (
    COMING_SOON,
    CONTINGENT,
    DELISTED,
    LISTED,
    LISTED_FOR_RENT,
    PENDING,
    PRICE_CHANGED,
    REDUCED,
    RELISTED,
    REMOVED,
    RENTAL_REMOVED,
    SOLD,
    canonical_event_type,
    resolve_price_change_direction,
)
from aevorex.db.models import PriceHistory
from aevorex.scoring.base import ScoringContext
from aevorex.scoring.config import DEFAULT_CONFIG
from aevorex.scoring.motivated_seller import MotivatedSellerScorer
from aevorex.scoring.utils import (
    count_price_reductions,
    has_off_market_event,
    has_relisted_event,
    original_list_price,
)
from tests.scoring.conftest import make_property

# The 12 event descriptions observed across the live raw_scrapes set.
REDFIN_EVENT_DESCRIPTIONS = {
    "Listed": LISTED,
    "Sold (MLS)": SOLD,
    "Sold (Public Records)": SOLD,
    "Price Changed": PRICE_CHANGED,
    "Listing Removed": REMOVED,
    "Pending": PENDING,
    "Listed for Rent": LISTED_FOR_RENT,
    "Rental Removed": RENTAL_REMOVED,
    "Relisted": RELISTED,
    "Contingent": CONTINGENT,
    "Coming Soon": COMING_SOON,
    "Delisted": DELISTED,
}


def row(event, event_date, price=None, event_type=None):
    return PriceHistory(
        event=event, event_type=event_type, event_date=event_date, price=price, source="redfin"
    )


def test_every_observed_redfin_event_description_maps():
    for description, expected in REDFIN_EVENT_DESCRIPTIONS.items():
        assert canonical_event_type(description) == expected, description


def test_legacy_lowercase_slugs_round_trip():
    for slug in (LISTED, REDUCED, RELISTED, PENDING, SOLD):
        assert canonical_event_type(slug) == slug


def test_unrecognized_wording_returns_none_rather_than_a_guess():
    for value in ("Auction Scheduled", "", "   ", None, 42):
        assert canonical_event_type(value) is None


def test_price_change_direction_needs_a_baseline():
    assert resolve_price_change_direction(PRICE_CHANGED, 300_000, 350_000) == REDUCED
    assert resolve_price_change_direction(PRICE_CHANGED, 400_000, 350_000) == "increased"
    assert resolve_price_change_direction(PRICE_CHANGED, 350_000, 350_000) == PRICE_CHANGED
    assert resolve_price_change_direction(PRICE_CHANGED, None, 350_000) == PRICE_CHANGED
    assert resolve_price_change_direction(PRICE_CHANGED, 300_000, None) == PRICE_CHANGED
    # Non-price-change types pass straight through.
    assert resolve_price_change_direction(RELISTED, 300_000, 350_000) == RELISTED


def test_relisted_is_detected_from_redfins_own_wording():
    history = [row("Relisted", datetime(2026, 4, 1), event_type=RELISTED)]

    assert has_relisted_event(history) is True


def test_relisted_is_detected_on_legacy_rows_with_no_event_type():
    history = [row("Relisted", datetime(2026, 4, 1))]

    assert has_relisted_event(history) is True


def test_reductions_count_from_event_type():
    history = [
        row("Listed", datetime(2026, 1, 5), price=450_000, event_type=LISTED),
        row("Price Changed", datetime(2026, 2, 5), price=440_000, event_type=REDUCED),
        row("Price Changed", datetime(2026, 3, 5), price=425_000, event_type=REDUCED),
        row("Price Changed", datetime(2026, 4, 5), price=430_000, event_type="increased"),
    ]

    assert count_price_reductions(history) == 2


def test_undirected_price_changes_are_not_counted_as_cuts():
    history = [row("Price Changed", datetime(2026, 2, 5), price=440_000, event_type=PRICE_CHANGED)]

    assert count_price_reductions(history) == 0


def test_original_list_price_ignores_unpriced_events():
    history = [
        row("Listing Removed", datetime(2026, 3, 1), event_type=REMOVED),
        row("Listed", datetime(2026, 1, 5), price=450_000, event_type=LISTED),
    ]

    assert original_list_price(history) == 450_000


def test_original_list_price_falls_back_to_the_first_priced_event():
    history = [
        row("Pending", datetime(2026, 3, 1), event_type=PENDING),
        row("Sold (MLS)", datetime(2026, 2, 1), price=390_000, event_type=SOLD),
    ]

    assert original_list_price(history) == 390_000


def test_original_list_price_of_an_entirely_unpriced_history_is_none():
    history = [row("Listing Removed", datetime(2026, 3, 1), event_type=REMOVED)]

    assert original_list_price(history) is None


def test_off_market_events_are_detectable():
    assert has_off_market_event([row("Listing Removed", datetime(2026, 3, 1), event_type=REMOVED)]) is True
    assert has_off_market_event([row("Delisted", datetime(2026, 3, 1), event_type=DELISTED)]) is True
    assert has_off_market_event([row("Pending", datetime(2026, 3, 1), event_type=PENDING)]) is False


def test_motivated_seller_relisted_factor_is_no_longer_dead():
    prop = make_property(days_on_market=120, price=425_000, description=None)
    history = [
        row("Listed", datetime(2026, 1, 5), price=450_000, event_type=LISTED),
        row("Listing Removed", datetime(2026, 2, 1), event_type=REMOVED),
        row("Relisted", datetime(2026, 3, 1), event_type=RELISTED),
        row("Price Changed", datetime(2026, 4, 1), price=425_000, event_type=REDUCED),
    ]
    ctx = ScoringContext(
        property=prop, price_history=history, tax_history=[], comps=[],
        location_score=None, features=None, market_snapshot=None, config=DEFAULT_CONFIG,
    )

    result = MotivatedSellerScorer().score(ctx)

    assert result.factors["relisted"] is True
    assert result.factors["price_reduction_count"] == 1
    assert result.factors["original_list_price"] == 450_000
    # This history contains BOTH a withdrawal and a relist. The rationale
    # reports the withdrawal, because "listed, failed to sell, pulled from the
    # market" is the stronger statement of the two and repeating both would
    # just be the same story told twice.
    assert result.factors["previously_withdrawn"] is True
    assert "withdrawn without selling" in result.rationale
