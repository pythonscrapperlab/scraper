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

    # -- convenience accessors, so scorers don't repeat None-guards ---------

    def market_value(self) -> Optional[float]:
        return self.valuation.market_value if self.valuation else None

    def arv(self) -> Optional[float]:
        return self.valuation.arv if self.valuation else None

    def rent(self) -> Optional[float]:
        return self.valuation.rent_estimate_monthly if self.valuation else None

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
