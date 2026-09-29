"""Motivated-seller scorer: seller pressure, ranked."""


import pytest

from aevorex.db.event_types import LISTED, REDUCED, RELISTED, REMOVED
from aevorex.scoring.motivated_seller import MotivatedSellerScorer
from tests.scoring.conftest import (
    days_ago,
    make_context,
    make_market,
    make_price_history_event,
    make_valuation,
)

scorer = MotivatedSellerScorer()


def ev(price, event, event_type, when):
    return make_price_history_event(price, event, when, event_type=event_type)


# ------------------------------------------------------- verified distress


def test_reo_scores_above_a_plain_stale_listing():
    reo = scorer.score(make_context(is_reo=True, is_foreclosure=False, is_short_sale=False,
                                    is_probate_or_estate=False, is_auction=False))
    plain = scorer.score(make_context(is_reo=False, is_foreclosure=False, is_short_sale=False,
                                      is_probate_or_estate=False, is_auction=False))
    assert reo.score > plain.score


def test_reo_outranks_short_sale():
    # Both are motivated, but a short sale needs lender approval and can take
    # 60-180 days or collapse. Motivated is not the same as executable.
    def one(**kw):
        base = dict(is_reo=False, is_foreclosure=False, is_short_sale=False,
                    is_probate_or_estate=False, is_auction=False)
        base.update(kw)
        return scorer.score(make_context(**base)).score

    assert one(is_reo=True) > one(is_short_sale=True)


def test_distress_signals_are_recorded_for_audit():
    result = scorer.score(make_context(
        is_probate_or_estate=True, is_reo=False, is_foreclosure=False,
        is_short_sale=False, is_auction=False,
    ))
    assert "probate_or_estate" in result.factors["distress_signals"]
    assert "probate" in result.rationale.lower()


def test_multiple_distress_flags_take_the_strongest_not_the_sum():
    # A foreclosure that is also an auction is one situation described twice.
    both = scorer.score(make_context(is_foreclosure=True, is_auction=True, is_reo=False,
                                     is_short_sale=False, is_probate_or_estate=False))
    just_foreclosure = scorer.score(make_context(
        is_foreclosure=True, is_auction=False, is_reo=False,
        is_short_sale=False, is_probate_or_estate=False))
    assert both.score == pytest.approx(just_foreclosure.score, abs=0.01)


def test_missing_listing_text_leaves_distress_unknown_not_zero():
    result = scorer.score(make_context())  # all distress flags None
    assert "no_listing_text_distress_status_unknown" in result.data_quality_flags


# ------------------------------------------------------------- price cuts


def test_cut_velocity_beats_cut_count():
    # Three cuts in 60 days is capitulation; three over two years is a
    # stubborn seller. The old scorer scored them identically.
    fast = scorer.score(make_context(
        days_on_market=60,
        price_history=[
            ev(400_000, "Listed", LISTED, days_ago(60)),
            ev(380_000, "Price Changed", REDUCED, days_ago(40)),
            ev(360_000, "Price Changed", REDUCED, days_ago(20)),
            ev(340_000, "Price Changed", REDUCED, days_ago(5)),
        ],
        price=340_000,
    ))
    slow = scorer.score(make_context(
        days_on_market=730,
        price_history=[
            ev(400_000, "Listed", LISTED, days_ago(730)),
            ev(380_000, "Price Changed", REDUCED, days_ago(600)),
            ev(360_000, "Price Changed", REDUCED, days_ago(400)),
            ev(340_000, "Price Changed", REDUCED, days_ago(200)),
        ],
        price=340_000,
    ))
    assert fast.factors["price_cuts_per_30_days"] > slow.factors["price_cuts_per_30_days"]


def test_relist_and_withdrawal_add_pressure():
    base_events = [ev(400_000, "Listed", LISTED, days_ago(200))]
    plain = scorer.score(make_context(price_history=base_events, days_on_market=200))
    withdrawn = scorer.score(make_context(
        price_history=base_events + [
            ev(None, "Listing Removed", REMOVED, days_ago(120)),
            ev(390_000, "Relisted", RELISTED, days_ago(100)),
        ],
        days_on_market=200,
    ))
    assert withdrawn.score > plain.score
    assert withdrawn.factors["previously_withdrawn"] is True
    assert withdrawn.factors["relisted"] is True


def test_rental_events_do_not_count_as_price_cuts():
    # Rents average ~$4,855 against ~$675k for sale listings; mixing them
    # produced 99% "price drops" in the old implementation.
    result = scorer.score(make_context(
        price_history=[
            ev(400_000, "Listed", LISTED, days_ago(100)),
            make_price_history_event(3_000, "Listed for Rent", days_ago(50),
                                     event_type="listed_for_rent", is_rental_event=True),
        ],
        price=400_000,
    ))
    assert result.factors["price_reduction_count"] == 0
    assert result.factors["original_list_price"] == 400_000


# --------------------------------------------------------- market-relative


def test_days_on_market_is_scored_against_the_local_median():
    # 60 days is unremarkable where the median is 55 and a strong signal
    # where it is 5.
    slow_market = scorer.score(make_context(
        days_on_market=60, market=make_market(median_dom=55.0)))
    fast_market = scorer.score(make_context(
        days_on_market=60, market=make_market(median_dom=5.0)))
    assert fast_market.score > slow_market.score
    assert fast_market.factors["dom_vs_market_median"] == 12.0


def test_falls_back_to_absolute_days_without_a_market_baseline():
    result = scorer.score(make_context(days_on_market=150, market=None))
    assert "no_market_dom_baseline_scored_on_absolute_days" in result.data_quality_flags
    assert result.score is not None


def test_discount_to_our_valuation_raises_the_score():
    cheap = scorer.score(make_context(
        price=240_000, valuation=make_valuation(market_value=300_000)))
    at_market = scorer.score(make_context(
        price=300_000, valuation=make_valuation(market_value=300_000)))
    assert cheap.score > at_market.score
    assert cheap.factors["pct_below_estimated_value"] == pytest.approx(20.0)


# ----------------------------------------------------------------- gates


def test_placeholder_price_is_not_scored():
    result = scorer.score(make_context(price=5_000, price_is_placeholder=True))
    assert result.score is None
    assert "price_is_a_placeholder_not_a_market_asking_price" in result.data_quality_flags


def test_occupancy_modifiers_move_in_opposite_directions():
    vacant = scorer.score(make_context(is_vacant=True, days_on_market=120))
    tenanted = scorer.score(make_context(is_tenant_occupied=True, days_on_market=120))
    neutral = scorer.score(make_context(days_on_market=120))
    assert vacant.score > neutral.score > tenanted.score


# ------------------------------------------------------------ confidence


def test_confidence_reflects_how_much_was_available():
    rich = scorer.score(make_context(
        is_reo=True, is_foreclosure=False, is_short_sale=False,
        is_probate_or_estate=False, is_auction=False,
        days_on_market=120, description="Motivated seller, bring all offers.",
        price_history=[ev(400_000, "Listed", LISTED, days_ago(120))],
    ))
    sparse = scorer.score(make_context(days_on_market=120, market=None, valuation=None))
    assert rich.confidence > sparse.confidence


def test_rationale_flags_a_provisional_score():
    sparse = scorer.score(make_context(days_on_market=90, market=None, valuation=None))
    if sparse.confidence < 0.6:
        assert "provisional" in sparse.rationale.lower()


def test_keyword_signal_is_weighted_below_verified_distress():
    keywords_only = scorer.score(make_context(
        description="MOTIVATED SELLER! Must sell! Bring all offers!",
        is_reo=False, is_foreclosure=False, is_short_sale=False,
        is_probate_or_estate=False, is_auction=False,
    ))
    verified = scorer.score(make_context(
        is_reo=True, is_foreclosure=False, is_short_sale=False,
        is_probate_or_estate=False, is_auction=False,
    ))
    assert verified.score > keywords_only.score, (
        "an agent typing 'motivated seller' must not outrank a bank-owned property"
    )
