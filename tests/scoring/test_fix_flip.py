"""Fix & flip: a full underwrite, and a rankable one."""

import pytest

from aevorex.scoring.fix_flip import FixAndFlipScorer
from tests.scoring.conftest import make_amenities, make_context, make_valuation

scorer = FixAndFlipScorer()


# ------------------------------------------------------------ rankability


def test_the_top_decile_is_rankable_not_a_wall_of_hundreds():
    # The defect this replaces: p90=0.7 and p99=100.0, with nothing between.
    scores = []
    for price in range(200_000, 340_000, 10_000):
        result = scorer.score(make_context(
            price=price, valuation=make_valuation(arv=400_000.0, rehab_cost_mid=30_000.0)))
        scores.append(result.score)

    assert all(a > b for a, b in zip(scores, scores[1:])), "cheaper must always score higher"
    assert len(set(scores)) == len(scores), "no two prices may collapse to one score"


def test_a_better_deal_always_outscores_a_worse_one():
    good = scorer.score(make_context(
        price=200_000, valuation=make_valuation(arv=400_000.0, rehab_cost_mid=30_000.0)))
    poor = scorer.score(make_context(
        price=380_000, valuation=make_valuation(arv=400_000.0, rehab_cost_mid=30_000.0)))
    assert good.score > poor.score


# --------------------------------------------------------------- the maths


def test_underwrite_includes_financing_and_holding_costs():
    # The old model omitted both entirely, which made mediocre deals look viable.
    result = scorer.score(make_context(price=250_000))
    f = result.factors
    assert f["financing_costs"] > 0, "hard money is not free"
    assert f["holding_costs"] > 0, "six months of taxes, insurance and utilities"
    assert f["selling_costs"] > 0
    assert f["purchase_closing_costs"] > 0
    assert f["total_cost"] > f["purchase_price"] + f["rehab_cost_mid"]


def test_roi_is_on_cash_invested_not_total_project_cost():
    result = scorer.score(make_context(price=250_000))
    f = result.factors
    assert f["cash_invested"] < f["total_cost"], "leverage is the point of hard money"
    expected = f["projected_profit"] / f["cash_invested"] * 100
    assert f["roi_pct"] == pytest.approx(expected, abs=0.1)


def test_annualised_roi_scales_from_the_hold_period():
    result = scorer.score(make_context(price=250_000))
    f = result.factors
    assert f["annualised_roi_pct"] == pytest.approx(f["roi_pct"] * 12 / f["hold_months"], abs=0.1)


def test_max_allowable_offer_is_produced_and_below_arv():
    result = scorer.score(make_context(price=250_000))
    mao = result.factors["max_allowable_offer"]
    assert mao is not None
    assert mao < result.factors["arv"]
    assert "Maximum sensible offer" in result.rationale


def test_holding_cost_reflects_this_property_not_a_flat_rule():
    # A condo with a $700/month HOA carries far more over six months than a
    # house without one, and the flat 70% rule hides that.
    with_hoa = scorer.score(make_context(
        valuation=make_valuation(annual_hoa=8_400.0)))
    without = scorer.score(make_context(valuation=make_valuation(annual_hoa=0.0)))
    assert with_hoa.factors["holding_costs"] > without.factors["holding_costs"]
    assert with_hoa.factors["max_allowable_offer"] < without.factors["max_allowable_offer"]


# ------------------------------------------------------------ uncertainty


def test_wide_comp_dispersion_discounts_arv_not_just_confidence():
    tight = scorer.score(make_context(
        price=250_000, valuation=make_valuation(comp_dispersion_cv=0.08)))
    wide = scorer.score(make_context(
        price=250_000, valuation=make_valuation(comp_dispersion_cv=0.45)))
    assert wide.factors["arv"] < tight.factors["arv"], (
        "an uncertain exit is worth less than a certain one of the same expected value"
    )
    assert wide.score < tight.score


def test_low_valuation_confidence_pulls_the_score_toward_the_middle():
    confident = scorer.score(make_context(
        price=200_000, valuation=make_valuation(valuation_confidence=0.9)))
    speculative = scorer.score(make_context(
        price=200_000, valuation=make_valuation(valuation_confidence=0.1)))
    assert confident.score > speculative.score


# ------------------------------------------------------ Florida specifics


def test_frame_construction_and_old_roof_penalise_exit_liquidity():
    sound = scorer.score(make_context(
        price=220_000, year_built=2015,
        amenities=make_amenities(construction_class="masonry", roof_class="premium")))
    risky = scorer.score(make_context(
        price=220_000, year_built=1975, flood_factor=9,
        amenities=make_amenities(construction_class="frame", roof_class="flat",
                                 has_impact_glazing=False)))
    assert risky.score < sound.score
    assert "insurability_risks" in risky.factors
    assert "insurability_may_limit_resale_buyer_pool" in risky.data_quality_flags


def test_insurability_penalty_is_capped():
    # Even a property failing every proxy must not be penalised out of
    # existence on proxies alone.
    worst = scorer.score(make_context(
        price=150_000, year_built=1950, flood_factor=10,
        amenities=make_amenities(construction_class="frame", roof_class="flat",
                                 has_impact_glazing=False)))
    assert worst.score is not None and worst.score > 0


# ---------------------------------------------------------------- refusals


def test_declines_without_an_arv():
    result = scorer.score(make_context(valuation=make_valuation(arv=None)))
    assert result.score is None
    assert "no_arv_cannot_underwrite" in result.data_quality_flags


def test_declines_without_a_rehab_estimate():
    result = scorer.score(make_context(valuation=make_valuation(rehab_cost_mid=None)))
    assert result.score is None


def test_declines_on_a_placeholder_price():
    result = scorer.score(make_context(price=5_000, price_is_placeholder=True))
    assert result.score is None


def test_declines_with_no_valuation_at_all():
    result = scorer.score(make_context(valuation=None))
    assert result.score is None


# ---------------------------------------------------------------- honesty


def test_rationale_states_the_estimate_is_not_a_bid():
    result = scorer.score(make_context(price=250_000))
    assert "not a contractor bid" in result.rationale


def test_loss_making_deals_are_described_as_such():
    result = scorer.score(make_context(
        price=395_000, valuation=make_valuation(arv=400_000.0, rehab_cost_mid=60_000.0)))
    assert result.factors["projected_profit"] < 0
    assert "LOSS" in result.rationale


# ------------------------------------------- auction opening bids (F-live-1)


def test_an_auction_opening_bid_is_not_underwritten():
    # Found in live output: a $60,000 opening bid on a $205,000 Orlando house
    # produced a 968% "return" and ranked #1 on this strategy. The price paid
    # at auction is settled in the room, so there is no purchase price to
    # underwrite against.
    result = scorer.score(make_context(
        price=60_000,
        valuation=make_valuation(
            market_value=204_780.0, arv=355_785.0,
            data_quality_flags=["auction_price_is_an_opening_bid_not_a_purchase_price"],
        ),
    ))
    assert result.score is None
    assert "auction_price_is_an_opening_bid_not_a_purchase_price" in result.data_quality_flags
    assert "opening bid" in result.rationale


def test_a_normal_discounted_listing_is_still_underwritten():
    # The guard must not swallow genuine below-market listings that are not
    # auctions — those are the whole point of the strategy.
    result = scorer.score(make_context(
        price=60_000,
        valuation=make_valuation(market_value=204_780.0, arv=355_785.0, data_quality_flags=[]),
    ))
    assert result.score is not None
