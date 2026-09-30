"""
Fix & flip scorer — a full underwrite, ranked the way a flipper ranks.

WHAT CHANGED AND WHY (v3)
-------------------------
The v2 ranking was owned by valuation error. Its top ten were all San Jose
townhouses "worth" 30-80% more than asking, because a tight-but-wrong comp
set (detached Cupertino houses) produced a huge ARV, and levered ROI — profit
over a small cash slice — turned that error into a 300% return. Corpus-wide
the valuation was well calibrated; the problem is that sorting by ROI selects
precisely for the tail where it is not.

Four changes:

1. **Ranked by gap to offer, not by ROI.** Every flipper triages on one
   number: maximum allowable offer versus asking. At or under MAO is a deal,
   within ~10% is a negotiation, 20% over is a pass. That gap now carries
   70% of the score. ROI remains as a 30% secondary input and is still
   reported in full.

2. **Fused with seller pressure.** A property 15% over MAO whose seller is in
   foreclosure and has cut three times is a better lead than one 5% over MAO
   listed yesterday. The motivated-seller score, computed first in the same
   run, scales the flip score within a configurable band.

3. **Winner's-curse gate.** The valuation layer now shrinks and flags a value
   that sits far above asking with nothing corroborating it. Anything still
   carrying that flag, or an AVM disagreement with asking well under value,
   is capped so it cannot lead the list.

4. **Exit liquidity.** ARV $/sqft above the market's upper-quartile sold
   $/sqft means the renovated house would be the most expensive sale on the
   block; a market median DOM over two months means a six-month clock is
   optimistic. Both penalise.

Alongside the score: projected profit, ROI on cash invested, annualised ROI,
the MAO, and the plain-language risks a buyer would forward to a partner.

FLORIDA SPECIFICS
-----------------
Exit liquidity is also adjusted for insurability. A frame-built pre-2002
house with an ageing roof is harder for a buyer to insure and therefore
harder to sell. Era-dated systems (cast iron drains, polybutylene, aluminium
wiring) are priced into the rehab estimate by the valuation layer and named
as risks here.
"""

from typing import List, Optional

from aevorex.scoring.base import BaseScorer, ScoreResult, ScoringContext, component, make_breakdown
from aevorex.scoring.config import letter_grade
from aevorex.scoring.curves import (
    apply_gate,
    clamp,
    confidence_adjusted,
    linear_ramp,
    logistic_score,
    weighted_blend,
)

# Raised by the valuation engine when an auction's listed figure is too far
# below our own valuation to be an asking price.
AUCTION_PRICE_FLAG = "auction_price_is_an_opening_bid_not_a_purchase_price"
# Raised by the valuation engine when our value sits far above asking with
# nothing independent corroborating it.
UNCORROBORATED_FLAG = "value_far_above_asking_uncorroborated_shrunk_toward_asking"
AVM_DIVERGENCE_FLAG = "comp_value_diverges_from_avm"
TYPE_MISMATCH_FLAG = "comp_property_type_mismatch_suspected"

_ERA_SYSTEM_LABELS = {
    "cast_iron_drain_repipe": "Pre-1975 cast iron drains: budget a re-pipe.",
    "polybutylene_repipe": "1978-95 polybutylene supply lines: insurers refuse them, budget a re-pipe.",
    "electrical_panel_and_wiring": "1960-85 electrical: panel replacement and possible rewire.",
    "roof_replacement": "Roof at end of insurable life: replacement priced in.",
    "impact_windows": "No impact glazing on a pre-2002 build: windows priced in.",
}


class FixAndFlipScorer(BaseScorer):
    STRATEGY_KEY = "fix_flip"

    def score(self, ctx: ScoringContext) -> ScoreResult:
        cfg = ctx.config.fix_flip
        prop = ctx.property
        valuation = ctx.valuation
        flags: List[str] = []
        factors: dict = {}
        risks: List[str] = []

        if ctx.config.skip_placeholder_prices and prop.price_is_placeholder:
            return self.with_breakdown(ctx, self.unscoreable(
                "Not scored — the listed price is an auction deposit or opening bid, not "
                "a purchase price, so any margin computed from it would be fictional.",
                ["price_is_a_placeholder_not_a_market_asking_price"],
            ))

        if not prop.price:
            return self.with_breakdown(ctx, self.unscoreable(
                "Couldn't score fix & flip — no listing price.",
                ["no_price_cannot_underwrite"],
            ))
        if valuation is None or not valuation.arv:
            return self.with_breakdown(ctx, self.unscoreable(
                "Couldn't score fix & flip — no after-repair value could be established. "
                "That needs at least three comparable sales or a source valuation, and "
                "neither was available.",
                ["no_arv_cannot_underwrite"],
            ))
        if valuation.rehab_cost_mid is None:
            return self.with_breakdown(ctx, self.unscoreable(
                "Couldn't score fix & flip — no renovation estimate, which needs the "
                "property's floor area.",
                ["no_rehab_estimate_cannot_underwrite"],
            ))

        valuation_flags = list(valuation.data_quality_flags or [])

        # An auction's listed figure is an opening bid, and the price actually
        # paid is settled in the room. Underwriting against it manufactured a
        # 968% return on a $60,000 opening bid for a $205,000 Orlando house.
        if AUCTION_PRICE_FLAG in valuation_flags:
            return self.with_breakdown(ctx, self.unscoreable(
                "Not scored — this is an auction and the listed figure is an opening bid, "
                f"not a purchase price (${prop.price:,.0f} against an estimated "
                f"${valuation.market_value:,.0f} of value). Any return computed from it "
                "would be fictional. See the motivated-seller score for the opportunity "
                "itself.",
                [AUCTION_PRICE_FLAG],
            ))

        # ---- ARV, discounted for how much the comps disagree ----
        arv, haircut = self._risk_adjusted_arv(ctx, factors, flags)

        # ---- the underwrite ----
        purchase = float(prop.price)
        rehab = float(valuation.rehab_cost_mid)
        purchase_closing = purchase * cfg.purchase_closing_pct
        selling_costs = arv * cfg.selling_cost_pct
        holding = self._holding_costs(ctx, cfg)
        financing, cash_invested = self._financing(purchase, rehab, purchase_closing, cfg)

        total_cost = purchase + rehab + purchase_closing + holding + financing + selling_costs
        profit = arv - total_cost
        roi_pct = (profit / cash_invested * 100) if cash_invested > 0 else None
        annualised = (roi_pct * 12 / cfg.hold_months) if roi_pct is not None else None

        # Maximum Allowable Offer: the same underwrite solved backwards for
        # the purchase price that still clears the target return.
        mao = self._max_allowable_offer(arv, rehab, holding, cfg)
        gap_pct = ((mao - purchase) / purchase * 100) if mao else None

        factors.update({
            "arv": round(arv),
            "arv_method": valuation.arv_method,
            "purchase_price": round(purchase),
            "rehab_cost_mid": round(rehab),
            "rehab_cost_range": [valuation.rehab_cost_low, valuation.rehab_cost_high],
            "condition_class": valuation.condition_class,
            "purchase_closing_costs": round(purchase_closing),
            "holding_costs": round(holding),
            "financing_costs": round(financing),
            "selling_costs": round(selling_costs),
            "total_cost": round(total_cost),
            "projected_profit": round(profit),
            "cash_invested": round(cash_invested),
            "roi_pct": round(roi_pct, 1) if roi_pct is not None else None,
            "annualised_roi_pct": round(annualised, 1) if annualised is not None else None,
            "max_allowable_offer": round(mao) if mao else None,
            "offer_vs_asking_pct": round(gap_pct, 1) if gap_pct is not None else None,
            "hold_months": cfg.hold_months,
            "financed": cfg.use_financing,
        })

        # ---- score: gap to offer first, return second ----
        gap_score = logistic_score(gap_pct, cfg.gap_midpoint_pct, cfg.gap_steepness)
        roi_score = logistic_score(
            roi_pct, cfg.roi_midpoint_pct, cfg.roi_steepness,
            tail_weight=cfg.roi_tail_weight, tail_span=cfg.roi_tail_span,
        )
        if mao is None:
            # The 70% rule leaves nothing after repairs and carry: there is
            # no price at which this is a flip.
            gap_score = 0.0
            flags.append("no_positive_offer_price_under_70pct_rule")
        base, _ = weighted_blend([(gap_score, cfg.gap_weight), (roi_score, cfg.roi_weight)])
        if base is None:
            return self.unscoreable(
                "Couldn't score fix & flip — the underwrite produced no return figure.",
                flags + ["underwrite_incomplete"], factors,
            )

        # ---- seller pressure fusion ----
        before_pressure = base
        base = self._apply_pressure(ctx, base, factors)
        pressure_multiplier = base / before_pressure if before_pressure else 1.0

        # ---- exit penalties ----
        penalty = self._insurability_penalty(ctx, factors, flags, risks)
        penalty += self._exit_liquidity_penalty(ctx, arv, factors, flags, risks)
        exit_multiplier = max(0.0, 1 - min(penalty, 0.45))
        base *= exit_multiplier

        # ---- winner's-curse gate ----
        cap = None
        if UNCORROBORATED_FLAG in valuation_flags:
            cap = cfg.uncorroborated_value_cap
            flags.append("value_uncorroborated_score_capped")
            risks.insert(0, "Our value sits far above asking with nothing corroborating it — verify the comps before trusting the margin.")
        elif AVM_DIVERGENCE_FLAG in valuation_flags and valuation.price_to_value_ratio and valuation.price_to_value_ratio < 0.8:
            cap = cfg.uncorroborated_value_cap
            flags.append("comp_value_disputed_by_avm_score_capped")
            risks.insert(0, "The source AVM disagrees sharply with our comp value — one of them is wrong.")
        if TYPE_MISMATCH_FLAG in valuation_flags:
            cap = min(cap or 100.0, cfg.type_mismatch_cap)
            flags.append("comp_type_mismatch_score_capped")
            risks.append("Comps appear to be a different property type from the subject — the ARV may not transfer.")
        base = apply_gate(base, cap)

        # ---- confidence ----
        confidence = float(valuation.valuation_confidence or 0.3)
        if valuation.rehab_basis and "condition:" not in str(
            (valuation.rehab_basis or {}).get("base_psf_basis", "")
        ):
            confidence *= 0.85
            flags.append("rehab_estimated_from_age_not_stated_condition")
        self._era_system_risks(valuation, risks)

        final = clamp(confidence_adjusted(base, confidence, floor=cfg.confidence_floor))
        factors["valuation_confidence"] = round(confidence, 3)
        factors["comp_dispersion_cv"] = valuation.comp_dispersion_cv
        if haircut:
            factors["arv_dispersion_haircut_pct"] = round(haircut * 100, 1)
        factors["top_risks"] = risks[:3]

        result = ScoreResult(
            score=round(final, 2),
            rationale=self._rationale(ctx, factors, final),
            factors=factors,
            data_quality_flags=flags + valuation_flags[:4],
            confidence=round(confidence, 3),
            grade=letter_grade(final),
            top_risks=risks[:3],
            component_scores={"offer_gap": gap_score, "levered_return": roi_score},
            score_adjustments=[
                {"type": "input_haircut", "label": "Comparable-sale dispersion haircut",
                 "value": haircut, "applied": haircut > 0, "component": "offer_gap"},
                {"type": "multiplier", "label": "Seller pressure",
                 "value": pressure_multiplier,
                 "applied": "seller_pressure_modifier" in factors},
                {"type": "multiplier", "label": "Exit and insurability risk",
                 "value": exit_multiplier, "applied": penalty > 0},
                {"type": "gate", "label": "Uncorroborated value",
                 "value": cfg.uncorroborated_value_cap,
                 "applied": UNCORROBORATED_FLAG in valuation_flags or
                 (AVM_DIVERGENCE_FLAG in valuation_flags and bool(valuation.price_to_value_ratio)
                  and valuation.price_to_value_ratio < 0.8)},
                {"type": "gate", "label": "Comparable property-type mismatch",
                 "value": cfg.type_mismatch_cap, "applied": TYPE_MISMATCH_FLAG in valuation_flags},
                {"type": "cap", "label": "Effective valuation-risk cap",
                 "value": cap or 100.0, "applied": cap is not None},
                {"type": "confidence_shrink", "label": "Valuation confidence",
                 "value": confidence, "floor": cfg.confidence_floor, "applied": True},
            ],
        )
        return self.with_breakdown(ctx, result)

    def breakdown(self, ctx: ScoringContext, result: ScoreResult) -> dict:
        cfg = ctx.config.fix_flip
        f, scores = result.factors, result.component_scores
        adjustments = list(result.score_adjustments) or [
            {"type": "input_haircut", "label": "Comparable-sale dispersion haircut",
             "value": 0.0, "applied": False, "component": "offer_gap"},
            {"type": "multiplier", "label": "Seller pressure", "value": 1.0,
             "applied": False},
            {"type": "multiplier", "label": "Exit and insurability risk", "value": 1.0,
             "applied": False},
            {"type": "gate", "label": "Uncorroborated value",
             "value": cfg.uncorroborated_value_cap, "applied": False},
            {"type": "gate", "label": "Comparable property-type mismatch",
             "value": cfg.type_mismatch_cap, "applied": False},
            {"type": "cap", "label": "Effective valuation-risk cap", "value": 100.0,
             "applied": False},
            {"type": "confidence_shrink", "label": "Valuation confidence", "value": 0.0,
             "floor": cfg.confidence_floor, "applied": False},
        ]
        components = [
            component("offer_gap", "Maximum-offer gap", cfg.gap_weight,
                      scores.get("offer_gap"), [
                {"label": "Maximum allowable offer", "value": f.get("max_allowable_offer"),
                 "field": "max_allowable_offer"},
                {"label": "Offer gap versus asking", "value": f.get("offer_vs_asking_pct"),
                 "field": "offer_vs_asking_pct"},
            ]),
            component("levered_return", "Levered flip return", cfg.roi_weight,
                      scores.get("levered_return"), [
                {"label": "Return on cash invested", "value": f.get("roi_pct"), "field": "roi_pct"},
                {"label": "Projected profit", "value": f.get("projected_profit"),
                 "field": "projected_profit"},
                {"label": "Risk-adjusted after-repair value", "value": f.get("arv"), "field": "arv"},
                {"label": "Renovation estimate", "value": f.get("rehab_cost_mid"),
                 "field": "rehab_cost_mid"},
            ]),
        ]
        return make_breakdown(lens=self.STRATEGY_KEY, version=ctx.config.version,
                              result=result, components=components, adjustments=adjustments)

    # ------------------------------------------------------------------

    def _apply_pressure(self, ctx: ScoringContext, base: float, factors: dict) -> float:
        """
        Scale the deal score by how likely the seller is to move.

        Neutral at the configured motivated-seller score; a seller under real
        pressure lifts the score by up to `pressure_weight`, a fresh unmoved
        listing lowers it by the same. The economics still dominate: this is
        a modifier, not a second deal score.
        """
        cfg = ctx.config.fix_flip
        pressure = ctx.prior_score("motivated_seller")
        if pressure is None or cfg.pressure_weight <= 0:
            return base
        # -1 at ms=0, 0 at neutral, +1 at ms=100 (piecewise linear).
        if pressure >= cfg.pressure_neutral_score:
            span = max(100.0 - cfg.pressure_neutral_score, 1.0)
            signal = (pressure - cfg.pressure_neutral_score) / span
        else:
            signal = (pressure - cfg.pressure_neutral_score) / max(cfg.pressure_neutral_score, 1.0)
        modifier = 1.0 + cfg.pressure_weight * signal
        factors["seller_pressure_score"] = round(pressure, 1)
        factors["seller_pressure_modifier"] = round(modifier, 3)
        return base * modifier

    def _risk_adjusted_arv(self, ctx: ScoringContext, factors: dict, flags: list):
        """
        Discount ARV when the comp set disagrees with itself.

        A wide spread of comp $/sqft means the exit price is genuinely
        uncertain, and an uncertain exit is worth less than a certain one of
        the same expected value.
        """
        cfg = ctx.config.fix_flip
        arv = float(ctx.valuation.arv)
        cv = ctx.valuation.comp_dispersion_cv

        if cv is None:
            return arv, 0.0
        if cv <= 0.15:
            return arv, 0.0

        span = max(cfg.dispersion_haircut_at_cv - 0.15, 0.01)
        fraction = min(1.0, (cv - 0.15) / span)
        haircut = fraction * cfg.max_dispersion_haircut
        if cv >= cfg.dispersion_haircut_at_cv:
            flags.append("wide_comp_dispersion_arv_discounted")
        return arv * (1 - haircut), haircut

    def _holding_costs(self, ctx: ScoringContext, cfg) -> float:
        """Taxes, insurance, HOA and CDD for the hold period, plus utilities."""
        valuation = ctx.valuation
        annual = 0.0
        for value in (valuation.annual_taxes, valuation.annual_insurance,
                      valuation.annual_hoa, valuation.annual_cdd):
            if value:
                annual += float(value)
        # Utilities on a vacant renovation: lights, water, and running the AC
        # to keep humidity down, which is not optional in Florida.
        annual += 2_400.0
        return annual * (cfg.hold_months / 12.0)

    def _financing(self, purchase: float, rehab: float, closing: float, cfg):
        """Hard-money cost and the cash the investor actually puts in."""
        if not cfg.use_financing:
            return 0.0, purchase + rehab + closing

        loan = (purchase + rehab) * cfg.loan_to_cost
        points = loan * cfg.origination_points
        interest = loan * cfg.annual_interest_rate * (cfg.hold_months / 12.0)
        cash = (purchase + rehab + closing) - loan
        return points + interest, max(cash, 1.0)

    def _max_allowable_offer(self, arv: float, rehab: float, holding: float, cfg) -> Optional[float]:
        """
        The 70% rule, adjusted for this property's actual holding cost.

        The classic form is `ARV * 0.70 - repairs`, where the 30% absorbs
        profit, selling costs and carry. Holding cost is subtracted explicitly
        because it varies enormously across this corpus — a condo with a
        $700/month HOA carries far more over six months than a house.
        """
        mao = arv * cfg.mao_arv_factor - rehab - holding
        return mao if mao > 0 else None

    def _insurability_penalty(self, ctx: ScoringContext, factors: dict, flags: list, risks: list) -> float:
        """Florida exit-liquidity drag from insurability."""
        cfg = ctx.config.fix_flip
        risk_factors = []

        if ctx.amenity("construction_class") == "frame":
            risk_factors.append("wood_frame_construction")
        if ctx.amenity("roof_class") == "flat":
            risk_factors.append("flat_roof")
        if (ctx.property.year_built or 9999) < 2002 and ctx.amenity("has_impact_glazing") is False:
            risk_factors.append("pre_2002_without_impact_glazing")
        if (ctx.property.flood_factor or 0) >= 8:
            risk_factors.append("high_flood_risk")

        if not risk_factors:
            return 0.0

        factors["insurability_risks"] = risk_factors
        flags.append("insurability_may_limit_resale_buyer_pool")
        risks.append("Insurability drag on resale: " + ", ".join(r.replace("_", " ") for r in risk_factors) + ".")
        return min(cfg.uninsurable_risk_penalty * len(risk_factors), 0.30)

    def _exit_liquidity_penalty(self, ctx: ScoringContext, arv: float, factors: dict, flags: list, risks: list) -> float:
        """Would the market actually absorb this exit, at this price, in six months?"""
        cfg = ctx.config.fix_flip
        market = ctx.market or {}
        penalty = 0.0

        p75 = market.get("p75_sold_ppsf")
        sqft = ctx.property.sqft
        if p75 and sqft:
            arv_ppsf = arv / sqft
            ratio = arv_ppsf / float(p75)
            factors["arv_ppsf_vs_market_p75"] = round(ratio, 2)
            if ratio > cfg.exit_ceiling_ratio:
                penalty += cfg.exit_ceiling_penalty
                flags.append("arv_above_market_upper_quartile_exit_uncertain")
                risks.append(
                    f"Exit at ${arv_ppsf:,.0f}/sqft would be {ratio:.2f}x the area's upper-quartile "
                    "sale — the most expensive house on the block is the hardest to sell."
                )

        median_dom = market.get("median_dom")
        if median_dom and float(median_dom) > cfg.slow_market_dom_days:
            fraction = linear_ramp(float(median_dom), cfg.slow_market_dom_days,
                                   cfg.slow_market_dom_days * 2, 0.0, 1.0) or 0.0
            penalty += fraction * cfg.slow_market_max_penalty
            flags.append("slow_market_for_a_six_month_flip")
            risks.append(f"Market median {float(median_dom):.0f} days on market — a six-month flip clock is optimistic here.")

        return penalty

    @staticmethod
    def _era_system_risks(valuation, risks: list) -> None:
        adders = ((valuation.rehab_basis or {}).get("adders") or {})
        for key in ("cast_iron_drain_repipe", "polybutylene_repipe", "electrical_panel_and_wiring"):
            if key in adders and len(risks) < 5:
                risks.append(_ERA_SYSTEM_LABELS[key])

    # ------------------------------------------------------------------

    def _rationale(self, ctx: ScoringContext, f: dict, score: float) -> str:
        parts = []
        mao = f.get("max_allowable_offer")
        delta = f.get("offer_vs_asking_pct")
        if mao:
            if delta is not None and delta >= 0:
                parts.append(
                    f"Asking is at or under the maximum sensible offer of ${mao:,.0f} — "
                    "a deal at list price."
                )
            elif delta is not None and delta > -10:
                parts.append(
                    f"Maximum sensible offer ${mao:,.0f}, {abs(delta):.0f}% below asking — "
                    "a negotiation, not a pass."
                )
            else:
                parts.append(
                    f"Maximum sensible offer ${mao:,.0f}, {abs(delta):.0f}% below asking."
                )
        else:
            parts.append("No offer price clears the 70% rule after repairs and carry.")

        profit = f.get("projected_profit")
        roi = f.get("roi_pct")
        if profit is not None and roi is not None:
            verb = "At asking, projected profit" if profit >= 0 else "At asking, projected LOSS"
            parts.append(
                f"{verb} ${abs(profit):,.0f} on ${f['cash_invested']:,.0f} cash "
                f"({roi}% ROI over {f['hold_months']:.0f} months"
                + (f", {f['annualised_roi_pct']}% annualised" if f.get("annualised_roi_pct") else "")
                + ")."
            )

        parts.append(
            f"ARV ${f['arv']:,.0f} from {f.get('arv_method', 'comps')}, "
            f"renovation ${f['rehab_cost_mid']:,.0f}"
            + (f" ({f['condition_class']} condition)" if f.get("condition_class") else "")
            + f", costs ${f['total_cost'] - f['purchase_price'] - f['rehab_cost_mid']:,.0f} "
              f"(carry, financing and sale)."
        )

        pressure = f.get("seller_pressure_score")
        if pressure is not None:
            if pressure >= 55:
                parts.append(f"Seller pressure is high ({pressure:.0f}/100), which improves the odds of reaching the offer price.")
            elif pressure < 20:
                parts.append(f"Seller shows little pressure ({pressure:.0f}/100) — expect to pay close to asking.")

        risks = f.get("top_risks") or []
        if risks:
            parts.append("Key risks: " + " ".join(risks))

        if f.get("arv_dispersion_haircut_pct"):
            parts.append(
                f"Comps disagree materially (CV {f.get('comp_dispersion_cv')}), so ARV is "
                f"discounted {f['arv_dispersion_haircut_pct']}% for exit uncertainty."
            )

        parts.append("Renovation cost is a heuristic estimate, not a contractor bid.")
        return f"Fix & flip grade {letter_grade(score)} ({round(score)}/100). " + " ".join(parts)
