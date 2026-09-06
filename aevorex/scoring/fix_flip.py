"""
Fix & flip scorer — a full underwrite, not an index.

WHAT CHANGED AND WHY
--------------------
The previous version produced p50=0.0, p90=0.7, p99=100.0: effectively binary,
useless for ranking. Two causes, both fixed here.

1. **The scoring curve.** A step function over six margin breakpoints
   quantised a continuous quantity into six values. 90% of retail listings
   genuinely have negative flip margin — that part was correct — but the top
   decile has to be *rankable*, and it was not. Replaced with a logistic
   centred at 15% ROI, which spends its resolution where the decisions are.

2. **The cost model.** The old version counted purchase price, a flat
   age-based repair estimate and 9% selling costs. It omitted financing
   entirely, omitted holding costs entirely, and used the comp MEDIAN as ARV.
   A flip funded with hard money at 12% plus two points over six months
   carries real cost, and a renovated house exits at the top of its comp
   range, not the middle. Both omissions pushed in the same direction:
   they made mediocre deals look viable and left genuinely good ones
   indistinguishable from them.

WHAT IT NOW PRODUCES
--------------------
Alongside the score: projected profit, ROI on cash invested, annualised ROI,
and a **Maximum Allowable Offer** — the number an investor actually asks for.

FLORIDA SPECIFICS
-----------------
Exit liquidity is adjusted for insurability. A frame-built pre-2002 house with
an ageing roof is harder for a buyer to insure and therefore harder to sell,
which is a real cost to a flipper on a six-month clock even when the
renovation itself goes perfectly.
"""

from typing import List, Optional

from aevorex.scoring.base import BaseScorer, ScoreResult, ScoringContext
from aevorex.scoring.curves import clamp, confidence_adjusted, logistic_score

# Raised by the valuation engine when an auction's listed figure is too far
# below our own valuation to be an asking price.
AUCTION_PRICE_FLAG = "auction_price_is_an_opening_bid_not_a_purchase_price"


class FixAndFlipScorer(BaseScorer):
    STRATEGY_KEY = "fix_flip"

    def score(self, ctx: ScoringContext) -> ScoreResult:
        cfg = ctx.config.fix_flip
        prop = ctx.property
        valuation = ctx.valuation
        flags: List[str] = []
        factors: dict = {}

        if ctx.config.skip_placeholder_prices and prop.price_is_placeholder:
            return self.unscoreable(
                "Not scored — the listed price is an auction deposit or opening bid, not "
                "a purchase price, so any margin computed from it would be fictional.",
                ["price_is_a_placeholder_not_a_market_asking_price"],
            )

        if not prop.price:
            return self.unscoreable(
                "Couldn't score fix & flip — no listing price.",
                ["no_price_cannot_underwrite"],
            )
        if valuation is None or not valuation.arv:
            return self.unscoreable(
                "Couldn't score fix & flip — no after-repair value could be established. "
                "That needs at least three comparable sales or a source valuation, and "
                "neither was available.",
                ["no_arv_cannot_underwrite"],
            )
        if valuation.rehab_cost_mid is None:
            return self.unscoreable(
                "Couldn't score fix & flip — no renovation estimate, which needs the "
                "property's floor area.",
                ["no_rehab_estimate_cannot_underwrite"],
            )

        # An auction's listed figure is an opening bid, and the price actually
        # paid is settled in the room. Underwriting against it manufactured a
        # 968% return on a $60,000 opening bid for a $205,000 Orlando house and
        # put it top of this strategy. The opportunity may well be real — it is
        # surfaced by motivated_seller, which measures the seller's situation
        # rather than pretending to know the purchase price — but a flip
        # underwrite needs a price, and there isn't one yet.
        if AUCTION_PRICE_FLAG in (valuation.data_quality_flags or []):
            return self.unscoreable(
                "Not scored — this is an auction and the listed figure is an opening bid, "
                f"not a purchase price (${prop.price:,.0f} against an estimated "
                f"${valuation.market_value:,.0f} of value). Any return computed from it "
                "would be fictional. See the motivated-seller score for the opportunity "
                "itself.",
                ["auction_price_is_an_opening_bid_not_a_purchase_price"],
            )

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

        # Maximum Allowable Offer: solve the same underwrite backwards for the
        # purchase price that still clears the target return. This is what an
        # investor actually wants — not "is this good" but "what do I bid".
        mao = self._max_allowable_offer(arv, rehab, holding, cfg)

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
            "offer_vs_asking_pct": (
                round((mao - purchase) / purchase * 100, 1) if mao and purchase else None
            ),
            "hold_months": cfg.hold_months,
            "financed": cfg.use_financing,
        })

        # ---- score the return ----
        base = logistic_score(
            roi_pct, cfg.roi_midpoint_pct, cfg.roi_steepness,
            tail_weight=cfg.roi_tail_weight, tail_span=cfg.roi_tail_span,
        )
        if base is None:
            return self.unscoreable(
                "Couldn't score fix & flip — the underwrite produced no return figure.",
                flags + ["underwrite_incomplete"], factors,
            )

        # Insurability drag on exit liquidity.
        penalty = self._insurability_penalty(ctx, factors, flags)
        base *= (1 - penalty)

        confidence = float(valuation.valuation_confidence or 0.3)
        if valuation.rehab_basis and "condition:" not in str(
            (valuation.rehab_basis or {}).get("base_psf_basis", "")
        ):
            # Rehab estimated from age rather than a stated condition — the
            # single biggest source of error in the underwrite.
            confidence *= 0.85
            flags.append("rehab_estimated_from_age_not_stated_condition")

        final = clamp(confidence_adjusted(base, confidence))
        factors["valuation_confidence"] = round(confidence, 3)
        factors["comp_dispersion_cv"] = valuation.comp_dispersion_cv
        if haircut:
            factors["arv_dispersion_haircut_pct"] = round(haircut * 100, 1)

        return ScoreResult(
            score=round(final, 2),
            rationale=self._rationale(ctx, factors, final),
            factors=factors,
            data_quality_flags=flags + list(valuation.data_quality_flags or [])[:4],
            confidence=round(confidence, 3),
        )

    # ------------------------------------------------------------------

    def _risk_adjusted_arv(self, ctx: ScoringContext, factors: dict, flags: list):
        """
        Discount ARV when the comp set disagrees with itself.

        A wide spread of comp $/sqft means the exit price is genuinely
        uncertain, and an uncertain exit is worth less than a certain one of
        the same expected value. Treating dispersion as a confidence
        annotation only — as the previous version did — lets a speculative ARV
        drive a high score with a footnote nobody reads.
        """
        cfg = ctx.config.fix_flip
        arv = float(ctx.valuation.arv)
        cv = ctx.valuation.comp_dispersion_cv

        if cv is None:
            return arv, 0.0
        if cv <= 0.15:
            return arv, 0.0

        # Ramp the haircut in from CV 0.15 up to the configured threshold.
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
        """
        Hard-money cost and the cash the investor actually puts in.

        ROI is computed on cash invested, not on total project cost — leverage
        is the whole point of using hard money, and an all-in denominator
        would understate the return on exactly the deals investors prefer.
        """
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
        here because it varies enormously across this corpus — a condo with a
        $700/month HOA carries far more over six months than a house without
        one, and the flat rule hides that.
        """
        mao = arv * cfg.mao_arv_factor - rehab - holding
        return mao if mao > 0 else None

    def _insurability_penalty(self, ctx: ScoringContext, factors: dict, flags: list) -> float:
        """
        Florida exit-liquidity drag.

        A buyer who cannot get affordable cover cannot get a mortgage, so an
        uninsurable property has a smaller buyer pool and sits longer — a real
        cost on a six-month clock.
        """
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
        # Each factor compounds, but the total is capped so a single property
        # cannot be penalised out of existence on proxies alone.
        return min(cfg.uninsurable_risk_penalty * len(risk_factors), 0.30)

    # ------------------------------------------------------------------

    def _rationale(self, ctx: ScoringContext, f: dict, score: float) -> str:
        parts = []
        profit = f.get("projected_profit")
        roi = f.get("roi_pct")

        if profit is not None and roi is not None:
            verb = "Projected profit" if profit >= 0 else "Projected LOSS"
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

        mao = f.get("max_allowable_offer")
        if mao:
            delta = f.get("offer_vs_asking_pct")
            if delta is not None and delta < 0:
                parts.append(
                    f"Maximum sensible offer ${mao:,.0f} — {abs(delta):.0f}% below the asking price."
                )
            else:
                parts.append(f"Maximum sensible offer ${mao:,.0f}, at or above asking.")

        if f.get("insurability_risks"):
            readable = ", ".join(r.replace("_", " ") for r in f["insurability_risks"])
            parts.append(f"Insurability drag on resale: {readable}.")

        if f.get("arv_dispersion_haircut_pct"):
            parts.append(
                f"Comps disagree materially (CV {f.get('comp_dispersion_cv')}), so ARV is "
                f"discounted {f['arv_dispersion_haircut_pct']}% for exit uncertainty."
            )

        parts.append("Renovation cost is a heuristic estimate, not a contractor bid.")
        return f"Fix & flip score {round(score)}/100. " + " ".join(parts)
