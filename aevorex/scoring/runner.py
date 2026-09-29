"""
ScoringRunner — scores properties against all five strategies and ranks them.

TWO PASSES, BY NECESSITY
------------------------
Pass 1 scores each property independently and writes its absolute score.
Pass 2 computes percentiles, which cannot happen inside a scorer because a
rank needs the whole population.

Percentiles are computed WITHIN A MARKET, not globally. Median sold $/sqft
across this corpus spans $121 (Jacksonville 32209) to $1,065 (Naples 34102) —
an 8.8x range — so a global rank would mostly measure which city a property
sits in. Properties are pooled by city where the city has enough of them to
rank against, and fall back to a statewide pool otherwise.

Pass 1 commits per property, mirroring PipelineRunner's incremental-commit
philosophy: one bad property should not lose a run's completed work.
"""

import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aevorex.db.models import (
    LocationScore,
    MarketSnapshot,
    PriceHistory,
    Property,
    PropertyAnalysis,
    PropertyComp,
    PropertyFeature,
    PropertyValuation,
    TaxHistory,
)
from aevorex.market.stats import classify_property, resolve_market
from aevorex.normalizers.amenities import parse_amenities
from aevorex.scoring.airbnb import AirbnbScorer
from aevorex.scoring.base import BaseScorer, ScoringContext
from aevorex.scoring.buy_hold import BuyAndHoldScorer
from aevorex.scoring.config import DEFAULT_CONFIG, ScoringConfig
from aevorex.scoring.curves import assign_percentiles
from aevorex.scoring.fix_flip import FixAndFlipScorer
from aevorex.scoring.motivated_seller import MotivatedSellerScorer
from aevorex.scoring.short_term_rental import MidTermRentalScorer

logger = logging.getLogger("aevorex.scoring.runner")

# A city needs at least this many scored properties to be its own ranking
# pool. Below that, ranking against a handful of neighbours is noise and the
# statewide pool is the more honest comparison.
MIN_POOL_SIZE = 30


class ScoringRunner:
    """
    Usage:
        runner = ScoringRunner()
        stats = await runner.run(session)
    """

    SCORERS: List[BaseScorer] = [
        MotivatedSellerScorer(),
        FixAndFlipScorer(),
        BuyAndHoldScorer(),
        MidTermRentalScorer(),   # `str` — mid-term / snowbird letting, 30+ days
        AirbnbScorer(),          # nightly vacation letting, under 30 days
    ]

    def __init__(self, config: ScoringConfig = DEFAULT_CONFIG):
        self.config = config
        self._market_cache: Dict[tuple, Optional[dict]] = {}

    async def run(self, session: AsyncSession, all_properties: bool = False) -> Dict[str, Any]:
        """Score, then rank."""
        stats: Dict[str, Any] = {
            "total": 0, "scored": 0, "errors": 0, "ranked": 0, "unvalued": 0,
        }

        query = select(Property)
        if not all_properties:
            query = query.where(Property.needs_analysis.is_(True))
        properties = (await session.execute(query)).scalars().all()
        stats["total"] = len(properties)
        logger.info(
            "Scoring %d properties against %d strategies...",
            len(properties), len(self.SCORERS),
        )

        for index, prop in enumerate(properties, 1):
            try:
                if not await self._score_one(session, prop):
                    stats["unvalued"] += 1
                stats["scored"] += 1
            except Exception as exc:
                await session.rollback()
                stats["errors"] += 1
                logger.error("Scoring failed: error_class=%s", type(exc).__name__)
            if index % 500 == 0:
                logger.info("  ... %d/%d", index, len(properties))

        # Percentiles need every score present, so this covers the whole table
        # rather than just the batch that was rescored — adding one property
        # shifts where every other one in that market sits.
        stats["ranked"] = await self._assign_percentiles(session)
        logger.info(
            "Scoring complete: %d scored, %d errors, %d ranked.",
            stats["scored"], stats["errors"], stats["ranked"],
        )
        # A property with no valuation row still gets an analysis row, but
        # only motivated_seller is populated in it — every other strategy
        # reports "couldn't score". That is a pipeline-ordering fault, not a
        # data-quality one, and it is silent unless it is counted here: the
        # first Palm Coast run wrote 416 analysis rows against 0 valuations
        # and looked, from the scoring log alone, like a clean success.
        if stats["unvalued"]:
            logger.warning(
                "%d of %d scored properties had no property_valuation row, so "
                "fix_flip / buy_hold / str / airbnb are NULL for them. Run "
                "`python main.py value` (or `analyze`, which orders the stages "
                "correctly) before scoring.",
                stats["unvalued"], stats["scored"],
            )
        return stats

    # ------------------------------------------------------------------

    async def _score_one(self, session: AsyncSession, prop: Property) -> bool:
        """Score one property. Returns whether a valuation backed the scores."""
        ctx = await self._build_context(session, prop)
        results = {scorer.STRATEGY_KEY: scorer.score(ctx) for scorer in self.SCORERS}
        await self._upsert_analysis(session, prop.id, results)
        prop.needs_analysis = False
        await session.commit()
        return ctx.valuation is not None

    async def _build_context(self, session: AsyncSession, prop: Property) -> ScoringContext:
        price_history = (await session.execute(
            select(PriceHistory)
            .where(PriceHistory.property_id == prop.id)
            .order_by(PriceHistory.event_date.asc())
        )).scalars().all()

        tax_history = (await session.execute(
            select(TaxHistory)
            .where(TaxHistory.property_id == prop.id)
            .order_by(TaxHistory.tax_year.asc())
        )).scalars().all()

        comps = (await session.execute(
            select(PropertyComp).where(PropertyComp.property_id == prop.id)
        )).scalars().all()

        location_score = await session.get(LocationScore, prop.id)
        features = await session.get(PropertyFeature, prop.id)
        valuation = await session.get(PropertyValuation, prop.id)

        market_snapshot = None
        if prop.zip_code:
            market_snapshot = (await session.execute(
                select(MarketSnapshot)
                .where(MarketSnapshot.zip_code == prop.zip_code)
                .order_by(MarketSnapshot.snapshot_date.desc())
            )).scalars().first()

        return ScoringContext(
            property=prop,
            price_history=list(price_history),
            tax_history=list(tax_history),
            comps=list(comps),
            location_score=location_score,
            features=features,
            market_snapshot=market_snapshot,
            valuation=valuation,
            market=await self._market_for(session, prop),
            amenities=parse_amenities(features.raw_amenities if features else None),
            config=self.config,
        )

    async def _market_for(self, session: AsyncSession, prop: Property) -> Optional[dict]:
        """Resolve and cache the market baseline; many properties share one."""
        key = (prop.zip_code, prop.city, prop.county, prop.state,
               classify_property(prop.property_type))
        if key not in self._market_cache:
            self._market_cache[key] = await resolve_market(
                session, prop.zip_code, prop.city, prop.county, prop.state, prop.property_type
            )
        return self._market_cache[key]

    async def _upsert_analysis(
        self, session: AsyncSession, property_id: UUID, results: dict
    ) -> None:
        analysis = (await session.execute(
            select(PropertyAnalysis).where(PropertyAnalysis.property_id == property_id)
        )).scalars().first()
        if analysis is None:
            analysis = PropertyAnalysis(property_id=property_id)
            session.add(analysis)

        analysis.scoring_config_version = self.config.version
        for strategy_key, result in results.items():
            setattr(analysis, f"{strategy_key}_score", result.score)
            setattr(analysis, f"{strategy_key}_rationale", result.rationale)
            setattr(analysis, f"{strategy_key}_factors", result.factors)
            setattr(analysis, f"{strategy_key}_flags", result.data_quality_flags)
            setattr(analysis, f"{strategy_key}_confidence", result.confidence)
        await session.flush()

    # ------------------------------------------------------------------

    async def _assign_percentiles(self, session: AsyncSession) -> int:
        """Rank every scored property within its own market pool."""
        rows = (await session.execute(
            select(PropertyAnalysis, Property.city, Property.state)
            .join(Property, Property.id == PropertyAnalysis.property_id)
        )).all()
        if not rows:
            return 0

        city_counts: Dict[Tuple[str, str], int] = defaultdict(int)
        for _, city, state in rows:
            city_counts[(city, state)] += 1

        pools: Dict[Any, List[PropertyAnalysis]] = defaultdict(list)
        for analysis, city, state in rows:
            key = ((city, state) if city_counts[(city, state)] >= MIN_POOL_SIZE
                   else ("__state__", state))
            pools[key].append(analysis)

        for pool in pools.values():
            for scorer in self.SCORERS:
                strategy = scorer.STRATEGY_KEY
                scores = [getattr(a, f"{strategy}_score") for a in pool]
                for analysis, percentile in zip(pool, assign_percentiles(scores)):
                    setattr(analysis, f"{strategy}_percentile", percentile)

        await session.commit()
        logger.info("Assigned percentiles across %d ranking pools.", len(pools))
        return len(rows)
