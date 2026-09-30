"""
Motivated-seller scorer — a seller-PRESSURE index.

WHAT CHANGED AND WHY (v3)
-------------------------
Three defects found in the live ranking, each fixed here:

1. **Price cuts were counted across the property's entire history** and
   divided by the current days on market. A Jacksonville listing three days
   old showed "2.0 cuts per 30 days" from a reduction made 2,484 days
   earlier. Cuts, depth and recency are now measured inside the CURRENT
   listing cycle only (from the most recent listed/relisted event).

2. **A single weak signal could reach 80/100.** Five San Jose coming-soon
   listings ranked in the global top seven on 14% signal coverage, because
   the generic confidence floor keeps 55% of the deviation from 50 no matter
   how little evidence there is. Below a coverage threshold the floor now
   drops sharply, so one input cannot carry a lead to the top.

3. **Equity was ignored.** A seller who bought in 2022 at the top cannot
   take a 15% discount however motivated they sound; one who bought in 2009
   can. The last sold event (present on 86% of properties) now feeds an
   equity signal, with an extra bonus for a loss seller — asking below what
   they paid within the last five years.

Also new: listing churn. Redfin's listing number changes with each new
listing agreement, so three distinct numbers in three years is a seller
cycling agents — a signal every broker reads as ready-to-deal.

The distress types remain deliberately NOT interchangeable. REO scores
highest because the seller is an unemotional institution and the closing is
normal. A short sale is genuinely motivated but the lender must approve —
motivated is not the same as executable.
"""

from datetime import datetime, timezone
from typing import List, Optional

from aevorex.db.event_types import LISTED, OFF_MARKET_TYPES, REDUCED, RELISTED
from aevorex.scoring.base import BaseScorer, ScoreResult, ScoringContext, component, make_breakdown
from aevorex.scoring.config import letter_grade
from aevorex.scoring.curves import (
    clamp,
    confidence_adjusted,
    linear_ramp,
    weighted_blend,
)
from aevorex.scoring.utils import (
    current_listing_cycle,
    event_kind,
    last_sale,
    listing_runs,
    original_list_price,
    sale_events,
    scan_keywords,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MotivatedSellerScorer(BaseScorer):
    STRATEGY_KEY = "motivated_seller"

    def score(self, ctx: ScoringContext) -> ScoreResult:
        cfg = ctx.config.motivated_seller
        prop = ctx.property
        flags: List[str] = []
        factors: dict = {}
        risks: List[str] = []

        if ctx.config.skip_placeholder_prices and prop.price_is_placeholder:
            return self.with_breakdown(ctx, self.unscoreable(
                "Not scored — the listed price is a placeholder (an auction deposit or "
                "opening bid), not a market asking price, so every pricing signal here "
                "would be measured against a number that does not mean what it appears to.",
                ["price_is_a_placeholder_not_a_market_asking_price"],
            ))

        distress = self._distress_score(ctx, flags, factors, risks)
        cuts = self._price_cut_score(ctx, flags, factors)
        dom = self._dom_score(ctx, flags, factors)
        discount = self._discount_score(ctx, flags, factors, risks)
        equity = self._equity_score(ctx, flags, factors, risks)
        keywords = self._keyword_score(ctx, flags, factors)

        blended, coverage = weighted_blend([
            (distress, cfg.distress_weight),
            (cuts, cfg.price_cut_weight),
            (dom, cfg.dom_weight),
            (discount, cfg.discount_weight),
            (equity, cfg.equity_weight),
            (keywords, cfg.keyword_weight),
        ])

        if blended is None:
            return self.with_breakdown(ctx, self.unscoreable(
                "Couldn't produce a motivated-seller score — no distress status, price "
                "history, time on market, valuation or listing remarks were available.",
                flags + ["insufficient_data_for_motivated_seller_score"], factors,
            ))

        # Occupancy modifiers: a vacant property is costing the owner money
        # every month with no income; a tenanted one is harder to sell.
        if prop.is_vacant:
            blended *= cfg.vacant_multiplier
            factors["vacant_modifier_applied"] = cfg.vacant_multiplier
        if prop.is_tenant_occupied:
            blended *= cfg.tenant_occupied_multiplier
            factors["tenant_occupied_modifier_applied"] = cfg.tenant_occupied_multiplier

        # Thin evidence gets a much weaker floor: with one signal out of six
        # the score should sit near 50, not near 80.
        floor = 0.55 if coverage >= cfg.thin_coverage_threshold else cfg.thin_coverage_floor
        if coverage < cfg.thin_coverage_threshold:
            flags.append("thin_signal_coverage_score_held_near_neutral")
        final = clamp(confidence_adjusted(blended, coverage, floor=floor))
        factors["signal_coverage"] = coverage

        result = ScoreResult(
            score=round(final, 2),
            rationale=self._rationale(ctx, factors, final),
            factors=factors,
            data_quality_flags=flags,
            confidence=round(coverage, 3),
            grade=letter_grade(final),
            top_risks=risks[:3],
            component_scores={
                "distress": distress, "price_cuts": cuts, "days_on_market": dom,
                "discount": discount, "equity": equity, "keywords": keywords,
            },
            score_adjustments=[
                {"type": "multiplier", "label": "Vacant property",
                 "value": cfg.vacant_multiplier, "applied": bool(prop.is_vacant)},
                {"type": "multiplier", "label": "Tenant occupied",
                 "value": cfg.tenant_occupied_multiplier,
                 "applied": bool(prop.is_tenant_occupied)},
                {"type": "confidence_shrink", "label": "Evidence coverage",
                 "value": coverage, "floor": floor, "applied": True},
            ],
        )
        return self.with_breakdown(ctx, result)

    def breakdown(self, ctx: ScoringContext, result: ScoreResult) -> dict:
        cfg = ctx.config.motivated_seller
        scores, f = result.component_scores, result.factors
        top = list(result.score_adjustments) or [
            {"type": "multiplier", "label": "Vacant property",
             "value": cfg.vacant_multiplier, "applied": False},
            {"type": "multiplier", "label": "Tenant occupied",
             "value": cfg.tenant_occupied_multiplier, "applied": False},
            {"type": "confidence_shrink", "label": "Evidence coverage", "value": 0.0,
             "floor": cfg.thin_coverage_floor, "applied": False},
        ]
        adjustments = [
            {"type": "component_bonus", "label": "Relisted", "value": cfg.relisted_bonus,
             "applied": bool(f.get("relisted")), "component": "price_cuts"},
            {"type": "component_bonus", "label": "Previously withdrawn",
             "value": cfg.off_market_bonus, "applied": bool(f.get("previously_withdrawn")),
             "component": "price_cuts"},
            {"type": "component_bonus", "label": "Two listing agreements",
             "value": cfg.churn_two_runs_bonus,
             "applied": f.get("listing_agreements_in_window") == 2,
             "component": "price_cuts"},
            {"type": "component_bonus", "label": "Three or more listing agreements",
             "value": cfg.churn_three_runs_bonus,
             "applied": (f.get("listing_agreements_in_window") or 0) >= 3,
             "component": "price_cuts"},
            *top,
        ]
        specs = [
            ("distress", "Distress signals", cfg.distress_weight,
             [("Listing distress signals", f.get("distress_signals", []), "distress_signals")]),
            ("price_cuts", "Current-listing price pressure", cfg.price_cut_weight, [
                ("Price reductions", f.get("price_reduction_count", 0), "price_reduction_count"),
                ("Cuts per 30 days", f.get("price_cuts_per_30_days"), "price_cuts_per_30_days"),
                ("Cumulative cut percent", f.get("cumulative_price_cut_pct"), "cumulative_price_cut_pct"),
                ("Listing agreements", f.get("listing_agreements_in_window"), "listing_agreements_in_window"),
            ]),
            ("days_on_market", "Time on market", cfg.dom_weight,
             [("Days on market", f.get("days_on_market"), "days_on_market"),
              ("Ratio to market median", f.get("dom_vs_market_median"), "dom_vs_market_median")]),
            ("discount", "Discount to estimated value", cfg.discount_weight,
             [("Percent below estimated value", f.get("pct_below_estimated_value"), "pct_below_estimated_value")]),
            ("equity", "Seller equity and loss pressure", cfg.equity_weight,
             [("Implied equity percent", f.get("implied_equity_pct"), "implied_equity_pct"),
              ("Recent loss seller", f.get("loss_seller", False), "loss_seller")]),
            ("keywords", "Listing urgency language", cfg.keyword_weight,
             [("High-signal phrases", f.get("high_signal_keywords", []), "description"),
              ("Medium-signal phrases", f.get("medium_signal_keywords", []), "description")]),
        ]
        components = [component(key, label, weight, scores.get(key), [
            {"label": dl, "value": value, "field": field} for dl, value, field in drivers
        ]) for key, label, weight, drivers in specs]
        return make_breakdown(lens=self.STRATEGY_KEY, version=ctx.config.version,
                              result=result, components=components, adjustments=adjustments)

    # ------------------------------------------------------------------

    def _distress_score(self, ctx: ScoringContext, flags: list, factors: dict, risks: list) -> Optional[float]:
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
        if "short_sale" in present:
            risks.append("Short sale: lender approval needed, expect 60-180 days and a real chance of collapse.")
        if "auction" in present or "foreclosure" in present:
            risks.append("Auction/foreclosure: cash-only, no inspection, possible occupants and junior liens.")
        # Take the strongest rather than summing: a property that is both a
        # foreclosure and an auction is one situation described twice, not two
        # independent reasons to buy.
        best = max(cfg.distress_points.get(name, 0.0) for name in present)
        return best

    def _price_cut_score(self, ctx: ScoringContext, flags: list, factors: dict) -> Optional[float]:
        """Velocity, depth and recency of price reductions in the CURRENT cycle."""
        cfg = ctx.config.motivated_seller
        all_events = sale_events(ctx.price_history)
        if not all_events:
            flags.append("no_price_history")
            return None
        cycle = current_listing_cycle(ctx.price_history)

        reductions = [e for e in cycle if event_kind(e) == REDUCED]
        # Relist and withdrawal are by nature about PREVIOUS cycles, so they
        # are read from the whole history.
        relisted = any(event_kind(e) == RELISTED for e in all_events)
        off_market = any(event_kind(e) in OFF_MARKET_TYPES for e in all_events)

        dom = ctx.property.days_on_market or 0
        # Velocity: cuts per 30 days on market. Guarded at 30 days so a
        # brand-new listing with one cut doesn't read as infinite urgency.
        months_on_market = max(dom, 30) / 30.0
        velocity = len(reductions) / months_on_market
        velocity_score = linear_ramp(velocity, 0.0, cfg.cuts_per_30d_for_full_score)

        # Depth is measured against the price this cycle OPENED at. A relist
        # carries no price of its own, so in that case the seller's previous
        # list price is the honest baseline — not the first reduction in the
        # cycle, which would make the cut measure itself.
        opening = cycle[0] if cycle else None
        if opening is not None and event_kind(opening) in (LISTED, RELISTED) and opening.price:
            original = opening.price
        else:
            original = original_list_price(ctx.price_history)
        depth_pct = None
        if original and ctx.property.price and original > 0:
            if original < ctx.property.price * cfg.min_original_price_ratio:
                # A $24,000 "original list price" on a $250,000 house is a
                # lot listing or a typo, not a 90% cut.
                flags.append("original_list_price_implausible_ignored")
                original = None
            else:
                depth_pct = max(0.0, (original - ctx.property.price) / original * 100)
        depth_score = linear_ramp(depth_pct, 0.0, cfg.cumulative_cut_pct_for_full_score)

        recency_score = 0.0
        if reductions:
            latest = max(e.event_date for e in reductions if e.event_date)
            days_since = (_now() - latest).days
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

        # Listing churn: distinct listing agreements in the window.
        runs = listing_runs(ctx.price_history, cfg.churn_window_months)
        if runs >= 3:
            score += cfg.churn_three_runs_bonus
        elif runs == 2:
            score += cfg.churn_two_runs_bonus

        factors.update({
            "price_reduction_count": len(reductions),
            "price_cuts_per_30_days": round(velocity, 3),
            "cumulative_price_cut_pct": round(depth_pct, 1) if depth_pct is not None else None,
            "original_list_price": original,
            "relisted": relisted,
            "previously_withdrawn": off_market,
            "listing_agreements_in_window": runs,
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

    def _discount_score(self, ctx: ScoringContext, flags: list, factors: dict, risks: list) -> Optional[float]:
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
        if discount_pct < -10:
            risks.append(
                f"Asking is {abs(discount_pct):.0f}% ABOVE our estimate of value — "
                "motivated or not, the price is not yet a deal."
            )
        # Priced at or above value contributes nothing rather than being
        # penalised — most listings are, and it says little about motivation.
        return linear_ramp(discount_pct, 0.0, cfg.discount_pct_for_full_score)

    def _equity_score(self, ctx: ScoringContext, flags: list, factors: dict, risks: list) -> Optional[float]:
        """
        Can this seller afford to take a discount?

        Implied equity is our value less the assumed remaining loan on what
        they paid. It is deliberately crude — no amortisation, no refinance
        knowledge — because the question it answers is coarse: is there room
        to negotiate at all. A loss seller (asking below their own purchase
        price, bought recently) is the strongest form of pressure short of a
        lender being involved and earns a bonus on top.
        """
        cfg = ctx.config.motivated_seller
        sale = last_sale(ctx.price_history)
        value = ctx.market_value() or ctx.property.price
        if not sale or not value:
            flags.append("no_prior_sale_equity_unknown")
            return None
        paid, sold_at = sale
        years_held = max(0.0, (_now() - sold_at).days / 365.25)

        if years_held >= cfg.equity_full_after_years:
            equity_pct = 1.0
        else:
            loan = paid * cfg.assumed_purchase_ltv
            equity_pct = (value - loan) / value

        score = linear_ramp(
            equity_pct, 0.0, cfg.equity_pct_for_full_score, 0.0, cfg.equity_ability_max_score
        ) or 0.0

        loss_seller = (
            ctx.property.price is not None
            and years_held <= cfg.loss_seller_window_years
            and ctx.property.price < paid
        )
        if loss_seller:
            score = max(score, cfg.loss_seller_score)

        factors.update({
            "last_sold_price": paid,
            "last_sold_years_ago": round(years_held, 1),
            "implied_equity_pct": round(equity_pct * 100, 1),
            "loss_seller": loss_seller,
        })
        if equity_pct < 0.10 and not loss_seller:
            risks.append("Little implied equity: seller may be unable to accept a discount without lender involvement.")
        return score

    def _keyword_score(self, ctx: ScoringContext, flags: list, factors: dict) -> Optional[float]:
        """
        Remark language. Lowest weight by design.

        Agents write "motivated seller" to attract attention as often as to
        report a fact, so this is the most gameable signal here and is
        weighted accordingly — 8%, against 30% for verified distress.
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
            sentence = f"{cuts} price cut{'s' if cuts > 1 else ''} this listing"
            if depth:
                sentence += f" totalling {depth}% off the original list price"
            if velocity and velocity >= 0.4:
                sentence += f" — {velocity} per 30 days on market, a fast-moving seller"
            parts.append(sentence + ".")

        if factors.get("previously_withdrawn"):
            parts.append("Previously withdrawn without selling.")
        elif factors.get("relisted"):
            parts.append("Pulled and relisted, so it failed to sell the first time.")
        runs = factors.get("listing_agreements_in_window") or 0
        if runs >= 3:
            parts.append(f"{runs} separate listing agreements in three years — the seller has cycled agents.")

        if factors.get("loss_seller"):
            parts.append(
                f"Asking below the ${factors['last_sold_price']:,.0f} they paid "
                f"{factors['last_sold_years_ago']} years ago — a loss seller."
            )
        elif factors.get("implied_equity_pct") is not None:
            parts.append(f"Implied equity about {factors['implied_equity_pct']:.0f}% of value.")

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

        grade = letter_grade(score)
        return f"Motivated-seller grade {grade} ({round(score)}/100). " + " ".join(parts)
