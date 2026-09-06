"""
Buy & hold scorer — long-term rental.

WHAT CHANGED AND WHY
--------------------
The previous version could only score 3,977 of 7,329 properties (54%), because
it depended on the source rent estimate at 56.5% coverage. The valuation layer
now supplies a rent for 92.2% of properties with its method recorded, so
coverage roughly doubles and every score carries how the rent was obtained.

Its expense model was also thin: a flat $1,800 insurance assumption and 1% of
price for maintenance, with nothing for HOA or CDD. In Florida that is not
conservative, it is wrong — a $400k condo with a $700/month HOA and a $400k
house without one are completely different investments, and under a flat model
they scored almost identically. Expenses now come from the valuation layer:
real taxes where published, modelled Florida insurance, actual HOA, and the
CDD bond nobody models.

Finally, cap rate is scored BOTH absolutely and against the local market. A 5%
cap is mediocre in Jacksonville and strong in Miami Beach; only comparing to
the market distinguishes them.
"""

from typing import List, Optional

from aevorex.scoring.base import BaseScorer, ScoreResult, ScoringContext
from aevorex.scoring.curves import (
    clamp,
    confidence_adjusted,
    linear_ramp,
    ratio_to_benchmark,
    weighted_blend,
)
from aevorex.scoring.utils import location_score_avg


class BuyAndHoldScorer(BaseScorer):
    STRATEGY_KEY = "buy_hold"

    def score(self, ctx: ScoringContext) -> ScoreResult:
        cfg = ctx.config.buy_hold
        prop = ctx.property
        valuation = ctx.valuation
        flags: List[str] = []
        factors: dict = {}

        if ctx.config.skip_placeholder_prices and prop.price_is_placeholder:
            return self.unscoreable(
                "Not scored — the listed price is a placeholder, so every yield computed "
                "from it would be meaningless.",
                ["price_is_a_placeholder_not_a_market_asking_price"],
            )

        if valuation is None or valuation.cap_rate is None:
            reason = "no rent estimate" if (valuation and not valuation.rent_estimate_monthly) \
                else "incomplete operating costs"
            extra = []
            if valuation and valuation.data_quality_flags:
                extra = [f for f in valuation.data_quality_flags
                         if "rent" in f or "operating" in f or "multi_family" in f][:3]
            return self.unscoreable(
                f"Couldn't score buy & hold — {reason}, so no net operating income "
                "could be computed.",
                ["no_noi_cannot_score_buy_hold"] + extra,
            )

        cap_rate = float(valuation.cap_rate)
        cap_score = linear_ramp(cap_rate, cfg.cap_rate_floor, cfg.cap_rate_ceiling)

        # Same yield judged against what this market actually pays.
        market_yield = (ctx.market or {}).get("median_gross_yield")
        yield_vs_market = ratio_to_benchmark(
            valuation.gross_yield, market_yield, midpoint=1.0, steepness=0.12
        )
        if yield_vs_market is None:
            flags.append("no_market_yield_baseline")

        appreciation = self._appreciation_score(ctx, factors, flags)
        location = self._location_score(ctx, factors, flags)

        blended, coverage = weighted_blend([
            (cap_score, cfg.cap_rate_weight),
            (yield_vs_market, cfg.yield_vs_market_weight),
            (appreciation, cfg.appreciation_weight),
            (location, cfg.location_weight),
        ])
        if blended is None:
            return self.unscoreable(
                "Couldn't score buy & hold — none of the return or location inputs resolved.",
                flags + ["insufficient_data_for_buy_hold"], factors,
            )

        # A lease already in place removes lease-up cost and void risk.
        if prop.is_tenant_occupied:
            blended += cfg.tenant_in_place_bonus
            factors["tenant_in_place"] = True
        # Association approval can add weeks and can reject an investor buyer.
        if ctx.amenity("hoa_approval_required"):
            blended -= cfg.hoa_approval_penalty
            factors["hoa_approval_required"] = True

        cash_on_cash = self._cash_on_cash(ctx, cfg, factors)

        factors.update({
            "cap_rate_pct": round(cap_rate * 100, 2),
            "gross_yield_pct": round(valuation.gross_yield * 100, 2) if valuation.gross_yield else None,
            "market_median_gross_yield_pct": round(market_yield * 100, 2) if market_yield else None,
            "monthly_rent_estimate": valuation.rent_estimate_monthly,
            "rent_method": valuation.rent_method,
            "noi_annual": valuation.noi_annual,
            "annual_taxes": valuation.annual_taxes,
            "annual_insurance": valuation.annual_insurance,
            "annual_hoa": valuation.annual_hoa,
            "annual_cdd": valuation.annual_cdd,
            "annual_operating_expenses": valuation.annual_operating_expenses,
            "cash_on_cash_pct": cash_on_cash,
            "signal_coverage": coverage,
        })

        # Rent confidence gates the whole score: a cap rate built on a
        # market-yield prior is an assumption dressed as a measurement.
        rent_confidence = float(valuation.rent_confidence or 0.3)
        confidence = coverage * (0.4 + 0.6 * rent_confidence)
        if valuation.rent_method == "market_yield_prior":
            flags.append("rent_inferred_from_market_yield_treat_as_provisional")

        final = clamp(confidence_adjusted(blended, confidence))
        return ScoreResult(
            score=round(final, 2),
            rationale=self._rationale(ctx, factors, final),
            factors=factors,
            data_quality_flags=flags,
            confidence=round(confidence, 3),
        )

    # ------------------------------------------------------------------

    def _appreciation_score(self, ctx, factors: dict, flags: list) -> Optional[float]:
        """Market price trend, from the zip snapshot where one exists."""
        snapshot = ctx.market_snapshot
        if snapshot and snapshot.yoy_price_change_pct is not None:
            yoy = float(snapshot.yoy_price_change_pct)
            factors["market_yoy_price_change_pct"] = round(yoy, 1)
            # -5% to +8% spans the realistic range; beyond that is noise.
            return linear_ramp(yoy, -5.0, 8.0)
        flags.append("no_market_appreciation_data")
        return None

    def _location_score(self, ctx, factors: dict, flags: list) -> Optional[float]:
        """
        Tenant-demand quality, weighted toward schools.

        School quality is the strongest predictor of family-rental demand and
        tenant tenure, and primary_schools_score is also the highest-variance
        location dimension available (sd 3.41 on a 0-10 scale), so it
        discriminates better than anything else here.
        """
        cfg = ctx.config.buy_hold
        location = ctx.location_score
        if location is None:
            flags.append("no_location_scores")
            return None

        schools = location_score_avg(location, ["primary_schools_score", "high_schools_score"])
        amenity = location_score_avg(
            location, ["groceries_score", "restaurants_score", "shopping_score",
                       "pedestrian_score", "parks_score"]
        )
        parts = []
        if schools is not None:
            factors["school_score_0_10"] = round(schools, 2)
            parts.append((schools * 10, cfg.school_weight_within_location))
        if amenity is not None:
            factors["amenity_score_0_10"] = round(amenity, 2)
            parts.append((amenity * 10, 1 - cfg.school_weight_within_location))
        if not parts:
            return None
        blended, _ = weighted_blend(parts)
        return blended

    def _cash_on_cash(self, ctx, cfg, factors: dict) -> Optional[float]:
        """
        Levered return on the cash actually put down.

        Reported alongside the unlevered cap rate rather than replacing it:
        cap rate compares properties, cash-on-cash compares investments.
        """
        valuation = ctx.valuation
        price = ctx.property.price
        if not price or valuation.noi_annual is None:
            return None

        down = price * cfg.down_payment_pct
        loan = price - down
        monthly_rate = cfg.mortgage_rate / 12
        months = cfg.mortgage_years * 12
        if monthly_rate > 0:
            payment = loan * (monthly_rate * (1 + monthly_rate) ** months) / \
                      ((1 + monthly_rate) ** months - 1)
        else:
            payment = loan / months
        annual_debt_service = payment * 12

        cash_flow = float(valuation.noi_annual) - annual_debt_service
        closing = price * 0.02
        invested = down + closing
        factors["annual_debt_service"] = round(annual_debt_service)
        factors["annual_cash_flow_after_debt"] = round(cash_flow)
        return round(cash_flow / invested * 100, 2) if invested > 0 else None

    # ------------------------------------------------------------------

    def _rationale(self, ctx, f: dict, score: float) -> str:
        parts = []
        cap = f.get("cap_rate_pct")
        rent = f.get("monthly_rent_estimate")
        if cap is not None and rent:
            parts.append(
                f"{cap}% cap rate on an estimated ${rent:,.0f}/month rent "
                f"(NOI ${f.get('noi_annual', 0):,.0f})."
            )

        market_yield = f.get("market_median_gross_yield_pct")
        gross = f.get("gross_yield_pct")
        if market_yield and gross:
            verdict = "above" if gross > market_yield else "below"
            parts.append(f"Gross yield {gross}%, {verdict} the local median of {market_yield}%.")

        expenses = []
        if f.get("annual_hoa"):
            expenses.append(f"${f['annual_hoa']:,.0f} HOA")
        if f.get("annual_insurance"):
            expenses.append(f"${f['annual_insurance']:,.0f} insurance (modelled)")
        if f.get("annual_cdd"):
            expenses.append(f"${f['annual_cdd']:,.0f} CDD")
        if expenses:
            parts.append("Annual costs include " + ", ".join(expenses) + ".")

        coc = f.get("cash_on_cash_pct")
        if coc is not None:
            flow = f.get("annual_cash_flow_after_debt", 0)
            parts.append(
                f"Levered cash-on-cash {coc}% "
                f"({'positive' if flow >= 0 else 'NEGATIVE'} cash flow of ${abs(flow):,.0f}/yr "
                f"at 25% down)."
            )

        if f.get("school_score_0_10") is not None:
            parts.append(f"School score {f['school_score_0_10']}/10 for family-rental demand.")
        if f.get("tenant_in_place"):
            parts.append("Tenant already in place — income from day one.")
        if f.get("hoa_approval_required"):
            parts.append("Association approval required, which can delay or block an investor buyer.")

        method = f.get("rent_method")
        if method and method != "source_avm":
            parts.append(f"Rent is {method.replace('_', ' ')}, so treat the yield as indicative.")

        return f"Buy & hold score {round(score)}/100. " + " ".join(parts)
