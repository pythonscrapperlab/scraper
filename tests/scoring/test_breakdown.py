"""E1 score anatomy, recomposition and frozen-score regression tests."""

import hashlib
import json

import pytest

from aevorex.scoring.recompose import recompose
from aevorex.scoring.runner import ScoringRunner
from tests.scoring.e1_cases import fixture_market


def _score_through(ctx, target):
    for scorer in ScoringRunner.SCORERS:
        result = scorer.score(ctx)
        ctx.prior_results[scorer.STRATEGY_KEY] = result
        if scorer.STRATEGY_KEY == target.STRATEGY_KEY:
            return result
    raise AssertionError(f"unknown scorer {target.STRATEGY_KEY}")


@pytest.mark.parametrize("scorer", ScoringRunner.SCORERS, ids=lambda scorer: scorer.STRATEGY_KEY)
def test_recompose_500_random_rows_per_lens(scorer):
    checked = 0
    for ctx in fixture_market(500):
        result = _score_through(ctx, scorer)
        assert result.score is not None
        assert abs(recompose(result.breakdown) - result.score) < 0.01
        assert all("available" in item for item in result.breakdown["components"])
        checked += 1
    assert checked == 500


def test_fixture_market_score_bytes_are_identical_to_pre_e1_baseline():
    rows = []
    for ctx in fixture_market(64):
        scores = {}
        for scorer in ScoringRunner.SCORERS:
            result = scorer.score(ctx)
            ctx.prior_results[scorer.STRATEGY_KEY] = result
            scores[scorer.STRATEGY_KEY] = result.score
        rows.append(scores)
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(payload).hexdigest() == (
        "83a652da2cab39d1dfbe11294e0424ec123de2b77a132cc52a70df969cd2beb7"
    )


def test_every_scorer_exports_all_components_for_an_unscoreable_row():
    ctx = next(fixture_market(1))
    ctx.property.price_is_placeholder = True
    for scorer in ScoringRunner.SCORERS:
        result = scorer.score(ctx)
        assert result.score is None
        assert result.breakdown["components"]
        assert all(item["subscore"] == 0 for item in result.breakdown["components"])
        assert all(item["available"] is False for item in result.breakdown["components"])
