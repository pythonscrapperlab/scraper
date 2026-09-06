"""
Factories for building ScoringContext fixtures without a live DB.

Note the 0-10 scale on location scores — that is the source's actual scale
(verified: every one of the 14 dimensions ranges 0-10 with a real spread).
The previous fixtures used 0-100 values, which meant the STR and buy-hold
tests were exercising the scorers with inputs an order of magnitude larger
than anything production ever sees.
"""

from datetime import datetime, timedelta

import pytest

from aevorex.db.models import (
    LocationScore,
    MarketSnapshot,
    PriceHistory,
    Property,
    PropertyComp,
    PropertyFeature,
    PropertyValuation,
    TaxHistory,
)
from aevorex.scoring.base import ScoringContext
from aevorex.scoring.config import DEFAULT_CONFIG


def make_property(**overrides) -> Property:
    defaults = dict(
        address="123 Main St",
        city="Orlando",
        state="FL",
        zip_code="32801",
        county="Orange",
        price=300_000,
        bedrooms=3,
        bathrooms=2.0,
        sqft=1500,
        lot_size=0.2,
        year_built=2000,
        property_type="Single Family Residential",
        listing_status="Active",
        listing_status_normalized="active",
        days_on_market=30,
        description=None,
        ai_summary=None,
        meta={},
        primary_source="redfin",
        price_is_placeholder=False,
    )
    defaults.update(overrides)
    return Property(**defaults)


def make_valuation(**overrides) -> PropertyValuation:
    """A healthy, fully-populated valuation. Override to test the gaps."""
    defaults = dict(
        market_value=310_000.0,
        market_value_method="comps",
        arv=360_000.0,
        arv_method="comps_p75",
        price_to_value_ratio=0.97,
        comp_count=6,
        comp_median_ppsf=207.0,
        comp_p75_ppsf=240.0,
        comp_dispersion_cv=0.12,
        comp_median_age_days=90,
        valuation_confidence=0.7,
        rehab_cost_low=21_000.0,
        rehab_cost_mid=30_000.0,
        rehab_cost_high=43_500.0,
        rehab_basis={"base_psf_basis": "condition:standard", "base_psf": 22.0},
        condition_class="standard",
        rent_estimate_monthly=2_200.0,
        rent_method="source_avm",
        rent_confidence=0.9,
        gross_yield=0.088,
        annual_taxes=3_400.0,
        annual_insurance=3_100.0,
        annual_hoa=0.0,
        annual_cdd=0.0,
        annual_maintenance=3_100.0,
        annual_operating_expenses=9_600.0,
        noi_annual=12_600.0,
        cap_rate=0.042,
        max_allowable_offer=210_000.0,
        valuation_version="v1",
    )
    defaults.update(overrides)
    return PropertyValuation(**defaults)


def make_market(**overrides) -> dict:
    """A resolved market_stats row, as `resolve_market` returns it."""
    defaults = dict(
        geo_level="zip",
        geo_key="32801",
        property_class="all",
        median_sold_ppsf=205.0,
        p25_sold_ppsf=170.0,
        p75_sold_ppsf=250.0,
        median_sold_price=295_000,
        sold_count=80,
        median_list_ppsf=210.0,
        median_list_price=305_000,
        median_dom=22.0,
        p75_dom=55.0,
        pct_listings_with_cut=0.40,
        median_gross_yield=0.075,
        median_tax_rate=0.0113,
        median_hoa_monthly=180.0,
        confidence=1.0,
    )
    defaults.update(overrides)
    return defaults


def make_amenities(**overrides) -> dict:
    """Parsed amenity fields. Everything None unless overridden, as in life."""
    from aevorex.normalizers.amenities import AMENITY_FIELDS

    base = {field: None for field in AMENITY_FIELDS}
    base.update(overrides)
    return base


def make_price_history_event(
    price, event: str, event_date: datetime,
    event_type=None, source: str = "redfin", is_rental_event: bool = False,
) -> PriceHistory:
    return PriceHistory(
        price=price, event=event, event_date=event_date, event_type=event_type,
        source=source, is_rental_event=is_rental_event,
    )


def days_ago(n: int) -> datetime:
    return datetime.now() - timedelta(days=n)


def make_tax_row(tax_year: int, tax_amount=None, assessed_value=None,
                 source: str = "redfin") -> TaxHistory:
    return TaxHistory(tax_year=tax_year, tax_amount=tax_amount,
                      assessed_value=assessed_value, source=source)


def make_comp(price=None, sqft=None, comp_address=None, bedrooms=None,
              bathrooms=None, sold_date=None, source: str = "redfin") -> PropertyComp:
    return PropertyComp(
        comp_address=comp_address, price=price, sqft=sqft, bedrooms=bedrooms,
        bathrooms=bathrooms, sold_date=sold_date or days_ago(90), source=source,
    )


def make_location_score(**overrides) -> LocationScore:
    """0-10 scale, which is what the source actually emits."""
    defaults = dict(
        source="redfin",
        pedestrian_score=4.7,
        transit_score=2.3,
        nightlife_score=4.4,
        restaurants_score=6.1,
        cafes_score=3.5,
        shopping_score=5.6,
        groceries_score=6.7,
        vibrant_score=2.1,
        parks_score=6.0,
        quiet_score=8.7,
        wellness_score=3.0,
        daycares_score=7.9,
        primary_schools_score=5.9,
        high_schools_score=2.7,
    )
    defaults.update(overrides)
    return LocationScore(**defaults)


def make_market_snapshot(**overrides) -> MarketSnapshot:
    defaults = dict(
        zip_code="32801",
        source="redfin",
        snapshot_date=datetime(2026, 1, 1).date(),
        median_list_price=300_000,
        median_sale_price=295_000,
        avg_days_on_market=20.0,
        sale_to_list_pct=98.0,
        yoy_price_change_pct=4.0,
        price_drop_pct=None,
    )
    defaults.update(overrides)
    return MarketSnapshot(**defaults)


# Distinguishes "caller didn't specify, give them the healthy default" from
# "caller explicitly passed None to exercise the missing-data path".
USE_DEFAULT = object()


def make_context(
    config=DEFAULT_CONFIG,
    price_history=None,
    tax_history=None,
    comps=None,
    location_score=None,
    features=None,
    market_snapshot=None,
    valuation=USE_DEFAULT,
    market=USE_DEFAULT,
    amenities=None,
    **prop_overrides,
) -> ScoringContext:
    """
    Build a context. `valuation` and `market` default to healthy fixtures;
    pass None explicitly to test the missing-data paths.
    """
    return ScoringContext(
        property=make_property(**prop_overrides),
        price_history=price_history or [],
        tax_history=tax_history or [],
        comps=comps or [],
        location_score=location_score,
        features=features,
        market_snapshot=market_snapshot,
        valuation=make_valuation() if valuation is USE_DEFAULT else valuation,
        market=make_market() if market is USE_DEFAULT else market,
        amenities=amenities if amenities is not None else make_amenities(),
        config=config,
    )


@pytest.fixture
def property_factory():
    return make_property


@pytest.fixture
def context_factory():
    return make_context
