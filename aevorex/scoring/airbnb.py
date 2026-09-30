"""
Airbnb scorer — nightly vacation letting, under 30 days.

THE TWO THINGS THAT MAKE THIS DIFFERENT
---------------------------------------

**1. Regulation is a gate, not a factor.**

Sub-30-day letting IS regulated in Florida — by the state, through the DBPR
vacation-rental licence, and decisively by municipalities and by condo
associations, which can forbid what a city allows. Nothing in this dataset
resolves the municipal question, so the curated `municipal_rules` table ships
EMPTY and every property is capped at `unverified_cap` until it is populated.
Hardcoding legal conclusions about named cities would be both unreliable and
a liability, so it is not done.

What the dataset CAN say is the property type. Condo buildings overwhelmingly
prohibit or restrict nightly letting in their own documents, so a condo is
capped below a house unless the listing positively advertises short-term
letting (v3).

**2. Revenue is a proxy and is labelled as one.**

There is no ADR, occupancy or booking data anywhere in this schema. Nightly
revenue is built from bedroom capacity, location tier and amenities, and
ships with `airbnb_revenue_is_a_proxy_not_a_forecast` on every property.

v3 halved the optimism: the nightly-rate multiple drops from 2.6x to 2.0x
the daily long-term rent and peak occupancy from 75% to 65%. Under the old
numbers a modelled $14,000/month Brickell 3-bed produced a $1,197 nightly
rate and a 24% net yield — roughly double what those units gross.

WHAT ACTUALLY DRIVES BOOKINGS
-----------------------------
A private pool (not a shared association pool), waterfront, sleeping
capacity, and walkable proximity to restaurants and nightlife — plus, new in
v3, distance to a beach or a theme park from the points-of-interest table,
which the walkability dimensions cannot see.
"""

from typing import List, Optional

from aevorex.scoring.base import BaseScorer, ScoreResult, ScoringContext, component, make_breakdown
from aevorex.scoring.config import letter_grade
from aevorex.scoring.curves import (
    apply_gate,
    clamp,
    confidence_adjusted,
    linear_ramp,
    weighted_blend,
)
from aevorex.scoring.utils import is_condo, nearest_beach_miles, nearest_theme_park_miles

UNVERIFIED_FLAG = "regulatory_status_unverified_check_local_ordinance"
PROXY_FLAG = "airbnb_revenue_is_a_proxy_not_a_forecast"


class AirbnbScorer(BaseScorer):
    STRATEGY_KEY = "airbnb"

    def score(self, ctx: ScoringContext) -> ScoreResult:
        cfg = ctx.config.airbnb
        prop = ctx.property
        valuation = ctx.valuation
        flags: List[str] = [PROXY_FLAG]
        factors: dict = {}
        risks: List[str] = ["Nightly revenue is a proxy from rent, location and amenities — there is no rate or occupancy data behind it."]

        if ctx.config.skip_placeholder_prices and prop.price_is_placeholder:
            return self.with_breakdown(ctx, self.unscoreable(
                "Not scored — the listed price is a placeholder, not a purchase price.",
                ["price_is_a_placeholder_not_a_market_asking_price"],
            ))

        if valuation is None or not valuation.rent_estimate_monthly:
            return self.with_breakdown(ctx, self.unscoreable(
                "Couldn't score nightly letting — no rent baseline to build a nightly "
                "rate proxy from.",
                ["no_rent_estimate_cannot_score_airbnb"] + flags,
            ))

        location, location_tier = self._location_score(ctx, factors, flags)
        revenue = self._revenue_score(ctx, location_tier, factors, flags)
        fit = self._property_fit_score(ctx, factors, flags)

        blended, coverage = weighted_blend([
            (location, cfg.location_weight),
            (revenue, cfg.revenue_weight),
            (fit, cfg.property_fit_weight),
        ])
        if blended is None:
            return self.with_breakdown(ctx, self.unscoreable(
                "Couldn't score nightly letting — no location, revenue or property inputs "
                "resolved.",
                flags + ["insufficient_data_for_airbnb"], factors,
            ))

        # ---- regulatory gate, applied last so nothing can score around it ----
        cap, verdict, gate_flags = self._regulatory_gate(ctx, factors, risks)
        flags.extend(gate_flags)
        gated = apply_gate(blended, cap)
        factors["regulatory_verdict"] = verdict
        factors["regulatory_score_cap"] = cap

        confidence = coverage * (0.4 + 0.6 * float(valuation.rent_confidence or 0.3))
        if verdict == "unverified":
            confidence *= 0.7

        final = clamp(confidence_adjusted(gated, confidence))
        factors["signal_coverage"] = coverage
        factors["top_risks"] = risks[:3]

        result = ScoreResult(
            score=round(final, 2),
            rationale=self._rationale(ctx, factors, final),
            factors=factors,
            data_quality_flags=flags,
            confidence=round(confidence, 3),
            grade=letter_grade(final),
            top_risks=risks[:3],
            component_scores={"location": location, "revenue": revenue, "property_fit": fit},
            score_adjustments=[
                {"type": "gate", "label": "Age-restricted community",
                 "value": cfg.age_restricted_cap,
                 "applied": verdict == "prohibited_age_restricted"},
                {"type": "gate", "label": "Association or lease restriction",
                 "value": cfg.hoa_restriction_cap,
                 "applied": verdict in {"restricted_by_hoa", "restricted_minimum_lease"}},
                {"type": "gate", "label": "Municipality prohibits nightly letting",
                 "value": cfg.prohibited_cap,
                 "applied": verdict == "prohibited_by_municipality"},
                {"type": "gate", "label": "Municipality restricts nightly letting",
                 "value": cfg.restricted_cap,
                 "applied": verdict == "restricted_by_municipality"},
                {"type": "gate", "label": "Condo documents unverified",
                 "value": cfg.condo_default_cap,
                 "applied": is_condo(prop.property_type) and not prop.allows_short_term_rental},
                {"type": "gate", "label": "Local rules unverified",
                 "value": cfg.unverified_cap, "applied": verdict == "unverified"},
                {"type": "cap", "label": "Effective regulatory cap",
                 "value": cap or 100.0, "applied": cap is not None},
                {"type": "confidence_shrink", "label": "Rent, evidence and regulatory confidence",
                 "value": confidence, "floor": 0.55, "applied": True},
            ],
        )
        return self.with_breakdown(ctx, result)

    def breakdown(self, ctx: ScoringContext, result: ScoreResult) -> dict:
        cfg = ctx.config.airbnb
        f, scores = result.factors, result.component_scores
        fit = f.get("property_fit_factors") or []
        top = list(result.score_adjustments) or [
            {"type": "gate", "label": "Age-restricted community",
             "value": cfg.age_restricted_cap, "applied": False},
            {"type": "gate", "label": "Association or lease restriction",
             "value": cfg.hoa_restriction_cap, "applied": False},
            {"type": "gate", "label": "Municipality prohibits nightly letting",
             "value": cfg.prohibited_cap, "applied": False},
            {"type": "gate", "label": "Municipality restricts nightly letting",
             "value": cfg.restricted_cap, "applied": False},
            {"type": "gate", "label": "Condo documents unverified",
             "value": cfg.condo_default_cap, "applied": False},
            {"type": "gate", "label": "Local rules unverified",
             "value": cfg.unverified_cap, "applied": False},
            {"type": "cap", "label": "Effective regulatory cap", "value": 100.0,
             "applied": False},
            {"type": "confidence_shrink", "label": "Rent, evidence and regulatory confidence",
             "value": 0.0, "floor": 0.55, "applied": False},
        ]
        adjustments = [
            {"type": "component_bonus", "label": "Beach proximity",
             "value": cfg.beach_bonus,
             "applied": (f.get("nearest_beach_miles") is not None and
                         f["nearest_beach_miles"] <= cfg.beach_radius_miles),
             "component": "location"},
            {"type": "component_bonus", "label": "Theme-park proximity",
             "value": cfg.theme_park_bonus,
             "applied": (f.get("nearest_theme_park_miles") is not None and
                         f["nearest_theme_park_miles"] <= cfg.theme_park_radius_miles),
             "component": "location"},
            {"type": "component_bonus", "label": "Private pool",
             "value": cfg.private_pool_bonus, "applied": "private_pool" in fit,
             "component": "property_fit"},
            {"type": "component_bonus", "label": "Waterfront",
             "value": cfg.waterfront_bonus, "applied": "waterfront" in fit,
             "component": "property_fit"},
            {"type": "component_bonus", "label": "Water view",
             "value": cfg.water_view_bonus, "applied": "water_view" in fit,
             "component": "property_fit"},
            {"type": "component_bonus", "label": "Furnished", "value": cfg.furnished_bonus,
             "applied": any(item.startswith("furnished:") for item in fit),
             "component": "property_fit", "reason": "Scaled by furnishing rank"},
            *top,
        ]
        components = [
            component("location", "Leisure-traveller location", cfg.location_weight,
                      scores.get("location"), [
                {"label": "Leisure dimensions", "value": f.get("location_dimensions_0_10", {}),
                 "field": "location_scores"},
                {"label": "Nearest beach miles", "value": f.get("nearest_beach_miles"),
                 "field": "points_of_interest.distance_miles"},
                {"label": "Nearest theme park miles", "value": f.get("nearest_theme_park_miles"),
                 "field": "points_of_interest.distance_miles"},
            ]),
            component("revenue", "Nightly revenue proxy", cfg.revenue_weight,
                      scores.get("revenue"), [
                {"label": "Proxy nightly rate", "value": f.get("proxy_nightly_rate"),
                 "field": "rent_estimate_monthly"},
                {"label": "Proxy occupancy", "value": f.get("proxy_occupancy"),
                 "field": "proxy_occupancy"},
                {"label": "Proxy net yield", "value": f.get("proxy_net_yield_pct"),
                 "field": "proxy_net_yield_pct"},
            ]),
            component("property_fit", "Nightly-rental property fit", cfg.property_fit_weight,
                      scores.get("property_fit"), [
                {"label": "Bedrooms", "value": f.get("bedrooms"), "field": "bedrooms"},
                {"label": "Property fit signals", "value": fit, "field": "property_features"},
            ]),
        ]
        return make_breakdown(lens=self.STRATEGY_KEY, version=ctx.config.version,
                              result=result, components=components, adjustments=adjustments)

    # ------------------------------------------------------------------

    def _regulatory_gate(self, ctx, factors: dict, risks: list):
        """
        Resolve whether nightly letting is permitted, and cap accordingly.

        Order matters: an outright disqualifier beats a municipal permission,
        because an HOA can forbid what a city allows.
        """
        cfg = ctx.config.airbnb
        prop = ctx.property
        flags: List[str] = []

        if prop.is_age_restricted:
            flags.append("age_restricted_community_prohibits_nightly_letting")
            risks.insert(0, "55+ community: nightly letting is not permitted.")
            return cfg.age_restricted_cap, "prohibited_age_restricted", flags

        if prop.is_rental_restricted or ctx.amenity("lease_restricted"):
            flags.append("listing_states_rental_restrictions")
            risks.insert(0, "Listing states rental restrictions.")
            return cfg.hoa_restriction_cap, "restricted_by_hoa", flags

        min_lease = ctx.amenity("min_lease_months")
        if min_lease and min_lease >= 1:
            flags.append("minimum_lease_term_precludes_nightly_letting")
            risks.insert(0, f"Minimum lease of {min_lease} month(s) precludes nightly letting.")
            return cfg.hoa_restriction_cap, "restricted_minimum_lease", flags

        city_key = f"{(prop.city or '').lower()},{prop.state or ''}"
        rule = cfg.municipal_rules.get(city_key)
        if rule == "prohibited":
            flags.append("municipality_prohibits_short_term_rental")
            return cfg.prohibited_cap, "prohibited_by_municipality", flags
        if rule == "restricted":
            flags.append("municipality_restricts_short_term_rental")
            return cfg.restricted_cap, "restricted_by_municipality", flags

        # Condo documents, not city ordinances, are what usually stop a
        # nightly let — and a listing that allows it says so.
        if is_condo(prop.property_type) and not prop.allows_short_term_rental:
            flags.append("condo_building_rules_usually_prohibit_nightly_letting")
            risks.insert(0, "Condo: most associations prohibit or restrict nightly letting — confirm the building's documents.")
            cap = cfg.condo_default_cap
        else:
            cap = None

        if rule == "permitted":
            if prop.allows_short_term_rental:
                return cap, "permitted", flags
            return cap, "permitted_by_municipality", flags

        flags.append(UNVERIFIED_FLAG)
        risks.append("Short-term-rental legality is unverified — check the local ordinance and HOA rules.")
        if prop.allows_short_term_rental:
            factors["listing_advertises_str"] = True
            flags.append("listing_advertises_short_term_rental_still_verify_locally")
        cap = cfg.unverified_cap if cap is None else min(cap, cfg.unverified_cap)
        return cap, "unverified", flags

    def _location_score(self, ctx, factors: dict, flags: list):
        """Leisure-traveller desirability. Returns (score, tier 0-1)."""
        cfg = ctx.config.airbnb
        location = ctx.location_score
        bonus = 0.0

        beach = nearest_beach_miles(ctx.pois)
        park = nearest_theme_park_miles(ctx.pois)
        if beach is not None:
            factors["nearest_beach_miles"] = round(beach, 2)
            if beach <= cfg.beach_radius_miles:
                bonus += cfg.beach_bonus
        if park is not None:
            factors["nearest_theme_park_miles"] = round(park, 2)
            if park <= cfg.theme_park_radius_miles:
                bonus += cfg.theme_park_bonus
        if ctx.amenity("is_waterfront"):
            bonus += 10
            factors["waterfront"] = True
        elif ctx.amenity("has_water_view"):
            bonus += 5

        if location is None:
            flags.append("no_location_scores")
            if bonus:
                score = clamp(40.0 + bonus)
                return score, score / 100.0
            return None, 0.5

        components = []
        detail = {}
        for dimension, weight in cfg.location_dimensions.items():
            value = getattr(location, dimension, None)
            if value is not None:
                detail[dimension] = round(float(value), 1)
                components.append((float(value) * 10, weight))
        if not components:
            return None, 0.5

        blended, _ = weighted_blend(components)
        factors["location_dimensions_0_10"] = detail
        score = clamp(blended + bonus)
        return score, score / 100.0

    def _revenue_score(self, ctx, location_tier: float, factors: dict, flags: list) -> Optional[float]:
        """Proxy nightly economics. Explicitly not a forecast."""
        cfg = ctx.config.airbnb
        valuation = ctx.valuation
        price = ctx.property.price
        if not price:
            return None

        daily_ltr = float(valuation.rent_estimate_monthly) * 12 / 365
        adr = daily_ltr * cfg.adr_multiple_of_daily_ltr

        occupancy = (cfg.weak_location_occupancy
                     + (cfg.peak_location_occupancy - cfg.weak_location_occupancy) * location_tier)

        gross = adr * 365 * occupancy
        management = gross * cfg.management_pct
        operating = float(valuation.annual_operating_expenses or 0)
        furnishing_annual = (ctx.property.sqft or 1200) * cfg.furnishing_cost_per_sqft / 5.0

        net = gross - management - operating - furnishing_annual
        basis = ctx.all_in_basis() or price
        net_yield = net / basis

        factors.update({
            "proxy_nightly_rate": round(adr),
            "proxy_occupancy": round(occupancy, 2),
            "proxy_gross_annual_revenue": round(gross),
            "management_and_cleaning": round(management),
            "furnishing_cost": round(furnishing_annual * 5),
            "proxy_net_annual_income": round(net),
            "all_in_basis": round(basis),
            "proxy_net_yield_pct": round(net_yield * 100, 2),
        })
        return linear_ramp(net_yield, 0.02, 0.10)

    def _property_fit_score(self, ctx, factors: dict, flags: list) -> Optional[float]:
        """Physical attributes that drive nightly bookings."""
        cfg = ctx.config.airbnb
        beds = ctx.property.bedrooms
        if beds is None:
            flags.append("no_bedroom_count")
            base = 50.0
        else:
            base = cfg.bedroom_scores.get(min(int(beds), 5), 60.0)

        applied = []
        if ctx.amenity("has_private_pool"):
            base += cfg.private_pool_bonus
            applied.append("private_pool")
        elif ctx.amenity("has_community_pool"):
            base += cfg.private_pool_bonus * 0.3
            applied.append("community_pool_only")

        if ctx.amenity("is_waterfront"):
            base += cfg.waterfront_bonus
            applied.append("waterfront")
        elif ctx.amenity("has_water_view"):
            base += cfg.water_view_bonus
            applied.append("water_view")

        furnished_rank = ctx.amenity("furnished_rank")
        if furnished_rank:
            base += cfg.furnished_bonus * furnished_rank
            applied.append(f"furnished:{ctx.amenity('furnished_level')}")

        if ctx.amenity("has_spa"):
            base += 4.0
            applied.append("spa")

        factors["property_fit_factors"] = applied
        factors["bedrooms"] = beds
        return clamp(base)

    # ------------------------------------------------------------------

    def _rationale(self, ctx, f: dict, score: float) -> str:
        parts = []
        verdict = f.get("regulatory_verdict")

        if verdict == "prohibited_age_restricted":
            parts.append("55+ age-restricted community — nightly letting is not permitted, so the score is capped.")
        elif verdict in ("restricted_by_hoa", "restricted_minimum_lease"):
            parts.append("The listing states rental restrictions or a minimum lease term, which rules out or severely limits nightly letting.")
        elif verdict == "prohibited_by_municipality":
            parts.append("This municipality prohibits short-term rentals.")
        elif verdict == "unverified":
            parts.append(
                "Short-term-rental legality is UNVERIFIED — Florida regulates sub-30-day "
                "letting at both state and municipal level and this dataset cannot resolve "
                "it. Confirm the local ordinance and HOA rules before relying on this score."
            )

        if f.get("proxy_net_yield_pct") is not None:
            parts.append(
                f"Proxy economics: ${f['proxy_nightly_rate']:,.0f}/night at "
                f"{f['proxy_occupancy'] * 100:.0f}% occupancy gives "
                f"${f['proxy_gross_annual_revenue']:,.0f} gross, netting "
                f"${f['proxy_net_annual_income']:,.0f} ({f['proxy_net_yield_pct']}% yield) "
                f"after {ctx.config.airbnb.management_pct * 100:.0f}% management and costs."
            )

        dims = f.get("location_dimensions_0_10") or {}
        if dims:
            parts.append(
                f"Location: restaurants {dims.get('restaurants_score', '?')}/10, "
                f"nightlife {dims.get('nightlife_score', '?')}/10, "
                f"vibrancy {dims.get('vibrant_score', '?')}/10."
            )
        if f.get("nearest_beach_miles") is not None and f["nearest_beach_miles"] <= ctx.config.airbnb.beach_radius_miles:
            parts.append(f"Beach {f['nearest_beach_miles']} miles away.")
        if f.get("nearest_theme_park_miles") is not None and f["nearest_theme_park_miles"] <= ctx.config.airbnb.theme_park_radius_miles:
            parts.append(f"Theme park {f['nearest_theme_park_miles']} miles away.")

        fit = f.get("property_fit_factors") or []
        highlights = [x.replace("_", " ") for x in fit if x != "community_pool_only"]
        if highlights:
            parts.append(f"{f.get('bedrooms', '?')} bed with " + ", ".join(highlights) + ".")

        risks = f.get("top_risks") or []
        if risks:
            parts.append("Key risks: " + " ".join(risks))
        return f"Airbnb grade {letter_grade(score)} ({round(score)}/100). " + " ".join(parts)
