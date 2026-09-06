"""
Airbnb scorer — nightly vacation letting, under 30 days.

THE TWO THINGS THAT MAKE THIS DIFFERENT
---------------------------------------

**1. Regulation is a gate, not a factor.**

Sub-30-day letting IS regulated in Florida — by the state, through the DBPR
vacation-rental licence, and decisively by municipalities, many of which
restrict it to specific zones or prohibit it outright. Miami Beach fines
unlicensed short-term letting at five figures. Whether a given house may be
let nightly can change street by street.

Nothing in this dataset resolves that. Only about 60 of 7,329 listings state a
minimum lease term in any form, structured or in the remarks. So the gate is a
curated `municipal_rules` table that ships EMPTY, and until it is populated
every property is capped at `unverified_cap` and carries
`regulatory_status_unverified`. Hardcoding legal conclusions about named
cities would be both unreliable and a liability, so it is not done.

A gate CAPS the score rather than weighting it. No amount of beachfront charm
makes a prohibited property a good nightly rental, and a weighted penalty
would let exactly that happen — a stunning Miami Beach condo in a
no-short-let building would still surface near the top. Capping rather than
zeroing is deliberate too: a property that cannot be let nightly may be an
excellent long-term hold, and the other four scores say so.

**2. Revenue is a proxy and is labelled as one.**

There is no ADR, occupancy or booking data anywhere in this schema. Nightly
revenue here is built from bedroom capacity, location tier and amenities, and
it ships with `airbnb_revenue_is_a_proxy_not_a_forecast` on every property.
It is a way to rank properties against each other, not a number to underwrite
against.

WHAT ACTUALLY DRIVES BOOKINGS
-----------------------------
Ranked by how much they move revenue in Florida leisure markets: a private
pool (not a shared association pool — the amenity parser keeps those apart for
this reason), waterfront, sleeping capacity, and walkable proximity to
restaurants and nightlife. The location weights here are close to the inverse
of the mid-term scorer's, which is the point.
"""

from typing import List, Optional

from aevorex.scoring.base import BaseScorer, ScoreResult, ScoringContext
from aevorex.scoring.curves import (
    apply_gate,
    clamp,
    confidence_adjusted,
    linear_ramp,
    weighted_blend,
)

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

        if ctx.config.skip_placeholder_prices and prop.price_is_placeholder:
            return self.unscoreable(
                "Not scored — the listed price is a placeholder, not a purchase price.",
                ["price_is_a_placeholder_not_a_market_asking_price"],
            )

        if valuation is None or not valuation.rent_estimate_monthly:
            return self.unscoreable(
                "Couldn't score nightly letting — no rent baseline to build a nightly "
                "rate proxy from.",
                ["no_rent_estimate_cannot_score_airbnb"] + flags,
            )

        location, location_tier = self._location_score(ctx, factors, flags)
        revenue = self._revenue_score(ctx, location_tier, factors, flags)
        fit = self._property_fit_score(ctx, factors, flags)

        blended, coverage = weighted_blend([
            (location, cfg.location_weight),
            (revenue, cfg.revenue_weight),
            (fit, cfg.property_fit_weight),
        ])
        if blended is None:
            return self.unscoreable(
                "Couldn't score nightly letting — no location, revenue or property inputs "
                "resolved.",
                flags + ["insufficient_data_for_airbnb"], factors,
            )

        # ---- regulatory gate, applied last so nothing can score around it ----
        cap, verdict, gate_flags = self._regulatory_gate(ctx, factors)
        flags.extend(gate_flags)
        gated = apply_gate(blended, cap)
        factors["regulatory_verdict"] = verdict
        factors["regulatory_score_cap"] = cap

        confidence = coverage * (0.4 + 0.6 * float(valuation.rent_confidence or 0.3))
        # Regulatory uncertainty is genuine uncertainty about the strategy's
        # viability, so it belongs in confidence as well as in the cap.
        if verdict == "unverified":
            confidence *= 0.7

        final = clamp(confidence_adjusted(gated, confidence))
        factors["signal_coverage"] = coverage

        return ScoreResult(
            score=round(final, 2),
            rationale=self._rationale(ctx, factors, final),
            factors=factors,
            data_quality_flags=flags,
            confidence=round(confidence, 3),
        )

    # ------------------------------------------------------------------

    def _regulatory_gate(self, ctx, factors: dict):
        """
        Resolve whether nightly letting is permitted, and cap accordingly.

        Order matters: an outright disqualifier beats a municipal permission,
        because an HOA can forbid what a city allows.
        """
        cfg = ctx.config.airbnb
        prop = ctx.property
        flags: List[str] = []

        # A 55+ community forbids the transient occupancy nightly letting
        # requires, and is the clearest disqualifier available.
        if prop.is_age_restricted:
            flags.append("age_restricted_community_prohibits_nightly_letting")
            return cfg.age_restricted_cap, "prohibited_age_restricted", flags

        # An explicit HOA/lease restriction in the listing.
        if prop.is_rental_restricted or ctx.amenity("lease_restricted"):
            flags.append("listing_states_rental_restrictions")
            return cfg.hoa_restriction_cap, "restricted_by_hoa", flags

        min_lease = ctx.amenity("min_lease_months")
        if min_lease and min_lease >= 1:
            flags.append("minimum_lease_term_precludes_nightly_letting")
            return cfg.hoa_restriction_cap, "restricted_minimum_lease", flags

        # Municipal rules, from the curated table. Empty by default.
        city_key = f"{(prop.city or '').lower()},{prop.state or ''}"
        rule = cfg.municipal_rules.get(city_key)
        if rule == "prohibited":
            flags.append("municipality_prohibits_short_term_rental")
            return cfg.prohibited_cap, "prohibited_by_municipality", flags
        if rule == "restricted":
            flags.append("municipality_restricts_short_term_rental")
            return cfg.restricted_cap, "restricted_by_municipality", flags
        if rule == "permitted":
            # The listing positively advertises nightly letting AND the
            # municipality allows it — the only ungated case.
            if prop.allows_short_term_rental:
                return None, "permitted", flags
            return None, "permitted_by_municipality", flags

        # The default for every property until the table is populated.
        flags.append(UNVERIFIED_FLAG)
        if prop.allows_short_term_rental:
            factors["listing_advertises_str"] = True
            flags.append("listing_advertises_short_term_rental_still_verify_locally")
        return cfg.unverified_cap, "unverified", flags

    def _location_score(self, ctx, factors: dict, flags: list):
        """Leisure-traveller desirability. Returns (score, tier 0-1)."""
        cfg = ctx.config.airbnb
        location = ctx.location_score
        if location is None:
            flags.append("no_location_scores")
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

        # Waterfront and beach proximity are leisure demand drivers that the
        # walkability dimensions do not capture at all.
        bonus = 0.0
        if ctx.amenity("is_waterfront"):
            bonus += 10
            factors["waterfront"] = True
        elif ctx.amenity("has_water_view"):
            bonus += 5
        score = clamp(blended + bonus)
        return score, score / 100.0

    def _revenue_score(self, ctx, location_tier: float, factors: dict, flags: list) -> Optional[float]:
        """
        Proxy nightly economics. Explicitly not a forecast.

        Nightly rate is derived as a multiple of the property's daily
        long-term rent, and occupancy is interpolated from the location tier —
        a walkable beachfront property fills far more nights than a suburban
        one. Both are stated assumptions, not measurements.
        """
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
        net_yield = net / price

        factors.update({
            "proxy_nightly_rate": round(adr),
            "proxy_occupancy": round(occupancy, 2),
            "proxy_gross_annual_revenue": round(gross),
            "management_and_cleaning": round(management),
            "furnishing_cost": round(furnishing_annual * 5),
            "proxy_net_annual_income": round(net),
            "proxy_net_yield_pct": round(net_yield * 100, 2),
        })
        # 2% to 10% net yield spans unattractive to strong for nightly letting,
        # which carries more operational burden than a long-term hold.
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
        # A private pool is one of the strongest booking drivers in Florida.
        # An association pool is a shared amenity guests cannot rely on and
        # is worth a fraction of it.
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
            parts.append(
                "55+ age-restricted community — nightly letting is not permitted, so the "
                "score is capped regardless of the property's other merits."
            )
        elif verdict in ("restricted_by_hoa", "restricted_minimum_lease"):
            parts.append(
                "The listing states rental restrictions or a minimum lease term, which "
                "rules out or severely limits nightly letting."
            )
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

        fit = f.get("property_fit_factors") or []
        highlights = [x.replace("_", " ") for x in fit if x != "community_pool_only"]
        if highlights:
            parts.append(f"{f.get('bedrooms', '?')} bed with " + ", ".join(highlights) + ".")

        parts.append(
            "Nightly revenue is a proxy from bedroom count, location and amenities — there "
            "is no rate or occupancy data behind it."
        )
        return f"Airbnb score {round(score)}/100. " + " ".join(parts)
