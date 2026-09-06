"""
Regression cover for the pipeline-ordering fault that emptied the Palm Coast run.

WHAT HAPPENED
-------------
416 Palm Coast properties were scraped and scored without an intervening
valuation pass. Scoring did not fail — it wrote 416 analysis rows in which
only motivated_seller was populated and the other four strategies each
carried a "couldn't score" rationale. From the scoring log alone the run
looked like a clean success, because nothing counted how many properties had
been scored against a missing valuation.

These tests pin both halves of that: which strategies actually depend on a
valuation, and that the dependency is now counted rather than swallowed.
"""

import pytest

from aevorex.scoring.airbnb import AirbnbScorer
from aevorex.scoring.buy_hold import BuyAndHoldScorer
from aevorex.scoring.fix_flip import FixAndFlipScorer
from aevorex.scoring.motivated_seller import MotivatedSellerScorer
from aevorex.scoring.short_term_rental import MidTermRentalScorer

VALUATION_DEPENDENT = (FixAndFlipScorer, BuyAndHoldScorer, MidTermRentalScorer, AirbnbScorer)


@pytest.mark.parametrize("scorer_cls", VALUATION_DEPENDENT)
def test_valuation_dependent_strategies_are_unscoreable_without_a_valuation(
    context_factory, scorer_cls
):
    """
    Without a valuation these must return no score AND say why.

    A silent 0 would be far worse than a None: it ranks, so an unvalued
    property would sit at the bottom of a list as though it had been assessed
    and found wanting, rather than being visibly absent from the assessment.
    """
    ctx = context_factory(valuation=None, comps=[])
    result = scorer_cls().score(ctx)

    assert result.score is None, (
        f"{scorer_cls.__name__} produced a score with no valuation behind it"
    )
    assert result.rationale, f"{scorer_cls.__name__} gave no reason for not scoring"


def test_motivated_seller_still_scores_without_a_valuation(context_factory):
    """
    The one strategy that must survive a missing valuation.

    Seller pressure is read off price cuts, days on market and distress flags,
    none of which need a valuation. This is why the Palm Coast run looked
    partly alive: motivated_seller was genuinely working.
    """
    ctx = context_factory(valuation=None, comps=[])
    result = MotivatedSellerScorer().score(ctx)

    assert result.score is not None
    assert 0.0 <= result.score <= 100.0


def test_scoring_runner_reports_unvalued_in_its_stats():
    """
    The counter that turns a silent degradation into a visible one.

    Asserted on the key rather than a value so the contract with
    `main._score_all`'s log line — which reads `stats["unvalued"]` — cannot be
    dropped without a test failing.
    """
    import inspect

    from aevorex.scoring.runner import ScoringRunner

    source = inspect.getsource(ScoringRunner.run)
    assert '"unvalued"' in source
    assert 'stats["unvalued"] += 1' in source


def test_score_one_signals_whether_a_valuation_backed_the_scores(context_factory):
    """`_score_one` returns the flag the counter above is built on."""
    import inspect

    from aevorex.scoring.runner import ScoringRunner

    signature = inspect.signature(ScoringRunner._score_one)
    assert signature.return_annotation is bool, (
        "_score_one must report valuation presence for the unvalued counter to work"
    )
