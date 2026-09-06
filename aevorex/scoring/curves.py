"""
Continuous scoring primitives.

WHY THESE REPLACE `step_score`
------------------------------
The previous scorers mapped every signal through a 4-6 point step function.
That quantises a continuous quantity into a handful of values and destroys
rank order inside each tier — two properties with 6% and 14% projected margin
received an identical score, and the fix & flip distribution collapsed to
p50=0.0, p90=0.7, p99=100.0. A number that is 0 for ninety percent of the
portfolio and 100 for one percent is a filter, not a ranking, and a product
whose whole job is to rank cannot be built on one.

Everything here is monotonic and continuous, so a better deal always scores
strictly higher than a worse one, however small the difference.

`step_score` is deliberately left in `utils.py`: hard thresholds are still
correct for genuine cliff-edges — a 55+ age restriction either forbids nightly
letting or it does not — and those should stay visibly different from a smooth
preference.
"""

import math
from typing import Iterable, List, Optional, Sequence, Tuple


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def linear_ramp(
    value: Optional[float],
    floor: float,
    ceiling: float,
    low_score: float = 0.0,
    high_score: float = 100.0,
) -> Optional[float]:
    """
    Straight-line interpolation between two anchor points, clamped outside them.

    The workhorse for a quantity with a defensible "bad" and "good" level —
    a 0% cap rate is bad, an 8% cap rate is excellent, and everything between
    should rank in order.
    """
    if value is None:
        return None
    if ceiling == floor:
        return high_score if value >= ceiling else low_score
    position = (value - floor) / (ceiling - floor)
    return clamp(low_score + position * (high_score - low_score),
                 min(low_score, high_score), max(low_score, high_score))


def logistic_score(
    value: Optional[float],
    midpoint: float,
    steepness: float,
    low_score: float = 0.0,
    high_score: float = 100.0,
    tail_weight: float = 0.0,
    tail_span: Optional[float] = None,
) -> Optional[float]:
    """
    S-curve: gentle at the extremes, discriminating around the midpoint.

    Correct where most of the population sits near one end and the interesting
    decisions happen in a narrow band. Flip return is the case in point — 90%
    of retail listings have negative margin, so a linear ramp spends most of
    its range on properties nobody will buy, while a logistic centred at the
    decision threshold spreads the top decile out where it matters.

    `steepness` is in the same units as `value`; larger means a gentler curve.

    THE TAIL, AND WHY IT IS NOT OPTIONAL FOR RANKING
    ------------------------------------------------
    A pure logistic saturates. With a 15% midpoint and steepness 9, a deal
    returning 100% and one returning 296% both map to 99.99-something and,
    after rounding, to an identical score — so the very best deals in the
    portfolio become unorderable. That is a milder version of the exact defect
    this curve was brought in to fix.

    `tail_weight` blends in a slow linear component over `tail_span`, which
    restores strict ordering at the extremes while leaving the shape around
    the midpoint essentially untouched. A few percent is enough: it is a
    tiebreaker, not a second opinion.
    """
    if value is None:
        return None
    if steepness <= 0:
        return high_score if value >= midpoint else low_score

    # Guard the exponent: a far-out value would otherwise overflow.
    exponent = -(value - midpoint) / steepness
    if exponent > 60:
        ratio = 0.0
    elif exponent < -60:
        ratio = 1.0
    else:
        ratio = 1.0 / (1.0 + math.exp(exponent))
    base = low_score + ratio * (high_score - low_score)

    if tail_weight > 0 and tail_span:
        # Clamps beyond +/- tail_span, which in practice is chosen wide enough
        # that nothing real reaches it.
        tail = linear_ramp(
            value, midpoint - tail_span, midpoint + tail_span, low_score, high_score
        )
        if tail is not None:
            base = (1 - tail_weight) * base + tail_weight * tail
    return base


def inverse_ramp(
    value: Optional[float], best: float, worst: float,
    low_score: float = 0.0, high_score: float = 100.0,
) -> Optional[float]:
    """Linear ramp where a LOWER input is better (price-to-value, expense ratios)."""
    return linear_ramp(value, worst, best, low_score, high_score)


def percentile_of(value: Optional[float], population: Sequence[float]) -> Optional[float]:
    """
    Where `value` falls within `population`, 0-100.

    Used to express a score relative to its own market. Median sold $/sqft in
    this corpus spans $121 to $1,065 — an 8.8x range — so an absolute score
    alone systematically favours whichever markets happen to suit the
    thresholds. The percentile answers the different and equally necessary
    question: is this a good deal *here*.
    """
    if value is None or not population:
        return None
    below = sum(1 for p in population if p < value)
    equal = sum(1 for p in population if p == value)
    # Midpoint convention, so a run of identical values lands mid-band rather
    # than all at the bottom.
    return round(100.0 * (below + 0.5 * equal) / len(population), 2)


def ratio_to_benchmark(
    value: Optional[float], benchmark: Optional[float],
    midpoint: float = 1.0, steepness: float = 0.15,
    invert: bool = False,
) -> Optional[float]:
    """
    Score a value against a market benchmark by their ratio.

    The general form of "is this better or worse than typical here" — a $/sqft
    30% under the local median scores well regardless of whether that median
    is $121 or $1,065.
    """
    if value is None or not benchmark:
        return None
    ratio = value / benchmark
    if invert:
        ratio = 2.0 - ratio
    return logistic_score(ratio, midpoint, steepness)


def weighted_blend(components: Iterable[Tuple[Optional[float], float]]) -> Tuple[Optional[float], float]:
    """
    Weighted mean over whichever components have a score, plus the share of
    total weight that was actually available.

    Returns (score, coverage). Coverage is what makes a partial score honest:
    a result built from two of five signals renormalises to look complete, and
    without the second return value the caller cannot tell it apart from one
    built on all five. The previous implementation returned only the score.
    """
    total_weight = 0.0
    used_weight = 0.0
    total = 0.0
    for score, weight in components:
        total_weight += weight
        if score is None:
            continue
        used_weight += weight
        total += score * weight
    if used_weight == 0:
        return None, 0.0
    coverage = used_weight / total_weight if total_weight else 0.0
    return total / used_weight, round(coverage, 4)


def apply_gate(score: Optional[float], cap: Optional[float]) -> Optional[float]:
    """
    Impose a ceiling from a hard constraint, without destroying rank order.

    A gate is not a weighted factor. If nightly letting is prohibited, no
    amount of beachfront charm makes the property a good Airbnb — and a
    weighted penalty would let exactly that happen. It caps rather than zeroes,
    because a prohibited property may still be an excellent long-term rental
    and the score should degrade rather than vanish.

    COMPRESSES rather than clips. A plain `min(score, cap)` collapses every
    property above the ceiling onto the ceiling itself: in the live set that
    put 594 HOA-restricted properties (8.8% of the strategy) on the identical
    score of 25.7, unorderable among themselves. Scaling the whole range into
    [0, cap] keeps every gated property strictly below every ungated one while
    preserving the ordering inside the gated cohort — which is what someone
    filtering to "HOA-restricted, but which of these is best" actually needs.

    It also makes the gate bite harder on weak properties than on strong ones,
    which is correct: a restricted property with poor fundamentals is a worse
    prospect than a restricted property with good ones, and clipping made them
    identical.
    """
    if score is None or cap is None:
        return score
    return score * cap / 100.0


def confidence_adjusted(score: Optional[float], confidence: float,
                        floor: float = 0.55) -> Optional[float]:
    """
    Pull a score toward the middle when the inputs behind it are weak.

    A speculative 90 and an evidenced 90 must not rank equally. Shrinkage is
    toward 50, not toward 0: low confidence means "we do not know", which is
    not the same as "this is bad".
    """
    if score is None:
        return None
    weight = floor + (1.0 - floor) * clamp(confidence, 0.0, 1.0)
    return 50.0 + (score - 50.0) * weight


def assign_percentiles(values: List[Optional[float]]) -> List[Optional[float]]:
    """
    Percentile-rank a whole column at once, ignoring the Nones.

    Used by the scoring runner after every property has an absolute score, so
    the market-relative column can be filled in a single pass.
    """
    population = [v for v in values if v is not None]
    if not population:
        return [None] * len(values)
    ordered = sorted(population)
    n = len(ordered)

    import bisect

    out: List[Optional[float]] = []
    for value in values:
        if value is None:
            out.append(None)
            continue
        low = bisect.bisect_left(ordered, value)
        high = bisect.bisect_right(ordered, value)
        out.append(round(100.0 * (low + high) / (2.0 * n), 2))
    return out
