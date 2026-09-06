"""
Mid-term / snowbird rental scorer — lettings of 30 days or more.

WHY THIS IS A SEPARATE STRATEGY FROM AIRBNB
-------------------------------------------
It is a different legal product, not a shorter lease.

Florida Statute 509.242 defines a regulated "vacation rental" as a unit let
for periods of less than 30 days, or more than three times in a calendar year.
At 30 days or more the property is an ordinary residential tenancy: no state
vacation-rental licence, no local vacation-rental permit, and — critically —
most HOA minimum-lease rules, which typically run one to three months, permit
it outright. The regulatory risk that dominates the nightly strategy largely
evaporates here.

The demand profile inverts too. Snowbirds, travelling healthcare staff and
relocating families want quiet, comfortable, furnished, near groceries and
healthcare, with somewhere to park. That is close to the opposite of the
nightly-rental profile, which wants restaurants, nightlife and walkable
buzz. The corpus separates cleanly on exactly this axis: Naples and Jupiter
score nightlife 3 / quiet 8, while Miami Beach scores nightlife 9 / quiet 4.

Most tellingly, a 55+ age restriction is a POSITIVE here and disqualifying for
nightly letting. Same column, opposite sign, which is the clearest possible
demonstration that folding these into one score would destroy information.
"""

from typing import List, Optional

from aevorex.scoring.base import BaseScorer, ScoreResult, ScoringContext
from aevorex.scoring.curves import (
    clamp,
    confidence_adjusted,
    linear_ramp,
    weighted_blend,
)


class MidTermRentalScorer(BaseScorer):
    """Registered as `str` — mid-term/snowbird letting, 30+ days."""

    STRATEGY_KEY = "str"

    def score(self, ctx: ScoringContext) -> ScoreResult:
        cfg = ctx.config.mid_term
        prop = ctx.property
        valuation = ctx.valuation
        flags: List[str] = []
        factors: dict = {}

        if ctx.config.skip_placeholder_prices and prop.price_is_placeholder:
            return self.unscoreable(
                "Not scored — the listed price is a placeholder, not a purchase price.",
                ["price_is_a_placeholder_not_a_market_asking_price"],
            )

        if valuation is None or not valuation.rent_estimate_monthly:
            return self.unscoreable(
                "Couldn't score mid-term letting — no long-term rent estimate to build a "
                "seasonal premium from.",
                ["no_rent_estimate_cannot_score_mid_term"],
            )

        location = self._location_score(ctx, factors, flags)
        revenue = self._revenue_score(ctx, factors, flags)
        suitability = self._suitability_score(ctx, factors, flags)

        blended, coverage = weighted_blend([
            (location, cfg.location_weight),
            (revenue, cfg.revenue_weight),
            (suitability, cfg.suitability_weight),
        ])
        if blended is None:
            return self.unscoreable(
                "Couldn't score mid-term letting — no location, revenue or suitability "
                "inputs resolved.",
                flags + ["insufficient_data_for_mid_term"], factors,
            )

        # A minimum lease longer than a snowbird season defeats the strategy —
        # the tenant pool for a 7+ month furnished let is much closer to a
        # long-term rental, so it should be scored as one.
        min_lease = ctx.amenity("min_lease_months")
        if min_lease and min_lease >= cfg.long_minimum_lease_months:
            blended -= cfg.long_minimum_lease_penalty
            factors["minimum_lease_months"] = min_lease
            flags.append("minimum_lease_exceeds_snowbird_season")

        confidence = coverage * (0.5 + 0.5 * float(valuation.rent_confidence or 0.3))
        final = clamp(confidence_adjusted(blended, confidence))
        factors["signal_coverage"] = coverage

        return ScoreResult(
            score=round(final, 2),
            rationale=self._rationale(ctx, factors, final),
            factors=factors,
            data_quality_flags=flags,
            confidence=round(confidence, 3),
        )

    # ------------------------------------------------------------------

    def _location_score(self, ctx, factors: dict, flags: list) -> Optional[float]:
        """Lifestyle fit for a three-month resident, not a weekend visitor."""
        cfg = ctx.config.mid_term
        location = ctx.location_score
        if location is None:
            flags.append("no_location_scores")
            return None

        components = []
        detail = {}
        for dimension, weight in cfg.location_dimensions.items():
            value = getattr(location, dimension, None)
            if value is not None:
                detail[dimension] = round(float(value), 1)
                components.append((float(value) * 10, weight))
        if not components:
            return None

        blended, _ = weighted_blend(components)
        factors["location_dimensions_0_10"] = detail
        return blended

    def _revenue_score(self, ctx, factors: dict, flags: list) -> Optional[float]:
        """
        Seasonal net yield: a furnished premium over a shorter occupied year.

        A mid-term let earns more per month than an annual lease but is empty
        for part of the year and costs more to run. Whether that trade is
        worth making is the actual question, so it is modelled explicitly
        rather than assumed to be positive.
        """
        cfg = ctx.config.mid_term
        valuation = ctx.valuation
        price = ctx.property.price
        base_rent = float(valuation.rent_estimate_monthly)
        if not price:
            return None

        premium_rent = base_rent * cfg.rent_premium_multiplier
        gross = premium_rent * cfg.occupied_months_per_year
        management = gross * cfg.management_pct
        operating = float(valuation.annual_operating_expenses or 0)

        # Furnishing is real up-front capital, amortised over five years.
        furnishing = (ctx.property.sqft or 1200) * cfg.furnishing_cost_per_sqft
        furnishing_annual = furnishing / 5.0

        net = gross - management - operating - furnishing_annual
        net_yield = net / price

        # Compared against the same property let annually, so the score
        # answers "is mid-term better here", not "is this property good".
        annual_net = (base_rent * 12 * 0.92) - operating - (base_rent * 12 * 0.09)
        uplift = (net - annual_net) / abs(annual_net) if annual_net else None

        factors.update({
            "monthly_rent_long_term": round(base_rent),
            "monthly_rent_mid_term": round(premium_rent),
            "occupied_months": cfg.occupied_months_per_year,
            "gross_seasonal_revenue": round(gross),
            "furnishing_cost": round(furnishing),
            "net_annual_income": round(net),
            "net_yield_pct": round(net_yield * 100, 2),
            "uplift_vs_annual_lease_pct": round(uplift * 100, 1) if uplift is not None else None,
        })
        if valuation.rent_method != "source_avm":
            flags.append("mid_term_revenue_built_on_a_modelled_rent")

        # 2% to 8% net yield spans unattractive to strong for this strategy.
        return linear_ramp(net_yield, 0.02, 0.08)

    def _suitability_score(self, ctx, factors: dict, flags: list) -> Optional[float]:
        """Property attributes a furnished seasonal tenant actually cares about."""
        cfg = ctx.config.mid_term
        score = 50.0
        applied = []

        # 55+ communities ARE the snowbird market.
        if ctx.property.is_age_restricted:
            score += cfg.age_restricted_bonus
            applied.append("age_restricted_community_matches_snowbird_demand")

        furnished_rank = ctx.amenity("furnished_rank")
        if furnished_rank:
            score += cfg.furnished_bonus * furnished_rank
            applied.append(f"furnished:{ctx.amenity('furnished_level')}")
        elif ctx.amenity("furnished_level") == "unfurnished":
            # Not a penalty — furnishing is a cost already counted in revenue.
            applied.append("unfurnished_furnishing_cost_included")

        if ctx.amenity("has_private_pool") or ctx.amenity("has_community_pool"):
            score += cfg.pool_bonus
            applied.append("pool_access")

        # An HOA rental restriction is far less likely to bite at 30+ days,
        # but where the listing states one it is still a real risk.
        if ctx.property.is_rental_restricted or ctx.amenity("lease_restricted"):
            score -= 12.0
            applied.append("listing_states_lease_restrictions")
            flags.append("lease_restrictions_verify_minimum_term_with_hoa")

        if ctx.amenity("pets_allowed") is True:
            score += 4.0
            applied.append("pets_allowed")

        factors["suitability_factors"] = applied
        return clamp(score)

    # ------------------------------------------------------------------

    def _rationale(self, ctx, f: dict, score: float) -> str:
        parts = []
        if f.get("monthly_rent_mid_term"):
            parts.append(
                f"Est. ${f['monthly_rent_mid_term']:,.0f}/month furnished over "
                f"{f['occupied_months']:.0f} months a year "
                f"(vs ${f['monthly_rent_long_term']:,.0f} on an annual lease), "
                f"netting ${f.get('net_annual_income', 0):,.0f} at a "
                f"{f.get('net_yield_pct')}% yield."
            )
        uplift = f.get("uplift_vs_annual_lease_pct")
        if uplift is not None:
            if uplift > 5:
                parts.append(f"About {uplift}% better than letting it annually.")
            elif uplift < -5:
                parts.append(
                    f"Roughly {abs(uplift)}% WORSE than an annual lease — the seasonal "
                    "void and running costs outweigh the premium here."
                )

        dims = f.get("location_dimensions_0_10") or {}
        if dims.get("quiet_score") is not None:
            parts.append(
                f"Quiet {dims['quiet_score']}/10, wellness {dims.get('wellness_score', '?')}/10, "
                f"groceries {dims.get('groceries_score', '?')}/10 — the snowbird profile."
            )

        applied = f.get("suitability_factors") or []
        if "age_restricted_community_matches_snowbird_demand" in applied:
            parts.append("55+ community, which is the core seasonal market.")
        if any(a.startswith("furnished:") for a in applied):
            parts.append("Already furnished, so no fit-out capital needed.")

        parts.append(
            "Lettings of 30+ days are not regulated as vacation rentals in Florida, so the "
            "licensing risk that applies to nightly letting does not apply here."
        )
        return f"Mid-term/snowbird score {round(score)}/100. " + " ".join(parts)
