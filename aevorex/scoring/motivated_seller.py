"""
Motivated-seller scorer — a seller-PRESSURE index.

WHAT CHANGED AND WHY
--------------------
The previous version proxied motivation from four listing-behaviour signals
and stated in its own docstring that no ownership or legal-distress data
existed in the schema. That is no longer true: `is_foreclosure`, `is_reo`,
`is_short_sale`, `is_probate_or_estate` and `is_auction` are structured
columns populated for every property with listing text. Distress is now the
single heaviest input, because it is a fact about the seller's circumstances
rather than an inference from their pricing.

Three other things changed:

1. **Cut velocity, not cut count.** Three price cuts in 60 days is a seller
   capitulating; three over two years is one slowly discovering the market.
   The old scorer scored them identically.
2. **Time on market is measured against the local median**, not an absolute
   day count. Median DOM by zip in this corpus ranges from 1 to 29 days, so
   "90 days" means completely different things in different markets.
3. **Discount is measured against our own comp-derived value**, available for
   ~99% of properties, rather than the source AVM at 57%.

The distress types are deliberately NOT interchangeable. REO scores highest
because the seller is an unemotional institution and the closing is normal.
A short sale is genuinely motivated but the lender must approve, which means
60-180 days and a real chance of collapse — motivated is not the same as
executable, and a scorer that conflates them sends investors at deals they
cannot close.
"""

from datetime import datetime, timezone
from typing import List, Optional

from aevorex.db.event_types import OFF_MARKET_TYPES, REDUCED, RELISTED
from aevorex.scoring.base import BaseScorer, ScoreResult, ScoringContext
from aevorex.scoring.curves import (
    apply_gate,
    clamp,
    confidence_adjusted,
    linear_ramp,
    weighted_blend,
)
from aevorex.scoring.utils import event_kind, original_list_price, sale_events, scan_keywords


class MotivatedSellerScorer(BaseScorer):
    STRATEGY_KEY = "motivated_seller"

    def score(self, ctx: ScoringContext) -> ScoreResult:
        cfg = ctx.config.motivated_seller
        prop = ctx.property
        flags: List[str] = []
        factors: dict = {}

        if ctx.config.skip_placeholder_prices and prop.price_is_placeholder:
            return self.unscoreable(
                "Not scored — the listed price is a placeholder (an auction deposit or "
                "opening bid), not a market asking price, so every pricing signal here "
                "would be measured against a number that does not mean what it appears to.",
                ["price_is_a_placeholder_not_a_market_asking_price"],
            )

        distress = self._distress_score(ctx, flags, factors)
        cuts = self._price_cut_score(ctx, flags, factors)
        dom = self._dom_score(ctx, flags, factors)
        discount = self._discount_score(ctx, flags, factors)
        keywords = self._keyword_score(ctx, flags, factors)

        blended, coverage = weighted_blend([
            (distress, cfg.distress_weight),
            (cuts, cfg.price_cut_weight),
            (dom, cfg.dom_weight),
            (discount, cfg.discount_weight),
            (keywords, cfg.keyword_weight),
        ])

        if blended is None:
            return self.unscoreable(
                "Couldn't produce a motivated-seller score — no distress status, price "
                "history, time on market, valuation or listing remarks were available.",
                flags + ["insufficient_data_for_motivated_seller_score"], factors,
            )

        # Occupancy modifiers: a vacant property is costing the owner money
        # every month with no income; a tenanted one is harder to sell.
        if prop.is_vacant:
            blended *= cfg.vacant_multiplier
            factors["vacant_modifier_applied"] = cfg.vacant_multiplier
        if prop.is_tenant_occupied:
            blended *= cfg.tenant_occupied_multiplier
            factors["tenant_occupied_modifier_applied"] = cfg.tenant_occupied_multiplier

        final = clamp(confidence_adjusted(blended, coverage))
        factors["signal_coverage"] = coverage

        return ScoreResult(
            score=round(final, 2),
            rationale=self._rationale(ctx, factors, final),
            factors=factors,
            data_quality_flags=flags,
            confidence=round(coverage, 3),
        )

    # ------------------------------------------------------------------

    def _distress_score(self, ctx: ScoringContext, flags: list, factors: dict) -> Optional[float]:
        """Verified distress status — the strongest available signal."""
        cfg = ctx.config.motivated_seller
        prop = ctx.property

        statuses = {
            "reo": prop.is_reo,
            "probate_or_estate": prop.is_probate_or_estate,
            "short_sale": prop.is_short_sale,
            "foreclosure": prop.is_foreclosure,
            "auction": prop.is_auction,
        }
        present = [name for name, value in statuses.items() if value]

        if all(value is None for value in statuses.values()):
            # No listing text at all, so distress could not be assessed —
            # distinct from having looked and found none.
            flags.append("no_listing_text_distress_status_unknown")
            return None

        if not present:
            return 0.0

        factors["distress_signals"] = present
        # Take the strongest rather than summing: a property that is both a
        # foreclosure and an auction is one situation described twice, not two
        # independent reasons to buy.
        best = max(cfg.distress_points.get(name, 0.0) for name in present)
        return best

    def _price_cut_score(self, ctx: ScoringContext, flags: list, factors: dict) -> Optional[float]:
        """Velocity, depth and recency of price reductions."""
        cfg = ctx.config.motivated_seller
        events = sale_events(ctx.price_history)
        if not events:
            flags.append("no_price_history")
            return None

        reductions = [e for e in events if event_kind(e) == REDUCED]
        relisted = any(event_kind(e) == RELISTED for e in events)
        off_market = any(event_kind(e) in OFF_MARKET_TYPES for e in events)

        dom = ctx.property.days_on_market or 0
        # Velocity: cuts per 30 days on market. Guarded at 30 days so a
        # brand-new listing with one cut doesn't read as infinite urgency.
        months_on_market = max(dom, 30) / 30.0
        velocity = len(reductions) / months_on_market
        velocity_score = linear_ramp(velocity, 0.0, cfg.cuts_per_30d_for_full_score)

        original = original_list_price(ctx.price_history)
        depth_pct = None
        if original and ctx.property.price and original > 0:
            depth_pct = max(0.0, (original - ctx.property.price) / original * 100)
        depth_score = linear_ramp(depth_pct, 0.0, cfg.cumulative_cut_pct_for_full_score)

        recency_score = 0.0
        if reductions:
            latest = max(e.event_date for e in reductions if e.event_date)
            days_since = (datetime.now(timezone.utc).replace(tzinfo=None) - latest).days
            factors["days_since_last_price_cut"] = days_since
            # Full marks for a cut inside the window, tapering to zero at 6x it.
            recency_score = linear_ramp(
                -days_since, -cfg.recent_cut_days * 6, -cfg.recent_cut_days
            ) or 0.0

        blended, _ = weighted_blend([
            (velocity_score, cfg.cut_velocity_weight),
            (depth_score, cfg.cut_depth_weight),
            (recency_score, cfg.cut_recency_weight),
        ])
        score = blended or 0.0

        # A listing that was pulled and relisted, or withdrawn without
        # selling, is direct evidence it failed to sell — the strongest
        # behavioural signal in the set.
        if relisted:
            score += cfg.relisted_bonus
        if off_market:
            score += cfg.off_market_bonus

        factors.update({
            "price_reduction_count": len(reductions),
            "price_cuts_per_30_days": round(velocity, 3),
            "cumulative_price_cut_pct": round(depth_pct, 1) if depth_pct is not None else None,
            "original_list_price": original,
            "relisted": relisted,
            "previously_withdrawn": off_market,
        })
        return clamp(score)

    def _dom_score(self, ctx: ScoringContext, flags: list, factors: dict) -> Optional[float]:
        """Time on market, relative to the local median wherever possible."""
        cfg = ctx.config.motivated_seller
        dom = ctx.property.days_on_market
        if dom is None:
            flags.append("no_days_on_market")
            return None
        factors["days_on_market"] = dom

        median_dom = (ctx.market or {}).get("median_dom")
        if median_dom and median_dom > 0:
            ratio = dom / float(median_dom)
            factors["dom_vs_market_median"] = round(ratio, 2)
            factors["market_median_dom"] = round(float(median_dom), 1)
            return linear_ramp(ratio, 1.0, cfg.dom_ratio_full_score)

        flags.append("no_market_dom_baseline_scored_on_absolute_days")
        return linear_ramp(dom, 0, cfg.dom_absolute_fallback_days)

    def _discount_score(self, ctx: ScoringContext, flags: list, factors: dict) -> Optional[float]:
        """How far below our own comp-derived value the property is listed."""
        cfg = ctx.config.motivated_seller
        value = ctx.market_value()
        price = ctx.property.price
        if not value or not price:
            flags.append("no_valuation_for_discount_signal")
            return None

        discount_pct = (value - price) / value * 100
        factors["estimated_market_value"] = round(value)
        factors["pct_below_estimated_value"] = round(discount_pct, 1)
        factors["valuation_method"] = (
            ctx.valuation.market_value_method if ctx.valuation else None
        )
        # Priced at or above value contributes nothing rather than being
        # penalised — most listings are, and it says little about motivation.
        return linear_ramp(discount_pct, 0.0, cfg.discount_pct_for_full_score)

    def _keyword_score(self, ctx: ScoringContext, flags: list, factors: dict) -> Optional[float]:
        """
        Remark language. Lowest weight by design.

        Agents write "motivated seller" to attract attention as often as to
        report a fact, so this is the most gameable signal here and is
        weighted accordingly — 8%, against 34% for verified distress.
        """
        cfg = ctx.config.motivated_seller
        text = " ".join(filter(None, [ctx.property.description, ctx.property.ai_summary]))
        if not text.strip():
            flags.append("no_description_text")
            return None

        high = scan_keywords(text, cfg.high_signal_keywords)
        medium = scan_keywords(text, cfg.medium_signal_keywords)
        if high:
            factors["high_signal_keywords"] = high
        if medium:
            factors["medium_signal_keywords"] = medium

        return clamp(len(high) * cfg.high_keyword_points + len(medium) * cfg.medium_keyword_points)

    # ------------------------------------------------------------------

    def _rationale(self, ctx: ScoringContext, factors: dict, score: float) -> str:
        parts: List[str] = []

        signals = factors.get("distress_signals")
        if signals:
            readable = ", ".join(s.replace("_", " ") for s in signals)
            parts.append(f"Verified distress: {readable}.")

        cuts = factors.get("price_reduction_count")
        if cuts:
            depth = factors.get("cumulative_price_cut_pct")
            velocity = factors.get("price_cuts_per_30_days")
            sentence = f"{cuts} price cut{'s' if cuts > 1 else ''}"
            if depth:
                sentence += f" totalling {depth}% off the original list price"
            if velocity and velocity >= 0.4:
                sentence += f" — {velocity} per 30 days on market, a fast-moving seller"
            parts.append(sentence + ".")

        if factors.get("previously_withdrawn"):
            parts.append("Previously withdrawn without selling.")
        elif factors.get("relisted"):
            parts.append("Pulled and relisted, so it failed to sell the first time.")

        dom = factors.get("days_on_market")
        ratio = factors.get("dom_vs_market_median")
        if dom is not None and ratio is not None:
            if ratio >= 1.5:
                parts.append(
                    f"On market {dom} days — {ratio}x the local median of "
                    f"{factors.get('market_median_dom')}."
                )
            else:
                parts.append(f"On market {dom} days, around the local norm.")
        elif dom is not None:
            parts.append(f"On market {dom} days (no local baseline to compare against).")

        below = factors.get("pct_below_estimated_value")
        if below is not None and below > 2:
            parts.append(
                f"Listed {below}% below our comp-derived value of "
                f"${factors.get('estimated_market_value'):,.0f}."
            )
        elif below is not None and below < -5:
            parts.append("Listed above our comp-derived value — no underpricing signal.")

        if factors.get("high_signal_keywords"):
            parts.append(f"Remarks: \"{factors['high_signal_keywords'][0]}\".")

        if not parts:
            parts.append("No distress status, pricing pressure or urgency language found.")

        coverage = factors.get("signal_coverage")
        if coverage is not None and coverage < 0.6:
            parts.append(
                f"Based on {round(coverage * 100)}% of the available signals — "
                "treat as provisional."
            )

        return f"Motivated-seller score {round(score)}/100. " + " ".join(parts)
