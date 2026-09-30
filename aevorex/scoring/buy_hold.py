"""
Buy & hold scorer — long-term rental.

WHAT CHANGED AND WHY (v3)
-------------------------
The v2 top decile was directionally right but concentrated in exactly the
stock a Florida investor is fleeing and rewarded missing data:

- 299 of its 941 properties were condos built before 1995. Post-Surfside
  milestone inspections and fully funded reserves are arriving as special
  assessments and falling prices, and the scorer had no factor for it.
- A Naples condo scored 93.7 with no HOA fee on record; operating expenses
  were computed without one, so NOI and cap rate were overstated. Condos
  with no HOA recorded averaged a 15.6% cap against 1.9% with one.
- A Floridays Resort unit ranked in the top ten. A condo-hotel cannot be
  let long-term and cannot be financed conventionally; it is not a
  buy-and-hold at all.
- Nothing measured whether the property paid its own mortgage.

So this version adds a Florida condo-risk factor, caps attached properties
with no HOA on record, gates condo-hotels, and reports debt service
coverage and monthly cash flow after debt, capping negative cash flow. Cap
rate is still scored both absolutely and against the local market, because
a 5% cap is mediocre in Jacksonville and strong in Miami Beach.
"""

from typing import List, Optional

from aevorex.scoring.base import BaseScorer, ScoreResult, ScoringContext, component, make_breakdown
from aevorex.scoring.config import letter_grade
from aevorex.scoring.curves import (
    apply_gate,
    clamp,
    confidence_adjusted,
    linear_ramp,
    ratio_to_benchmark,
    weighted_blend,
)
from aevorex.scoring.utils import (
    condo_risk_text_signals,
    is_attached,
    is_condo,
    location_score_avg,
    looks_like_condo_hotel,
)


class BuyAndHoldScorer(BaseScorer):
    STRATEGY_KEY = "buy_hold"

    def score(self, ctx: ScoringContext) -> ScoreResult:
        cfg = ctx.config.buy_hold
        prop = ctx.property
        valuation = ctx.valuation
        flags: List[str] = []
        factors: dict = {}
        risks: List[str] = []

        if ctx.config.skip_placeholder_prices and prop.price_is_placeholder:
            return self.with_breakdown(ctx, self.unscoreable(
                "Not scored — the listed price is a placeholder, so every yield computed "
                "from it would be meaningless.",
                ["price_is_a_placeholder_not_a_market_asking_price"],
            ))

        if valuation is None or valuation.cap_rate is None:
            reason = "no rent estimate" if (valuation and not valuation.rent_estimate_monthly) \
                else "incomplete operating costs"
            extra = []
            if valuation and valuation.data_quality_flags:
                extra = [f for f in valuation.data_quality_flags
                         if "rent" in f or "operating" in f or "multi_family" in f][:3]
            return self.with_breakdown(ctx, self.unscoreable(
                f"Couldn't score buy & hold — {reason}, so no net operating income "
                "could be computed.",
                ["no_noi_cannot_score_buy_hold"] + extra,
            ))

        cap_rate = float(valuation.cap_rate)
        # Score on the all-in basis: NOI over price plus the renovation
        # needed before a tenant can move in. The headline cap on price alone
        # is still reported.
        cap_rate_all_in = cap_rate
        basis = ctx.all_in_basis()
        if cfg.use_all_in_basis and basis and prop.price:
            # cap_rate is NOI / price, so NOI / (price + rehab) is the same
            # number scaled by price / basis.
            cap_rate_all_in = cap_rate * float(prop.price) / basis
            rehab_share = (basis - prop.price) / prop.price
            if rehab_share >= 0.25:
                flags.append("renovation_required_before_letting")
                risks.append(
                    f"Needs about ${basis - prop.price:,.0f} of renovation before it can be let "
                    f"({rehab_share * 100:.0f}% of price) — yield is measured on the all-in cost."
                )
        cap_score = linear_ramp(cap_rate_all_in, cfg.cap_rate_floor, cfg.cap_rate_ceiling)

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
            return self.with_breakdown(ctx, self.unscoreable(
                "Couldn't score buy & hold — none of the return or location inputs resolved.",
                flags + ["insufficient_data_for_buy_hold"], factors,
            ))

        # A lease already in place removes lease-up cost and void risk.
        if prop.is_tenant_occupied:
            blended += cfg.tenant_in_place_bonus
            factors["tenant_in_place"] = True
        # Association approval can add weeks and can reject an investor buyer.
        if ctx.amenity("hoa_approval_required"):
            blended -= cfg.hoa_approval_penalty
            factors["hoa_approval_required"] = True

        # ---- debt service ----
        cash_on_cash, dscr, monthly_cash_flow = self._debt_service(ctx, cfg, factors)

        # ---- Florida condo risk ----
        condo_penalty, condo_cap = self._condo_risk(ctx, cfg, factors, flags, risks)
        blended *= (1 - condo_penalty)

        # ---- gates: things that make the property not a buy-and-hold ----
        cap = condo_cap
        if is_attached(prop.property_type) and not valuation.annual_hoa:
            cap = min(cap or 100.0, cfg.missing_hoa_cap)
            flags.append("attached_property_missing_hoa_score_capped")
            risks.insert(0, "No HOA fee on record for an attached unit — NOI is overstated until the fee is known.")
        if monthly_cash_flow is not None and monthly_cash_flow < 0:
            cap = min(cap or 100.0, cfg.negative_cash_flow_cap)
            flags.append("negative_levered_cash_flow_score_capped")
            risks.append(f"Negative cash flow of ${abs(monthly_cash_flow):,.0f}/month after debt at 25% down.")
        elif dscr is not None and dscr < cfg.min_dscr:
            cap = min(cap or 100.0, cfg.negative_cash_flow_cap)
            flags.append("dscr_below_one_score_capped")
        blended = apply_gate(blended, cap)

        factors.update({
            "cap_rate_pct": round(cap_rate * 100, 2),
            "cap_rate_all_in_pct": round(cap_rate_all_in * 100, 2),
            "all_in_basis": round(basis) if basis else None,
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
            "dscr": dscr,
            "monthly_cash_flow_after_debt": monthly_cash_flow,
            "signal_coverage": coverage,
        })

        # Rent confidence gates the whole score: a cap rate built on a
        # market-yield prior is an assumption dressed as a measurement.
        rent_confidence = float(valuation.rent_confidence or 0.3)
        confidence = coverage * (0.4 + 0.6 * rent_confidence)
        if valuation.rent_method == "market_yield_prior":
            flags.append("rent_inferred_from_market_yield_treat_as_provisional")
        elif valuation.rent_method == "zip_bed_model":
            risks.append("Rent is modelled from the zip's rental listings, not from a comparable — verify before underwriting.")

        # A listing that has sat far longer than its market usually has a
        # reason the data cannot see: financing problems in the building,
        # an assessment pending, a failed inspection.
        median_dom = (ctx.market or {}).get("median_dom")
        if prop.days_on_market and median_dom and float(median_dom) > 0 \
                and prop.days_on_market > max(120, 4 * float(median_dom)):
            flags.append("stale_listing_for_its_market")
            risks.append(
                f"On market {prop.days_on_market} days against a local median of "
                f"{float(median_dom):.0f} — find out why before anything else."
            )

        final = clamp(confidence_adjusted(blended, confidence))
        factors["top_risks"] = risks[:3]
        result = ScoreResult(
            score=round(final, 2),
            rationale=self._rationale(ctx, factors, final),
            factors=factors,
            data_quality_flags=flags,
            confidence=round(confidence, 3),
            grade=letter_grade(final),
            top_risks=risks[:3],
            component_scores={
                "cap_rate": cap_score, "yield_vs_market": yield_vs_market,
                "appreciation": appreciation, "location": location,
            },
            score_adjustments=[
                {"type": "bonus", "label": "Tenant already in place",
                 "value": cfg.tenant_in_place_bonus, "applied": bool(prop.is_tenant_occupied)},
                {"type": "penalty", "label": "Association approval required",
                 "value": cfg.hoa_approval_penalty,
                 "applied": bool(ctx.amenity("hoa_approval_required"))},
                {"type": "multiplier", "label": "Florida condo risk",
                 "value": 1 - condo_penalty, "applied": condo_penalty > 0},
                {"type": "gate", "label": "Condo-hotel or resort unit",
                 "value": cfg.condo_hotel_cap, "applied": condo_cap is not None},
                {"type": "gate", "label": "Attached unit missing HOA fee",
                 "value": cfg.missing_hoa_cap,
                 "applied": is_attached(prop.property_type) and not valuation.annual_hoa},
                {"type": "gate", "label": "Negative cash flow or DSCR below one",
                 "value": cfg.negative_cash_flow_cap,
                 "applied": (monthly_cash_flow is not None and monthly_cash_flow < 0) or
                 (monthly_cash_flow is not None and monthly_cash_flow >= 0 and dscr is not None
                  and dscr < cfg.min_dscr)},
                {"type": "cap", "label": "Effective financeability cap",
                 "value": cap or 100.0, "applied": cap is not None},
                {"type": "confidence_shrink", "label": "Rent and evidence confidence",
                 "value": confidence, "floor": 0.55, "applied": True},
            ],
        )
        return self.with_breakdown(ctx, result)

    def breakdown(self, ctx: ScoringContext, result: ScoreResult) -> dict:
        cfg = ctx.config.buy_hold
        f, scores = result.factors, result.component_scores
        adjustments = list(result.score_adjustments) or [
            {"type": "bonus", "label": "Tenant already in place",
             "value": cfg.tenant_in_place_bonus, "applied": False},
            {"type": "penalty", "label": "Association approval required",
             "value": cfg.hoa_approval_penalty, "applied": False},
            {"type": "multiplier", "label": "Florida condo risk", "value": 1.0,
             "applied": False},
            {"type": "gate", "label": "Condo-hotel or resort unit",
             "value": cfg.condo_hotel_cap, "applied": False},
            {"type": "gate", "label": "Attached unit missing HOA fee",
             "value": cfg.missing_hoa_cap, "applied": False},
            {"type": "gate", "label": "Negative cash flow or DSCR below one",
             "value": cfg.negative_cash_flow_cap, "applied": False},
            {"type": "cap", "label": "Effective financeability cap", "value": 100.0,
             "applied": False},
            {"type": "confidence_shrink", "label": "Rent and evidence confidence",
             "value": 0.0, "floor": 0.55, "applied": False},
        ]
        components = [
            component("cap_rate", "Net operating income yield", cfg.cap_rate_weight,
                      scores.get("cap_rate"), [
                {"label": "All-in capitalization rate", "value": f.get("cap_rate_all_in_pct"),
                 "field": "cap_rate_all_in_pct"},
                {"label": "Annual net operating income", "value": f.get("noi_annual"),
                 "field": "noi_annual"},
            ]),
            component("yield_vs_market", "Yield versus local market", cfg.yield_vs_market_weight,
                      scores.get("yield_vs_market"), [
                {"label": "Gross yield", "value": f.get("gross_yield_pct"), "field": "gross_yield"},
                {"label": "Local median gross yield", "value": f.get("market_median_gross_yield_pct"),
                 "field": "median_gross_yield"},
            ]),
            component("appreciation", "Market appreciation", cfg.appreciation_weight,
                      scores.get("appreciation"), [
                {"label": "Year-over-year price change", "value": f.get("market_yoy_price_change_pct"),
                 "field": "yoy_price_change_pct"},
            ]),
            component("location", "Long-term tenant location", cfg.location_weight,
                      scores.get("location"), [
                {"label": "School quality", "value": f.get("school_score_0_10"),
                 "field": "primary_schools_score"},
                {"label": "Everyday amenities", "value": f.get("amenity_score_0_10"),
                 "field": "location_scores"},
            ]),
        ]
        return make_breakdown(lens=self.STRATEGY_KEY, version=ctx.config.version,
                              result=result, components=components, adjustments=adjustments)

    # ------------------------------------------------------------------

    def _appreciation_score(self, ctx, factors: dict, flags: list) -> Optional[float]:
        """Market price trend, from the zip snapshot where one exists."""
        snapshot = ctx.market_snapshot
        if snapshot and snapshot.yoy_price_change_pct is not None:
            yoy = float(snapshot.yoy_price_change_pct)
            factors["market_yoy_price_change_pct"] = round(yoy, 1)
            return linear_ramp(yoy, -5.0, 8.0)
        flags.append("no_market_appreciation_data")
        return None

    def _location_score(self, ctx, factors: dict, flags: list) -> Optional[float]:
        """Tenant-demand quality, weighted toward schools."""
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

    def _debt_service(self, ctx, cfg, factors: dict):
        """
        Levered return, coverage ratio and monthly cash flow after debt.

        Cap rate compares properties; these compare investments. DSCR is what
        the lender underwrites, monthly cash flow is what the owner feels.
        """
        valuation = ctx.valuation
        price = ctx.property.price
        if not price or valuation.noi_annual is None:
            return None, None, None

        rehab = float(valuation.rehab_cost_mid or 0.0) if cfg.use_all_in_basis else 0.0
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

        noi = float(valuation.noi_annual)
        cash_flow = noi - annual_debt_service
        # Renovation is paid in cash on top of the down payment.
        invested = down + price * cfg.closing_cost_pct + rehab
        factors["annual_debt_service"] = round(annual_debt_service)
        factors["annual_cash_flow_after_debt"] = round(cash_flow)
        cash_on_cash = round(cash_flow / invested * 100, 2) if invested > 0 else None
        dscr = round(noi / annual_debt_service, 2) if annual_debt_service > 0 else None
        return cash_on_cash, dscr, round(cash_flow / 12)

    def _condo_risk(self, ctx, cfg, factors: dict, flags: list, risks: list):
        """
        Association-health and financeability risk for attached units.

        Returns (penalty_fraction, cap). Points accumulate into a penalty;
        a condo-hotel caps outright because it is a different asset class.
        """
        prop = ctx.property
        if not is_attached(prop.property_type):
            return 0.0, None

        text_signals = condo_risk_text_signals(prop.description, prop.ai_summary)
        points = 0.0
        reasons: List[str] = []

        if looks_like_condo_hotel(prop.address, prop.description, prop.ai_summary):
            flags.append("condo_hotel_or_resort_unit_not_a_long_term_rental")
            risks.insert(0, "Condo-hotel / resort unit: long-term letting is usually prohibited and conventional financing unavailable.")
            factors["condo_hotel_suspected"] = True
            return 0.0, cfg.condo_hotel_cap

        if is_condo(prop.property_type) and prop.year_built and prop.year_built < cfg.condo_risk_year_built_before:
            points += cfg.condo_risk_age_points
            reasons.append(f"built {prop.year_built}: milestone inspection and reserve funding apply")

        market_hoa = (ctx.market or {}).get("median_hoa_monthly")
        hoa_annual = ctx.valuation.annual_hoa
        if hoa_annual and market_hoa and float(market_hoa) > 0:
            ratio = (float(hoa_annual) / 12.0) / float(market_hoa)
            factors["hoa_vs_market_median"] = round(ratio, 2)
            if ratio >= cfg.condo_hoa_ratio_high:
                points += cfg.condo_hoa_high_points
                reasons.append(f"HOA {ratio:.1f}x the local median — deferred maintenance may already be billed")

        if text_signals["special_assessment"]:
            points += cfg.condo_special_assessment_points
            reasons.append("special assessment mentioned in remarks")
        if text_signals["reserve_or_milestone"]:
            points += cfg.condo_special_assessment_points * 0.5
            reasons.append("milestone / reserve language in remarks")
        if prop.is_cash_only:
            points += cfg.condo_cash_only_points
            reasons.append("cash only — the building likely cannot be financed")

        if points <= 0:
            return 0.0, None
        penalty = min(cfg.condo_risk_max_penalty, cfg.condo_risk_max_penalty * points / 100.0)
        factors["condo_risk_points"] = round(points)
        factors["condo_risk_reasons"] = reasons
        flags.append("florida_condo_risk_penalty_applied")
        risks.append("Condo risk: " + "; ".join(reasons) + ".")
        return penalty, None

    # ------------------------------------------------------------------

    def _rationale(self, ctx, f: dict, score: float) -> str:
        parts = []
        cap = f.get("cap_rate_pct")
        cap_all_in = f.get("cap_rate_all_in_pct")
        rent = f.get("monthly_rent_estimate")
        if cap is not None and rent:
            sentence = (f"{cap}% cap rate on an estimated ${rent:,.0f}/month rent "
                        f"(NOI ${f.get('noi_annual', 0):,.0f})")
            if cap_all_in is not None and abs(cap_all_in - cap) >= 0.3:
                sentence += f", {cap_all_in}% on the all-in cost including renovation"
            parts.append(sentence + ".")

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

        flow = f.get("monthly_cash_flow_after_debt")
        dscr = f.get("dscr")
        coc = f.get("cash_on_cash_pct")
        if flow is not None:
            parts.append(
                f"At 25% down: {'positive' if flow >= 0 else 'NEGATIVE'} cash flow of "
                f"${abs(flow):,.0f}/month, DSCR {dscr}, cash-on-cash {coc}%."
            )

        if f.get("school_score_0_10") is not None:
            parts.append(f"School score {f['school_score_0_10']}/10 for family-rental demand.")
        if f.get("tenant_in_place"):
            parts.append("Tenant already in place — income from day one.")
        if f.get("hoa_approval_required"):
            parts.append("Association approval required, which can delay or block an investor buyer.")

        risks = f.get("top_risks") or []
        if risks:
            parts.append("Key risks: " + " ".join(risks))

        method = f.get("rent_method")
        if method and method != "source_avm":
            parts.append(f"Rent is {method.replace('_', ' ')}, so treat the yield as indicative.")

        return f"Buy & hold grade {letter_grade(score)} ({round(score)}/100). " + " ".join(parts)
