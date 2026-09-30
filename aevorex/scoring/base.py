"""Shared scorer contracts: ScoreResult, ScoringContext, BaseScorer."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from aevorex.db.models import (
    LocationScore,
    MarketSnapshot,
    PriceHistory,
    Property,
    PropertyComp,
    PropertyFeature,
    PropertyValuation,
    TaxHistory,
)
from aevorex.scoring.config import DEFAULT_CONFIG, ScoringConfig


@dataclass
class ScoreResult:
    """
    Result of scoring one property against one strategy.

    `score` is None (never a fabricated 0) whenever the strategy's core inputs
    are missing entirely — callers must branch on data_quality_flags to
    distinguish "scored low" from "couldn't be scored".

    `confidence` (0-1) is how much of the strategy's weighted input was
    actually available. Without it a score built on two of five signals is
    indistinguishable from one built on all five, which is exactly how the
    previous version misled: a property with no rent estimate and no comps
    could still surface near the top on the strength of one lucky factor.
    """

    score: Optional[float]
    rationale: str
    factors: Dict[str, Any] = field(default_factory=dict)
    data_quality_flags: List[str] = field(default_factory=list)
    confidence: float = 0.0
    # Set by ScoringRunner after every property is scored — a rank needs the
    # whole population, so it cannot be computed inside a single scorer.
    percentile: Optional[float] = None
    # Letter grade A-F on the final score (see config.letter_grade), and the
    # short list of plain-language risks an investor would forward to a
    # partner. Both are what a buyer actually reads; the score is what the
    # API sorts on.
    grade: Optional[str] = None
    top_risks: List[str] = field(default_factory=list)
    breakdown: Dict[str, Any] = field(default_factory=dict)
    component_scores: Dict[str, Optional[float]] = field(default_factory=dict, repr=False)
    score_adjustments: List[Dict[str, Any]] = field(default_factory=list, repr=False)


@dataclass
class ScoringContext:
    """Everything a scorer needs for one property, gathered by ScoringRunner."""

    property: Property
    price_history: List[PriceHistory]  # ordered oldest -> newest
    tax_history: List[TaxHistory]  # ordered by tax_year ascending
    comps: List[PropertyComp]
    location_score: Optional[LocationScore]
    features: Optional[PropertyFeature]
    market_snapshot: Optional[MarketSnapshot]  # latest snapshot for property.zip_code

    # Our own derived economics — value, ARV, rehab, rent, NOI, carrying
    # costs. Produced by aevorex/valuation/engine.py before scoring runs.
    # Present for ~99% of properties, versus 57% for the source AVM it
    # replaces, and it is what makes the strategies comparable to each other.
    valuation: Optional[PropertyValuation] = None

    # Resolved market baseline (zip, else city/county/state) as a plain dict
    # from market_stats. Every "is this good?" question is answered against
    # this rather than a fixed threshold, because median sold $/sqft across
    # the corpus spans $121 to $1,065.
    market: Optional[dict] = None

    # Typed MLS amenity fields — construction class, roof, pool, furnished,
    # lease restrictions and so on. See normalizers/amenities.py.
    amenities: Dict[str, Any] = field(default_factory=dict)

    config: ScoringConfig = field(default_factory=lambda: DEFAULT_CONFIG)

    # Nearby places from Redfin's `places` block, as (name, distance_miles)
    # pairs. Category is never populated by the source, so consumers match on
    # name — hospitals for mid-term demand, beaches and theme parks for
    # nightly demand.
    pois: List[tuple] = field(default_factory=list)

    # Results of strategies scored earlier in the same run. ScoringRunner
    # scores motivated_seller first so fix & flip can fuse deal economics
    # with seller pressure — a property 15% over MAO with a seller in
    # foreclosure who has cut three times is a better lead than one 5% over
    # MAO listed yesterday, and only one of the two scores can see that.
    prior_results: Dict[str, "ScoreResult"] = field(default_factory=dict)

    # -- convenience accessors, so scorers don't repeat None-guards ---------

    def prior_score(self, strategy_key: str) -> Optional[float]:
        result = self.prior_results.get(strategy_key)
        return result.score if result else None

    def market_value(self) -> Optional[float]:
        return self.valuation.market_value if self.valuation else None

    def arv(self) -> Optional[float]:
        return self.valuation.arv if self.valuation else None

    def rent(self) -> Optional[float]:
        return self.valuation.rent_estimate_monthly if self.valuation else None

    def all_in_basis(self) -> Optional[float]:
        """
        Purchase price plus the mid renovation estimate.

        The denominator every income yield should use: rent is earned by a
        lettable property, and getting there costs the rehab as well as the
        price. Falls back to price alone when no estimate exists.
        """
        price = self.property.price
        if not price:
            return None
        rehab = self.valuation.rehab_cost_mid if self.valuation else None
        return float(price) + float(rehab or 0.0)

    def amenity(self, key: str, default: Any = None) -> Any:
        value = self.amenities.get(key)
        return default if value is None else value


class BaseScorer(ABC):
    """One subclass per investment strategy."""

    STRATEGY_KEY: str  # matches the `{strategy}_` column prefix on PropertyAnalysis

    @abstractmethod
    def score(self, ctx: ScoringContext) -> ScoreResult:
        """Score ctx.property against this strategy."""
        ...

    @abstractmethod
    def breakdown(self, ctx: ScoringContext, result: ScoreResult) -> Dict[str, Any]:
        """Return section 6.2 anatomy from intermediates used by ``score``."""
        ...

    # -- shared helpers ----------------------------------------------------

    @staticmethod
    def unscoreable(reason: str, flags: List[str], factors: Optional[dict] = None) -> ScoreResult:
        """
        Decline to score, rather than emit a misleading zero.

        A 0 means "we evaluated this and it is bad". A None means "we could
        not evaluate it". Collapsing the two is what buried genuinely good
        properties among genuinely unscoreable ones in the previous version.
        """
        return ScoreResult(
            score=None, rationale=reason, factors=factors or {},
            data_quality_flags=flags, confidence=0.0,
        )

    def with_breakdown(self, ctx: ScoringContext, result: ScoreResult) -> ScoreResult:
        result.breakdown = self.breakdown(ctx, result)
        return result


def tier_for_percentile(percentile: Optional[float]) -> Optional[str]:
    if percentile is None:
        return None
    if percentile >= 95:
        return "top"
    if percentile >= 80:
        return "strong"
    return "rest"


def make_breakdown(
    *, lens: str, version: str, result: ScoreResult,
    components: List[Dict[str, Any]], adjustments: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "lens": lens,
        "version": version,
        "score": result.score,
        "grade": result.grade,
        "percentile": result.percentile,
        "confidence": result.confidence,
        "tier": tier_for_percentile(result.percentile),
        "pool": {"level": None, "key": None, "n": 0},
        "components": components,
        "adjustments": adjustments,
        "flags": list(result.data_quality_flags),
        "rationale": result.rationale,
    }


def component(
    key: str, label: str, weight: float, subscore: Optional[float],
    drivers: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "key": key, "label": label, "weight": weight,
        "subscore": subscore if subscore is not None else 0.0,
        "available": subscore is not None, "drivers": drivers,
    }
