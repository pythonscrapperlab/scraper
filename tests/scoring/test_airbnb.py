"""
Nightly vacation letting (under 30 days).

The regulatory gate is the most important thing to get right here — a gate
that can be out-voted by a high enough demand score is not a gate, and would
send an investor at a property they legally cannot operate.
"""

import pytest

from aevorex.scoring.airbnb import PROXY_FLAG, UNVERIFIED_FLAG, AirbnbScorer
from aevorex.scoring.config import AirbnbConfig, ScoringConfig
from tests.scoring.conftest import (
    make_amenities,
    make_context,
    make_location_score,
    make_valuation,
)

scorer = AirbnbScorer()


def prime_location():
    """The Miami Beach profile: nightlife 9, quiet 4."""
    return make_location_score(
        restaurants_score=9.0, nightlife_score=9.0, vibrant_score=8.0,
        cafes_score=8.0, shopping_score=8.0, pedestrian_score=9.0, quiet_score=3.0,
    )


# ------------------------------------------------------- the regulatory gate


def test_age_restricted_community_is_gated_regardless_of_everything_else():
    # The property is otherwise perfect. It must still not rank.
    result = scorer.score(make_context(
        is_age_restricted=True,
        location_score=prime_location(),
        amenities=make_amenities(has_private_pool=True, is_waterfront=True,
                                 furnished_level="turnkey", furnished_rank=1.0),
        bedrooms=5,
    ))
    assert result.score <= 15, "a 55+ community cannot be let nightly, however good it looks"
    assert result.factors["regulatory_verdict"] == "prohibited_age_restricted"


def test_hoa_restriction_caps_the_score():
    result = scorer.score(make_context(
        is_rental_restricted=True,
        location_score=prime_location(),
        amenities=make_amenities(has_private_pool=True, is_waterfront=True),
        bedrooms=4,
    ))
    assert result.score <= 30
    assert result.factors["regulatory_verdict"] == "restricted_by_hoa"


def test_a_stated_minimum_lease_precludes_nightly_letting():
    result = scorer.score(make_context(
        location_score=prime_location(),
        amenities=make_amenities(min_lease_months=3, has_private_pool=True),
    ))
    assert result.factors["regulatory_verdict"] == "restricted_minimum_lease"
    assert result.score <= 30


def test_unverified_is_the_default_and_caps_below_full_marks():
    # The municipal table ships empty on purpose; nothing should read as
    # fully cleared until someone populates it from actual ordinances.
    result = scorer.score(make_context(
        location_score=prime_location(),
        amenities=make_amenities(has_private_pool=True, is_waterfront=True),
        bedrooms=5,
    ))
    assert result.factors["regulatory_verdict"] == "unverified"
    assert UNVERIFIED_FLAG in result.data_quality_flags
    assert result.score < 100


def test_a_prohibiting_municipality_gates_hard():
    config = ScoringConfig(airbnb=AirbnbConfig(
        municipal_rules={"orlando,FL": "prohibited"}))
    result = scorer.score(make_context(
        config=config, city="Orlando", state="FL",
        location_score=prime_location(),
        amenities=make_amenities(has_private_pool=True), bedrooms=4,
    ))
    assert result.score <= 10
    assert result.factors["regulatory_verdict"] == "prohibited_by_municipality"


def test_a_permitting_municipality_removes_the_cap():
    config = ScoringConfig(airbnb=AirbnbConfig(
        municipal_rules={"orlando,FL": "permitted"}))
    permitted = scorer.score(make_context(
        config=config, city="Orlando", state="FL",
        location_score=prime_location(),
        amenities=make_amenities(has_private_pool=True), bedrooms=4,
    ))
    unverified = scorer.score(make_context(
        city="Orlando", state="FL",
        location_score=prime_location(),
        amenities=make_amenities(has_private_pool=True), bedrooms=4,
    ))
    assert permitted.factors["regulatory_score_cap"] is None
    assert permitted.score > unverified.score


def test_a_gate_never_raises_a_weak_score():
    weak = scorer.score(make_context(
        location_score=make_location_score(
            restaurants_score=1.0, nightlife_score=0.5, vibrant_score=0.5,
            cafes_score=0.5, shopping_score=1.0, pedestrian_score=1.0),
        bedrooms=1,
    ))
    assert weak.score < 50


# ----------------------------------------------------- demand and property


def test_lively_locations_beat_quiet_ones_the_inverse_of_mid_term():
    lively = scorer.score(make_context(location_score=prime_location()))
    sleepy = scorer.score(make_context(location_score=make_location_score(
        restaurants_score=3.0, nightlife_score=1.0, vibrant_score=1.0,
        cafes_score=1.5, shopping_score=2.0, pedestrian_score=2.0, quiet_score=9.5)))
    assert lively.score > sleepy.score


def test_a_private_pool_counts_for_far_more_than_a_shared_one():
    private = scorer.score(make_context(
        location_score=prime_location(),
        amenities=make_amenities(has_private_pool=True, has_community_pool=False)))
    community = scorer.score(make_context(
        location_score=prime_location(),
        amenities=make_amenities(has_private_pool=False, has_community_pool=True)))
    assert private.score > community.score, (
        "an association pool is not a booking driver the way a private one is"
    )


def test_waterfront_adds_demand():
    waterfront = scorer.score(make_context(
        location_score=prime_location(), amenities=make_amenities(is_waterfront=True)))
    inland = scorer.score(make_context(
        location_score=prime_location(), amenities=make_amenities(is_waterfront=False)))
    assert waterfront.score > inland.score


def test_more_bedrooms_raise_the_score():
    small = scorer.score(make_context(bedrooms=1, location_score=prime_location()))
    large = scorer.score(make_context(bedrooms=5, location_score=prime_location()))
    assert large.score > small.score


# ----------------------------------------------------------- honest limits


def test_revenue_is_always_labelled_a_proxy():
    result = scorer.score(make_context(location_score=prime_location()))
    assert PROXY_FLAG in result.data_quality_flags
    assert "proxy" in result.rationale.lower()


def test_proxy_economics_are_exposed_for_inspection():
    result = scorer.score(make_context(location_score=prime_location()))
    f = result.factors
    for key in ("proxy_nightly_rate", "proxy_occupancy",
                "proxy_gross_annual_revenue", "proxy_net_annual_income"):
        assert key in f
    assert 0.3 < f["proxy_occupancy"] < 0.9, "occupancy assumption must stay plausible"


def test_nightly_management_costs_far_more_than_long_term():
    result = scorer.score(make_context(location_score=prime_location()))
    gross = result.factors["proxy_gross_annual_revenue"]
    management = result.factors["management_and_cleaning"]
    assert management / gross > 0.2, "cleaning, platform fees and turnover are not 9%"


def test_declines_without_a_rent_baseline():
    result = scorer.score(make_context(valuation=make_valuation(rent_estimate_monthly=None)))
    assert result.score is None
    assert "no_rent_estimate_cannot_score_airbnb" in result.data_quality_flags


def test_declines_on_a_placeholder_price():
    result = scorer.score(make_context(price=5_000, price_is_placeholder=True))
    assert result.score is None


def test_unverified_regulation_reduces_confidence():
    config = ScoringConfig(airbnb=AirbnbConfig(municipal_rules={"orlando,FL": "permitted"}))
    known = scorer.score(make_context(
        config=config, city="Orlando", state="FL", location_score=prime_location()))
    unknown = scorer.score(make_context(
        city="Orlando", state="FL", location_score=prime_location()))
    assert known.confidence > unknown.confidence
