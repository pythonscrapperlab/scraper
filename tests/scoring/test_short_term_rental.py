"""
Mid-term / snowbird letting (30+ days) — registered as the `str` strategy.

The tests that matter most here are the ones showing this is NOT the same
strategy as nightly letting: a 55+ community helps here and disqualifies
there, and the regulatory burden is fundamentally different.
"""


from aevorex.scoring.short_term_rental import MidTermRentalScorer
from tests.scoring.conftest import (
    make_amenities,
    make_context,
    make_location_score,
    make_valuation,
)

scorer = MidTermRentalScorer()


def test_registered_as_the_str_strategy():
    assert scorer.STRATEGY_KEY == "str"


# ------------------------------------------------ the snowbird distinction


def test_age_restriction_is_a_positive_here():
    # 55+ communities ARE the snowbird market. The Airbnb scorer treats the
    # identical column as disqualifying, which is the clearest demonstration
    # that folding these two strategies together would destroy information.
    restricted = scorer.score(make_context(
        is_age_restricted=True, location_score=make_location_score()))
    open_market = scorer.score(make_context(
        is_age_restricted=False, location_score=make_location_score()))
    assert restricted.score > open_market.score


def test_quiet_locations_beat_lively_ones():
    quiet = scorer.score(make_context(location_score=make_location_score(
        quiet_score=9.5, wellness_score=6.0, parks_score=8.0,
        nightlife_score=1.0, restaurants_score=4.0)))
    lively = scorer.score(make_context(location_score=make_location_score(
        quiet_score=2.0, wellness_score=3.0, parks_score=3.0,
        nightlife_score=9.5, restaurants_score=9.0)))
    assert quiet.score > lively.score, "a three-month tenant wants calm, not a nightlife strip"


def test_rationale_states_the_30_day_regulatory_position():
    result = scorer.score(make_context(location_score=make_location_score()))
    assert "30+ days" in result.rationale
    assert "vacation rental" in result.rationale.lower()


# ------------------------------------------------------------- economics


def test_revenue_model_accounts_for_the_seasonal_void():
    result = scorer.score(make_context(location_score=make_location_score()))
    f = result.factors
    assert f["occupied_months"] < 12, "snowbird season is not a full year"
    assert f["monthly_rent_mid_term"] > f["monthly_rent_long_term"], "furnished commands a premium"
    assert f["net_annual_income"] < f["gross_seasonal_revenue"], "costs must be deducted"


def test_uplift_versus_an_annual_lease_is_reported():
    # The actual question is not "is this a good property" but "is mid-term
    # better than just letting it annually".
    result = scorer.score(make_context(location_score=make_location_score()))
    assert "uplift_vs_annual_lease_pct" in result.factors


def test_furnishing_cost_is_charged():
    result = scorer.score(make_context(sqft=2000, location_score=make_location_score()))
    assert result.factors["furnishing_cost"] > 0


def test_already_furnished_scores_higher():
    furnished = scorer.score(make_context(
        location_score=make_location_score(),
        amenities=make_amenities(furnished_level="turnkey", furnished_rank=1.0)))
    bare = scorer.score(make_context(
        location_score=make_location_score(),
        amenities=make_amenities(furnished_level="unfurnished", furnished_rank=0.0)))
    assert furnished.score > bare.score


# --------------------------------------------------------------- lease terms


def test_a_minimum_lease_beyond_a_season_is_penalised():
    # A 12-month minimum makes this a long-term rental, not a seasonal one.
    seasonal = scorer.score(make_context(
        location_score=make_location_score(),
        amenities=make_amenities(min_lease_months=3)))
    annual_only = scorer.score(make_context(
        location_score=make_location_score(),
        amenities=make_amenities(min_lease_months=12)))
    assert annual_only.score < seasonal.score
    assert "minimum_lease_exceeds_snowbird_season" in annual_only.data_quality_flags


def test_stated_lease_restrictions_are_flagged_but_not_fatal():
    # At 30+ days most HOA minimums are satisfied, so this is a caution
    # rather than the hard gate it is for nightly letting.
    result = scorer.score(make_context(
        is_rental_restricted=True, location_score=make_location_score()))
    assert result.score is not None and result.score > 0
    assert any("lease_restrictions" in f for f in result.data_quality_flags)


# ---------------------------------------------------------------- refusals


def test_declines_without_a_rent_estimate():
    result = scorer.score(make_context(valuation=make_valuation(rent_estimate_monthly=None)))
    assert result.score is None
    assert "no_rent_estimate_cannot_score_mid_term" in result.data_quality_flags


def test_declines_on_a_placeholder_price():
    result = scorer.score(make_context(price=5_000, price_is_placeholder=True))
    assert result.score is None


def test_modelled_rent_lowers_confidence():
    published = scorer.score(make_context(
        location_score=make_location_score(),
        valuation=make_valuation(rent_method="source_avm", rent_confidence=0.9)))
    inferred = scorer.score(make_context(
        location_score=make_location_score(),
        valuation=make_valuation(rent_method="market_yield_prior", rent_confidence=0.3)))
    assert published.confidence > inferred.confidence
