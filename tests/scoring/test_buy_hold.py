"""
Buy & hold — long-term rental.

The previous version could only score 54% of the portfolio because it needed
the source rent estimate. It also modelled expenses as a flat $1,800 insurance
figure plus 1% maintenance, with nothing for HOA or CDD, which in Florida is
not conservative — it is wrong, and it made a condo with a $700/month HOA look
almost identical to a house without one.
"""


from aevorex.scoring.buy_hold import BuyAndHoldScorer
from tests.scoring.conftest import (
    make_amenities,
    make_context,
    make_location_score,
    make_market,
    make_market_snapshot,
    make_valuation,
)

scorer = BuyAndHoldScorer()


# ------------------------------------------------------------------ returns


def test_higher_cap_rate_scores_higher():
    strong = scorer.score(make_context(valuation=make_valuation(cap_rate=0.085)))
    weak = scorer.score(make_context(valuation=make_valuation(cap_rate=0.025)))
    assert strong.score > weak.score


def test_cap_rate_is_rankable_across_its_range():
    scores = [
        scorer.score(make_context(valuation=make_valuation(cap_rate=r / 1000))).score
        for r in range(20, 90, 5)
    ]
    assert scores == sorted(scores)
    assert len(set(scores)) > 5, "must not collapse into a handful of tiers"


def test_yield_is_also_judged_against_the_local_market():
    # 6% gross is unremarkable where the median is 7.5% and strong where it is 4%.
    rich_market = scorer.score(make_context(
        valuation=make_valuation(gross_yield=0.06),
        market=make_market(median_gross_yield=0.075)))
    thin_market = scorer.score(make_context(
        valuation=make_valuation(gross_yield=0.06),
        market=make_market(median_gross_yield=0.040)))
    assert thin_market.score > rich_market.score


def test_cash_on_cash_is_reported_alongside_the_cap_rate():
    result = scorer.score(make_context(price=300_000))
    f = result.factors
    assert "cash_on_cash_pct" in f
    assert "annual_debt_service" in f
    assert "annual_cash_flow_after_debt" in f


# ----------------------------------------------------------------- expenses


def test_real_expense_lines_are_surfaced():
    result = scorer.score(make_context(valuation=make_valuation(
        annual_taxes=4_000.0, annual_insurance=3_500.0,
        annual_hoa=8_400.0, annual_cdd=1_800.0)))
    f = result.factors
    assert f["annual_hoa"] == 8_400.0
    assert f["annual_cdd"] == 1_800.0
    assert f["annual_insurance"] == 3_500.0
    assert "HOA" in result.rationale


def test_a_heavy_hoa_reduces_the_score_relative_to_an_identical_property_without_one():
    # The exact case a flat expense model could not distinguish.
    with_hoa = scorer.score(make_context(
        valuation=make_valuation(annual_hoa=9_000.0, noi_annual=4_000.0, cap_rate=0.013)))
    without = scorer.score(make_context(
        valuation=make_valuation(annual_hoa=0.0, noi_annual=13_000.0, cap_rate=0.043)))
    assert without.score > with_hoa.score


def test_modelled_insurance_is_described_as_modelled():
    result = scorer.score(make_context(valuation=make_valuation(annual_insurance=3_500.0)))
    assert "modelled" in result.rationale


# ---------------------------------------------------------------- location


def test_school_quality_lifts_the_score():
    good = scorer.score(make_context(location_score=make_location_score(
        primary_schools_score=9.5, high_schools_score=9.0)))
    poor = scorer.score(make_context(location_score=make_location_score(
        primary_schools_score=1.0, high_schools_score=0.5)))
    assert good.score > poor.score
    assert good.factors["school_score_0_10"] > poor.factors["school_score_0_10"]


def test_appreciation_contributes_when_available():
    rising = scorer.score(make_context(
        market_snapshot=make_market_snapshot(yoy_price_change_pct=8.0),
        location_score=make_location_score()))
    falling = scorer.score(make_context(
        market_snapshot=make_market_snapshot(yoy_price_change_pct=-4.0),
        location_score=make_location_score()))
    assert rising.score > falling.score


# ----------------------------------------------------------------- modifiers


def test_tenant_in_place_is_a_positive_here():
    # The opposite of its effect on fix & flip, where it blocks renovation.
    tenanted = scorer.score(make_context(is_tenant_occupied=True))
    vacant = scorer.score(make_context(is_tenant_occupied=False))
    assert tenanted.score > vacant.score


def test_association_approval_is_investor_friction():
    friction = scorer.score(make_context(
        amenities=make_amenities(hoa_approval_required=True)))
    clear = scorer.score(make_context(amenities=make_amenities()))
    assert friction.score < clear.score


# ----------------------------------------------------------------- refusals


def test_declines_without_a_cap_rate():
    result = scorer.score(make_context(valuation=make_valuation(cap_rate=None)))
    assert result.score is None
    assert "no_noi_cannot_score_buy_hold" in result.data_quality_flags


def test_declines_with_no_valuation():
    result = scorer.score(make_context(valuation=None))
    assert result.score is None


def test_declines_on_a_placeholder_price():
    result = scorer.score(make_context(price=5_000, price_is_placeholder=True))
    assert result.score is None


def test_a_rent_prior_lowers_confidence_and_is_disclosed():
    inferred = scorer.score(make_context(valuation=make_valuation(
        rent_method="market_yield_prior", rent_confidence=0.29)))
    published = scorer.score(make_context(valuation=make_valuation(
        rent_method="source_avm", rent_confidence=0.9)))
    assert inferred.confidence < published.confidence
    assert "rent_inferred_from_market_yield_treat_as_provisional" in inferred.data_quality_flags
    assert "indicative" in inferred.rationale
