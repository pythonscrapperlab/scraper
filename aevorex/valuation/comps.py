"""
Sales-comparison valuation from sold comps.

WHY COMPS AND NOT THE SOURCE AVM
--------------------------------
Two reasons, one practical and one methodological.

Practical: the source AVM is present on 57.1% of properties, and that is a
hard ceiling — coalescing across every historical scrape of the same property
returns exactly 57.0%, so there is nothing unread to recover. Comps cover
94.6%, a median of 5.9 per property, 99.3% of them sold within twelve months.

Methodological: an AVM estimates *as-is* value. For fix & flip that is the
wrong number entirely — the thesis is that the property will not stay as-is.
Comps let us produce two different figures from one evidence base: a median
for current value and an upper-quartile for the renovated exit.

METHOD
------
A weighted sales-comparison approach, which is what an appraiser does:

1. **Select** — sold within 12 months, sane price and size, and within a size
   band of the subject. Comps arrive pre-screened by the source for locality,
   so proximity filtering is not needed on top.
2. **Weight** — by similarity to the subject (size, beds, baths) and by
   recency. A comp half the subject's size still carries information, just
   less of it, so it is down-weighted rather than discarded.
3. **Reconcile** — weighted median $/sqft for market value, weighted 75th
   percentile for ARV.
4. **Correct for size** — $/sqft falls as homes get larger, so multiplying a
   small-comp $/sqft by a large subject's area overstates it. Value is scaled
   sub-linearly with the size ratio.
5. **Measure dispersion** — the coefficient of variation of comp $/sqft is the
   honest confidence signal. Live median is 0.158; 46% of properties sit under
   0.15 and 12% over 0.30. A wide set means an uncertain exit and must reduce
   a flip score, not merely annotate it.
"""

import math
import statistics
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

# Comps outside this size band relative to the subject are excluded outright —
# beyond roughly half to double the area they are a different product, and
# $/sqft stops transferring.
MIN_SIZE_RATIO = 0.5
MAX_SIZE_RATIO = 2.0

# Sanity bounds. The live corpus contains $100 quitclaim transfers and a
# $117M outlier in the sold series; neither is a market comparable.
MIN_COMP_PRICE = 10_000
MAX_COMP_PRICE = 100_000_000
MIN_COMP_SQFT = 200
MAX_COMP_SQFT = 30_000
MIN_PPSF = 20.0
MAX_PPSF = 3_000.0

MAX_COMP_AGE_DAYS = 400

# $/sqft scales sub-linearly with floor area: doubling the size of a house
# does not double its price.
#
# CALIBRATED, not assumed. A leave-one-out backtest over 40,323 held-out comp
# sales (predict each comp from the other comps of the same property) was swept
# across elasticity 0.60-1.00. Two things came out of it:
#
#   elasticity   median abs err   bias spread across size bands
#      0.60          10.83%              7.9pp
#      0.70          10.95%              4.2pp   <- chosen
#      0.80          11.18%              9.0pp
#      0.90          11.84%             17.4pp   <- the original guess
#
# 0.60 minimises average error but does it by shrinking every estimate toward
# the comp-set median size, which over-prices small homes (+4.2%) and
# under-prices large ones (-3.7%). 0.70 costs 0.12pp of average accuracy and
# roughly halves that swing, so it is close to unbiased in every size band
# rather than only in aggregate. Valuing one specific property correctly
# matters more here than minimising error over the population.
SIZE_ELASTICITY = 0.70

# Central estimator for as-is market value. NOT the median.
#
# Prices within a comp set are right-skewed, so the median $/sqft sits below
# the expected sale price and produces a systematic under-estimate. The same
# backtest showed a -3.66% bias at the median; sweeping the quantile put the
# zero-bias point at 0.60 (-0.03% bias), with 0.63 overshooting. This is a
# calibrated correction for skew, not a thumb on the scale.
MARKET_VALUE_QUANTILE = 0.60

# After-repair value. Deliberately a judgment call rather than a calibrated
# one: the backtest predicts a *typical* comp, whereas ARV predicts a
# *renovated* one, and there is no renovated-vs-not label in the comp data to
# calibrate against. p75 encodes the appraisal convention that a refurbished
# home transacts in the upper quartile of its comp range, because that is
# where the already-renovated comps sit.
ARV_QUANTILE = 0.75

# Recency half-life. A comp 6 months old carries half the weight of one that
# closed yesterday.
RECENCY_HALF_LIFE_DAYS = 180.0

# Below this many usable comps the estimate is not trustworthy on its own.
MIN_COMPS_FOR_VALUATION = 3
# Sample at which comp count stops adding confidence.
FULL_CONFIDENCE_COMPS = 8


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def select_comps(comps: Sequence[Any], subject_sqft: Optional[int]) -> List[dict]:
    """
    Filter a property's comps down to the usable, comparable ones.

    Returns dicts rather than ORM rows so the maths below is testable without
    a database.
    """
    usable: List[dict] = []
    now = _now()

    for comp in comps:
        price = getattr(comp, "price", None)
        sqft = getattr(comp, "sqft", None)
        sold_date = getattr(comp, "sold_date", None)

        if not price or not sqft:
            continue
        if not (MIN_COMP_PRICE <= price <= MAX_COMP_PRICE):
            continue
        if not (MIN_COMP_SQFT <= sqft <= MAX_COMP_SQFT):
            continue

        ppsf = price / sqft
        if not (MIN_PPSF <= ppsf <= MAX_PPSF):
            continue

        age_days = (now - sold_date).days if sold_date else None
        if age_days is not None and (age_days < 0 or age_days > MAX_COMP_AGE_DAYS):
            continue

        # Size band, only when the subject's own area is known.
        if subject_sqft:
            ratio = sqft / subject_sqft
            if not (MIN_SIZE_RATIO <= ratio <= MAX_SIZE_RATIO):
                continue

        usable.append({
            "price": price,
            "sqft": sqft,
            "ppsf": ppsf,
            "beds": getattr(comp, "bedrooms", None),
            "baths": getattr(comp, "bathrooms", None),
            "age_days": age_days,
            "address": getattr(comp, "comp_address", None),
        })

    return usable


def _weight(comp: dict, subject) -> float:
    """
    Similarity x recency weight for one comp.

    Each dimension contributes a multiplier in (0, 1]. Nothing is ever zero —
    a dissimilar comp is weak evidence, not absent evidence, and zeroing it
    would silently shrink the sample.
    """
    weight = 1.0

    subject_sqft = getattr(subject, "sqft", None)
    if subject_sqft and comp["sqft"]:
        # 1.0 at identical size, decaying with relative difference.
        delta = abs(comp["sqft"] - subject_sqft) / subject_sqft
        weight *= 1.0 / (1.0 + 1.5 * delta)

    subject_beds = getattr(subject, "bedrooms", None)
    if subject_beds and comp["beds"]:
        weight *= 1.0 / (1.0 + 0.35 * abs(comp["beds"] - subject_beds))

    subject_baths = getattr(subject, "bathrooms", None)
    if subject_baths and comp["baths"]:
        weight *= 1.0 / (1.0 + 0.25 * abs(float(comp["baths"]) - float(subject_baths)))

    if comp["age_days"] is not None:
        weight *= 0.5 ** (comp["age_days"] / RECENCY_HALF_LIFE_DAYS)

    return max(weight, 1e-6)


def weighted_percentile(values: Sequence[float], weights: Sequence[float], q: float) -> Optional[float]:
    """
    Weighted percentile.

    A plain percentile would treat a four-year-old comp of a different size as
    equal evidence to last month's near-identical sale. Uses the standard
    cumulative-weight interpolation.
    """
    if not values:
        return None
    pairs = sorted(zip(values, weights))
    total = sum(w for _, w in pairs)
    if total <= 0:
        return None

    target = q * total
    cumulative = 0.0
    previous_value = pairs[0][0]
    for value, weight in pairs:
        if cumulative + weight >= target:
            if weight <= 0:
                return value
            # Linear interpolation within this comp's weight band.
            fraction = (target - cumulative) / weight
            return previous_value + (value - previous_value) * min(max(fraction, 0.0), 1.0)
        cumulative += weight
        previous_value = value
    return pairs[-1][0]


def analyse_comps(comps: Sequence[Any], subject) -> Optional[Dict[str, Any]]:
    """
    Reduce a property's comps to the statistics the valuation needs.

    Returns None when there is nothing usable to work from.
    """
    usable = select_comps(comps, getattr(subject, "sqft", None))
    if not usable:
        return None

    weights = [_weight(c, subject) for c in usable]
    ppsf_values = [c["ppsf"] for c in usable]

    # `median_ppsf` carries the MARKET_VALUE_QUANTILE estimator, not the
    # literal median — see the constant for the calibration behind that.
    # The name is kept because it is the central-value estimator in every
    # downstream sense.
    median_ppsf = weighted_percentile(ppsf_values, weights, MARKET_VALUE_QUANTILE)
    p75_ppsf = weighted_percentile(ppsf_values, weights, ARV_QUANTILE)
    p25_ppsf = weighted_percentile(ppsf_values, weights, 0.25)

    # Dispersion is deliberately UNweighted: it measures how much the local
    # evidence actually disagrees, and weighting would flatter a tight cluster
    # of similar comps while hiding genuine spread in the wider set.
    mean_ppsf = statistics.fmean(ppsf_values)
    cv = (statistics.pstdev(ppsf_values) / mean_ppsf) if mean_ppsf > 0 and len(ppsf_values) > 1 else 0.0

    ages = [c["age_days"] for c in usable if c["age_days"] is not None]

    return {
        "count": len(usable),
        "median_ppsf": median_ppsf,
        "p25_ppsf": p25_ppsf,
        "p75_ppsf": p75_ppsf,
        "dispersion_cv": round(cv, 4),
        "median_age_days": int(statistics.median(ages)) if ages else None,
        "median_comp_sqft": statistics.median([c["sqft"] for c in usable]),
        "total_weight": round(sum(weights), 3),
    }


def _size_adjusted_value(ppsf: float, subject_sqft: int, comp_median_sqft: float) -> float:
    """
    Apply $/sqft to the subject, correcting for the size difference.

    A flat `ppsf * sqft` assumes price scales linearly with area, which it does
    not: a 4,000 sqft home does not sell for four times a 1,000 sqft home in
    the same street. Where the subject matches the comp set's typical size
    this reduces exactly to `ppsf * sqft`.
    """
    if not comp_median_sqft or comp_median_sqft <= 0:
        return ppsf * subject_sqft

    ratio = subject_sqft / comp_median_sqft
    # Clamp before applying the exponent so a freak comp set can't produce an
    # absurd extrapolation.
    ratio = min(max(ratio, MIN_SIZE_RATIO), MAX_SIZE_RATIO)
    adjusted_ratio = math.pow(ratio, SIZE_ELASTICITY)
    return ppsf * comp_median_sqft * adjusted_ratio


def valuation_confidence(analysis: Dict[str, Any]) -> float:
    """
    0-1 confidence in a comp-derived value.

    Three inputs, multiplied: how many comps, how much they agree, and how
    fresh they are. All three have to be reasonable for the estimate to be
    trusted, which is why they multiply rather than average — a tight, recent
    set of two comps is still only two comps.
    """
    count = analysis["count"]
    count_factor = min(1.0, count / FULL_CONFIDENCE_COMPS)

    # CV 0.0 -> 1.0, CV 0.30 -> ~0.4, CV 0.60+ -> ~0.14. Live median is 0.158.
    cv = analysis.get("dispersion_cv") or 0.0
    agreement_factor = 1.0 / (1.0 + 3.0 * cv)

    age = analysis.get("median_age_days")
    recency_factor = 1.0 if age is None else max(0.4, 1.0 - (age / 730.0))

    return round(count_factor * agreement_factor * recency_factor, 4)


def estimate_value_and_arv(
    comps: Sequence[Any], subject, source_avm: Optional[float] = None
) -> Dict[str, Any]:
    """
    Produce as-is market value and after-repair value for one property.

    ARV uses the comp set's 75th percentile $/sqft, not its median. This is the
    single most consequential departure from the previous implementation,
    which used the median for both. A renovated home does not sell at the
    middle of its comp range — it sells at the top, because that is where the
    renovated comps already are. Using the median systematically understated
    flip margin on exactly the properties the strategy exists to find.

    Where the source AVM is available it is blended into the as-is value only,
    never into ARV, since an AVM is by definition an as-is estimate.
    """
    result: Dict[str, Any] = {
        "market_value": None, "market_value_method": None,
        "arv": None, "arv_ceiling": None, "arv_method": None,
        "comp_count": None, "comp_median_ppsf": None, "comp_p75_ppsf": None,
        "comp_dispersion_cv": None, "comp_median_age_days": None,
        "valuation_confidence": None, "flags": [],
    }

    subject_sqft = getattr(subject, "sqft", None)
    analysis = analyse_comps(comps, subject) if subject_sqft else None

    if analysis and analysis["count"] >= MIN_COMPS_FOR_VALUATION:
        result.update({
            "comp_count": analysis["count"],
            "comp_median_ppsf": round(analysis["median_ppsf"], 2),
            "comp_p75_ppsf": round(analysis["p75_ppsf"], 2),
            "comp_dispersion_cv": analysis["dispersion_cv"],
            "comp_median_age_days": analysis["median_age_days"],
        })
        confidence = valuation_confidence(analysis)

        comp_value = _size_adjusted_value(
            analysis["median_ppsf"], subject_sqft, analysis["median_comp_sqft"]
        )
        # The CEILING an exit could reach if the property were brought up to
        # the best standard in its comp set. How much of that gap a specific
        # property can actually capture depends on how much work it needs, so
        # the caller scales it — see `scale_arv_by_condition`. Publishing the
        # ceiling as ARV outright credited a brand-new 2023 house with a 7%
        # renovation premium for renovating nothing.
        result["arv_ceiling"] = round(_size_adjusted_value(
            analysis["p75_ppsf"], subject_sqft, analysis["median_comp_sqft"]
        ))
        result["arv_method"] = "comps_p75"

        if source_avm and source_avm > 0:
            # Blend by confidence: a tight comp set outvotes the AVM, a ragged
            # one defers to it. Two independent estimates that agree are
            # stronger evidence than either alone.
            comp_weight = confidence
            blended = comp_value * comp_weight + source_avm * (1 - comp_weight)
            result["market_value"] = round(blended)
            result["market_value_method"] = "comps_avm_blend"
            divergence = abs(comp_value - source_avm) / max(source_avm, 1)
            if divergence > 0.35:
                # Worth surfacing: either the comp set or the AVM is wrong, and
                # a blended midpoint hides that rather than resolving it.
                result["flags"].append("comp_value_diverges_from_avm")
            # Agreement between two independent methods earns a modest boost.
            confidence = min(1.0, confidence * (1.15 if divergence < 0.15 else 1.0))
        else:
            result["market_value"] = round(comp_value)
            result["market_value_method"] = "comps"

        result["valuation_confidence"] = round(confidence, 4)
        if analysis["dispersion_cv"] > 0.30:
            result["flags"].append("wide_comp_dispersion_uncertain_value")
        if analysis["count"] < FULL_CONFIDENCE_COMPS:
            result["flags"].append("thin_comp_set")
        return result

    # ---- fallbacks, in descending order of trust ----
    if source_avm and source_avm > 0:
        result.update({
            "market_value": round(source_avm),
            "market_value_method": "avm",
            # An AVM is an as-is figure. Presenting it as ARV would claim a
            # renovation premium that nothing in the data supports, so ARV
            # stays NULL and the flip scorer declines rather than guesses.
            "valuation_confidence": 0.35,
        })
        result["flags"].append("no_usable_comps_fell_back_to_avm")
        if analysis:
            result["comp_count"] = analysis["count"]
        return result

    result["flags"].append("no_comps_and_no_avm_cannot_value")
    if analysis:
        result["comp_count"] = analysis["count"]
    return result


# How much of the gap between as-is value and the comp-set ceiling a property
# can realistically capture, by how much work it needs.
#
# THE BUG THIS FIXES: ARV was the comp p75 unconditionally, so a 2023-built
# house in "new" condition was credited with a 7% uplift for renovating
# nothing, and a 2022 townhouse showed a 15.6% uplift and a 73% "return" on a
# renovation that does not exist. A renovation premium has to be earned by an
# actual renovation.
#
# `new` is not exactly zero because even new stock benefits marginally from
# staging and presentation, but it is close enough to zero to stop generating
# phantom deals.
RENOVATION_CAPTURE = {
    "new": 0.05,
    "updated": 0.25,
    "standard": 0.70,
    "fixer": 1.00,
}
# Used when condition could not be determined (~16% of the corpus). Below the
# "standard" figure on purpose: absent evidence of work needing doing, assume
# less upside rather than more.
DEFAULT_RENOVATION_CAPTURE = 0.55


def scale_arv_by_condition(
    market_value: Optional[float],
    arv_ceiling: Optional[float],
    condition_class: Optional[str],
) -> Optional[float]:
    """
    Interpolate ARV between as-is value and the comp-set ceiling.

    A fixer can realistically reach the top of its comp range once renovated.
    A property already in that condition cannot go further — there is nowhere
    for it to be improved to. Returns the ceiling unchanged when as-is value
    is unknown, since there is then no gap to interpolate across.
    """
    if arv_ceiling is None:
        return None
    if market_value is None or market_value <= 0:
        return arv_ceiling
    capture = RENOVATION_CAPTURE.get(condition_class or "", DEFAULT_RENOVATION_CAPTURE)
    # Never below as-is value: renovating cannot make a property worth less.
    return round(max(market_value, market_value + capture * (arv_ceiling - market_value)))
