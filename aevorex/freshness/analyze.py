"""Changed-row valuation/scoring and market-relative tier transition events."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aevorex.db.models import (
    AnalysisTier,
    ChangeEvent,
    ListingPresence,
    Property,
    PropertyAnalysis,
    utc_now,
)
from aevorex.db.session import async_session_maker
from aevorex.freshness.diff import tier_direction, tier_for_percentile
from aevorex.market.stats import build_market_stats
from aevorex.scoring.runner import ScoringRunner
from aevorex.valuation.engine import ValuationEngine

logger = logging.getLogger("aevorex.freshness.analyze")
LENSES = ("motivated_seller", "fix_flip", "buy_hold", "str", "airbnb")


@dataclass(frozen=True)
class TierSnapshot:
    """One current lens result for transition comparison."""

    property_id: UUID
    redfin_id: str
    market_slug: str | None
    lens: str
    score: float | None
    percentile: float | None
    tier: str


async def run_changed_analysis(*, nightly: bool = False) -> dict[str, object]:
    """Value and score only flagged rows, then persist rank transition events."""
    results: dict[str, object] = {}
    async with async_session_maker() as session:
        changed_ids = (
            await session.execute(
                select(Property.id).where(Property.needs_analysis.is_(True))
            )
        ).scalars().all()
        changed = len(changed_ids)
        results["changed"] = changed
        if nightly:
            results["market_stats"] = await build_market_stats(session)
        results["valuation"] = await ValuationEngine().run(
            session, all_properties=False
        )
        results["scoring"] = await ScoringRunner().run(
            session, all_properties=False
        )
        results["tier_events"] = await record_tier_events(session)
    return results


async def record_tier_events(session: AsyncSession) -> dict[str, int]:
    """Compare all ranked rows with the last recorded tier/percentile state."""
    rows = (
        await session.execute(
            select(PropertyAnalysis, Property.redfin_id)
            .join(Property, Property.id == PropertyAnalysis.property_id)
            .where(Property.redfin_id.is_not(None))
        )
    ).all()
    presence_rows = (
        await session.execute(
            select(ListingPresence.property_id, ListingPresence.market_slug).where(
                ListingPresence.property_id.is_not(None)
            )
        )
    ).all()
    markets = {property_id: slug for property_id, slug in presence_rows}
    previous_rows = (await session.execute(select(AnalysisTier))).scalars().all()
    previous: dict[tuple[UUID, str], Any] = {
        (cast(UUID, row.property_id), str(row.lens)): row for row in previous_rows
    }

    initialized = 0
    tier_events = 0
    score_moves = 0
    now = utc_now()
    for analysis, redfin_id in rows:
        analysis_row: Any = analysis
        for lens in LENSES:
            percentile = cast(float | None, getattr(analysis_row, f"{lens}_percentile"))
            score = cast(float | None, getattr(analysis_row, f"{lens}_score"))
            tier = tier_for_percentile(percentile)
            snapshot = TierSnapshot(
                property_id=cast(UUID, analysis_row.property_id),
                redfin_id=str(redfin_id),
                market_slug=markets.get(analysis_row.property_id),
                lens=lens,
                score=score,
                percentile=percentile,
                tier=tier,
            )
            prior = previous.get((snapshot.property_id, lens))
            if prior is None:
                prior = AnalysisTier(property_id=snapshot.property_id, lens=lens)
                session.add(prior)
                initialized += 1
            elif snapshot.market_slug is not None:
                direction = tier_direction(str(prior.tier), tier)
                if direction is not None:
                    session.add(
                        _event(
                            snapshot,
                            direction,
                            float(prior.percentile)
                            if prior.percentile is not None
                            else None,
                        )
                    )
                    tier_events += 1
                if (
                    prior.percentile is not None
                    and percentile is not None
                    and abs(float(percentile) - float(prior.percentile)) >= 10
                ):
                    session.add(_event(snapshot, "score_move", float(prior.percentile)))
                    score_moves += 1
            mutable_prior: Any = prior
            mutable_prior.score = score
            mutable_prior.percentile = percentile
            mutable_prior.tier = tier
            mutable_prior.computed_at = now

    await session.commit()
    logger.info(
        "Tier state updated: initialized=%s tier_events=%s score_moves=%s",
        initialized,
        tier_events,
        score_moves,
    )
    return {
        "initialized": initialized,
        "tier_events": tier_events,
        "score_moves": score_moves,
    }


def _event(snapshot: TierSnapshot, kind: str, previous_percentile: float | None) -> ChangeEvent:
    if snapshot.market_slug is None:
        raise ValueError("Tier event requires a checked market")
    return ChangeEvent(
        property_id=snapshot.property_id,
        market_slug=snapshot.market_slug,
        redfin_id=snapshot.redfin_id,
        kind=kind,
        detail={
            "lens": snapshot.lens,
            "previous_percentile": previous_percentile,
            "percentile": snapshot.percentile,
            "tier": snapshot.tier,
            "score": snapshot.score,
        },
        observed_at=utc_now(),
    )
