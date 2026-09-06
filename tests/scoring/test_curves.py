"""
Tests for the continuous scoring primitives.

These exist because the single biggest defect in the previous scoring layer
was its use of step functions: fix & flip came out p50=0.0, p90=0.7,
p99=100.0 — a filter pretending to be a ranking. Monotonicity is therefore
the property that matters most here, and it is asserted directly.
"""

import pytest

from aevorex.scoring.curves import (
    apply_gate,
    assign_percentiles,
    clamp,
    confidence_adjusted,
    inverse_ramp,
    linear_ramp,
    logistic_score,
    percentile_of,
    ratio_to_benchmark,
    weighted_blend,
)


# ---------------------------------------------------------------- monotonic


def test_linear_ramp_is_strictly_monotonic_between_anchors():
    scores = [linear_ramp(v, 0.0, 10.0) for v in range(0, 11)]
    assert scores == sorted(scores)
    assert len(set(scores)) == 11, "no two distinct inputs may share a score"


def test_logistic_is_strictly_monotonic_across_a_wide_range():
    scores = [logistic_score(v, midpoint=15.0, steepness=9.0) for v in range(-50, 60, 2)]
    assert all(b > a for a, b in zip(scores, scores[1:])), "must never plateau"


def test_logistic_spreads_the_decision_band():
    # The whole point: near the midpoint, small differences in the underlying
    # quantity should produce visible score differences.
    low = logistic_score(10.0, 15.0, 9.0)
    mid = logistic_score(15.0, 15.0, 9.0)
    high = logistic_score(20.0, 15.0, 9.0)
    assert mid == pytest.approx(50.0)
    assert high - mid > 10, "5 points of ROI must move the score meaningfully"
    assert mid - low > 10


def test_ramps_clamp_outside_their_anchors():
    assert linear_ramp(-100, 0, 10) == 0.0
    assert linear_ramp(1000, 0, 10) == 100.0


def test_inverse_ramp_rewards_lower_values():
    assert inverse_ramp(0.8, best=0.8, worst=1.2) == 100.0
    assert inverse_ramp(1.2, best=0.8, worst=1.2) == 0.0
    assert inverse_ramp(1.0, best=0.8, worst=1.2) == pytest.approx(50.0)


def test_none_input_propagates_as_none():
    assert linear_ramp(None, 0, 10) is None
    assert logistic_score(None, 1, 1) is None
    assert ratio_to_benchmark(None, 5) is None
    assert ratio_to_benchmark(5, None) is None


def test_logistic_does_not_overflow_on_extreme_input():
    assert logistic_score(1e12, 15.0, 9.0) == pytest.approx(100.0)
    assert logistic_score(-1e12, 15.0, 9.0) == pytest.approx(0.0)


# ------------------------------------------------------------------ blending


def test_weighted_blend_reports_coverage():
    score, coverage = weighted_blend([(80.0, 0.5), (40.0, 0.3), (None, 0.2)])
    # Renormalised over the 0.8 of weight that was available.
    assert score == pytest.approx((80 * 0.5 + 40 * 0.3) / 0.8)
    assert coverage == pytest.approx(0.8)


def test_weighted_blend_with_nothing_available():
    assert weighted_blend([(None, 0.5), (None, 0.5)]) == (None, 0.0)


def test_coverage_distinguishes_partial_from_complete():
    # The defect this fixes: without coverage, these two are indistinguishable.
    partial, cov_partial = weighted_blend([(90.0, 0.2), (None, 0.8)])
    complete, cov_complete = weighted_blend([(90.0, 0.2), (90.0, 0.8)])
    assert partial == complete == 90.0
    assert cov_partial < cov_complete


# -------------------------------------------------------------------- gates


def test_gate_caps_rather_than_zeroes():
    # A property that can't be let nightly may still be a fine long-term hold,
    # so the score should degrade, not vanish.
    assert apply_gate(95.0, 25.0) <= 25.0
    assert apply_gate(95.0, 25.0) > 0
    assert apply_gate(95.0, None) == 95.0


def test_gate_never_raises_a_score():
    for score in (5.0, 40.0, 95.0):
        assert apply_gate(score, 25.0) <= score


def test_gate_preserves_ordering_inside_the_gated_cohort():
    # Clipping with min() collapsed 594 live HOA-restricted properties onto a
    # single score, leaving them unorderable. Compression keeps them ranked.
    gated = [apply_gate(s, 25.0) for s in (30.0, 50.0, 70.0, 90.0)]
    assert gated == sorted(gated)
    assert len(set(gated)) == 4


def test_every_gated_score_stays_below_the_cap():
    assert all(apply_gate(s, 25.0) <= 25.0 for s in range(0, 101, 5))


# ------------------------------------------------------------- confidence


def test_confidence_shrinks_toward_the_middle_not_toward_zero():
    # Low confidence means "we don't know", which is not "this is bad".
    speculative = confidence_adjusted(90.0, confidence=0.0)
    evidenced = confidence_adjusted(90.0, confidence=1.0)
    assert evidenced == 90.0
    assert 50.0 < speculative < 90.0

    low_speculative = confidence_adjusted(10.0, confidence=0.0)
    assert 10.0 < low_speculative < 50.0, "a weak low score should also move up"


def test_confidence_preserves_ordering():
    a = confidence_adjusted(80.0, 0.5)
    b = confidence_adjusted(60.0, 0.5)
    assert a > b


# ----------------------------------------------------------- percentiles


def test_assign_percentiles_ranks_and_preserves_position():
    values = [10.0, 20.0, 30.0, 40.0]
    out = assign_percentiles(values)
    assert out == sorted(out)
    assert out[-1] > out[0]
    assert len(out) == len(values)


def test_assign_percentiles_passes_none_through():
    out = assign_percentiles([10.0, None, 30.0])
    assert out[1] is None
    assert out[0] is not None and out[2] is not None


def test_assign_percentiles_handles_all_none():
    assert assign_percentiles([None, None]) == [None, None]


def test_ties_land_mid_band_not_at_the_bottom():
    out = assign_percentiles([5.0, 5.0, 5.0, 5.0])
    assert all(v == pytest.approx(50.0) for v in out)


def test_percentile_of_empty_population():
    assert percentile_of(5.0, []) is None


def test_clamp_bounds():
    assert clamp(-10) == 0.0
    assert clamp(500) == 100.0
    assert clamp(42.5) == 42.5
