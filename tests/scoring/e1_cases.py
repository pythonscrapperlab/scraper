"""Deterministic synthetic market used by E1 score-freeze tests."""

import random
from collections.abc import Iterator

from tests.scoring.conftest import (
    make_amenities,
    make_context,
    make_location_score,
    make_market,
    make_market_snapshot,
    make_valuation,
)


def fixture_market(size: int, seed: int = 20260930) -> Iterator:
    """Yield varied, production-shaped contexts without wall-clock-dependent events."""
    rng = random.Random(seed)
    descriptions = [
        None,
        "Well maintained home near shops.",
        "Investor special, needs TLC and a quick close.",
        "Motivated seller says bring all offers.",
    ]
    for index in range(size):
        price = rng.randint(145_000, 1_050_000)
        market_value = price * rng.uniform(0.82, 1.28)
        rent = rng.uniform(1_350, 6_200)
        gross_yield = rent * 12 / price
        operating = rng.uniform(7_000, 24_000)
        noi = rent * 12 * 0.92 - operating
        amenities = make_amenities(
            has_private_pool=rng.random() < 0.18,
            has_community_pool=rng.random() < 0.28,
            is_waterfront=rng.random() < 0.10,
            has_water_view=rng.random() < 0.16,
            has_spa=rng.random() < 0.12,
            furnished_rank=rng.choice([None, None, 0.5, 1.0]),
            furnished_level=rng.choice([None, "unfurnished", "partially_furnished", "furnished"]),
            hoa_approval_required=rng.random() < 0.20,
            lease_restricted=rng.random() < 0.16,
            min_lease_months=rng.choice([None, None, 1, 3, 7, 12]),
            pets_allowed=rng.choice([None, True, False]),
            construction_class=rng.choice([None, "masonry", "frame"]),
            roof_class=rng.choice([None, "gable", "flat"]),
            has_impact_glazing=rng.choice([None, True, False]),
        )
        valuation = make_valuation(
            market_value=market_value,
            arv=market_value * rng.uniform(1.02, 1.32),
            valuation_confidence=rng.uniform(0.35, 0.98),
            rehab_cost_mid=rng.uniform(18_000, 110_000),
            comp_dispersion_cv=rng.uniform(0.05, 0.48),
            rent_estimate_monthly=rent,
            rent_confidence=rng.uniform(0.35, 1.0),
            gross_yield=gross_yield,
            annual_operating_expenses=operating,
            noi_annual=noi,
            cap_rate=noi / price,
            rent_method=rng.choice(["source_avm", "rent_per_bed", "market_yield_prior"]),
        )
        location = None if index % 7 == 0 else make_location_score(**{
            name: rng.uniform(0, 10)
            for name in (
                "pedestrian_score", "nightlife_score", "restaurants_score", "cafes_score",
                "shopping_score", "groceries_score", "vibrant_score", "parks_score",
                "quiet_score", "wellness_score", "primary_schools_score", "high_schools_score",
            )
        })
        snapshot = None if index % 5 == 0 else make_market_snapshot(
            yoy_price_change_pct=rng.uniform(-8, 12)
        )
        yield make_context(
            valuation=valuation,
            market=make_market(
                median_dom=rng.uniform(8, 65),
                median_gross_yield=rng.uniform(0.035, 0.11),
            ),
            amenities=amenities,
            location_score=location,
            market_snapshot=snapshot,
            price=price,
            bedrooms=rng.randint(0, 6),
            sqft=rng.randint(550, 4_200),
            days_on_market=rng.randint(0, 360),
            description=rng.choice(descriptions),
            is_reo=rng.random() < 0.04,
            is_probate_or_estate=rng.random() < 0.05,
            is_short_sale=rng.random() < 0.03,
            is_foreclosure=rng.random() < 0.04,
            is_auction=rng.random() < 0.02,
            is_vacant=rng.random() < 0.16,
            is_tenant_occupied=rng.random() < 0.18,
            is_age_restricted=rng.random() < 0.10,
            is_rental_restricted=rng.random() < 0.12,
            allows_short_term_rental=rng.random() < 0.08,
            year_built=rng.randint(1920, 2026),
            flood_factor=rng.choice([None, 1, 3, 5, 8, 10]),
        )
