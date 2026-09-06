"""
A property whose price is a placeholder must not be scored by anything.

Foreclosure auctions publish the deposit or opening bid in the price field —
42 live properties sit at exactly $5,000 against assessed values of
$167k-$556k. All five strategies are price-relative, so scoring those would
rank them at the top of every list on a number that does not mean what it
appears to. Before this gate existed they scored 46.8 on motivated-seller,
squarely mid-pack, which is arguably worse than topping the list: it hides
them.
"""

import pytest

from aevorex.scoring.airbnb import AirbnbScorer
from aevorex.scoring.buy_hold import BuyAndHoldScorer
from aevorex.scoring.config import ScoringConfig
from aevorex.scoring.fix_flip import FixAndFlipScorer
from aevorex.scoring.motivated_seller import MotivatedSellerScorer
from aevorex.scoring.short_term_rental import MidTermRentalScorer

from .conftest import make_context, make_location_score

ALL_SCORERS = [
    MotivatedSellerScorer(),
    FixAndFlipScorer(),
    BuyAndHoldScorer(),
    MidTermRentalScorer(),
    AirbnbScorer(),
]

PLACEHOLDER_FLAG = "price_is_a_placeholder_not_a_market_asking_price"


@pytest.mark.parametrize("scorer", ALL_SCORERS, ids=lambda s: s.STRATEGY_KEY)
def test_no_strategy_scores_a_placeholder_price(scorer):
    ctx = make_context(price=5_000, price_is_placeholder=True,
                       location_score=make_location_score())
    result = scorer.score(ctx)

    assert result.score is None, f"{scorer.STRATEGY_KEY} scored a fake price"
    assert PLACEHOLDER_FLAG in result.data_quality_flags
    assert result.confidence == 0.0


@pytest.mark.parametrize("scorer", ALL_SCORERS, ids=lambda s: s.STRATEGY_KEY)
def test_rationale_explains_the_refusal(scorer):
    # Each scorer words this in its own terms — the flip scorer talks about
    # margin, the income scorers about yield — so assert the concept rather
    # than one phrasing.
    rationale = scorer.score(
        make_context(price=5_000, price_is_placeholder=True)
    ).rationale.lower()
    assert "not scored" in rationale
    assert any(term in rationale for term in ("placeholder", "auction deposit", "opening bid"))


@pytest.mark.parametrize("flag", [False, None])
@pytest.mark.parametrize("scorer", ALL_SCORERS, ids=lambda s: s.STRATEGY_KEY)
def test_a_normal_price_is_still_scored(scorer, flag):
    # Guard against the gate swallowing everything: only an explicit True
    # short-circuits, so an un-backfilled NULL still goes through the scorers.
    result = scorer.score(make_context(
        price=300_000, price_is_placeholder=flag,
        description="Well maintained home.", location_score=make_location_score(),
    ))
    assert result.score is not None, f"{scorer.STRATEGY_KEY} refused a real price"
    assert PLACEHOLDER_FLAG not in result.data_quality_flags


def test_the_gate_can_be_disabled_by_config():
    # Turning it off should let the scorers through — the switch exists so a
    # user can inspect what an auction property would look like if the price
    # were real, not so it silently defaults on.
    config = ScoringConfig(skip_placeholder_prices=False)
    result = MotivatedSellerScorer().score(make_context(
        config=config, price=5_000, price_is_placeholder=True,
        description="Foreclosure Auction Ends July 29, 2026.",
    ))
    assert result.score is not None
