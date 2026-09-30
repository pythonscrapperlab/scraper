"""
Mid-term / snowbird rental scorer — lettings of 30 days or more.

WHY THIS IS A SEPARATE STRATEGY FROM AIRBNB
-------------------------------------------
It is a different legal product, not a shorter lease.

Florida Statute 509.242 defines a regulated "vacation rental" as a unit let
for periods of less than 30 days, or more than three times in a calendar year.
At 30 days or more the property is an ordinary residential tenancy: no state
vacation-rental licence, no local vacation-rental permit, and most HOA
minimum-lease rules permit it outright.

WHAT CHANGED AND WHY (v3)
-------------------------
The v2 revenue model — a flat 1.35x premium over eight months — lost to an
annual lease on every one of 9,423 scored properties (top-decile average
uplift: minus 39%). Because the score then only tracked net yield, it
degenerated into "cheapest property wins" and ranked a $42,000 Jacksonville
house as the third-best snowbird rental in the corpus.

A Florida seasonal let is not a flat premium. In-season (roughly January to
April) furnished rents in the snowbird markets run 1.8-2.5x the annual rate,
shoulder months sit a little above annual, and summer is soft or empty. It is
now modelled as three blocks with a per-market in-season multiplier, and the
revenue score answers the question that matters — is mid-term better than an
annual lease HERE — as well as whether the yield is good. Where the annual
lease wins, the strategy says so and is capped rather than pretending.

Also new: hospital proximity. Travelling healthcare staff on 13-week
contracts are the other half of mid-term demand, and a hospital inside three
miles is a stronger signal than any walkability dimension.
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
from aevorex.scoring.utils import nearest_hospital_miles


class MidTermRentalScorer(BaseScorer):
    """Registered as `str` — mid-term/snowbird letting, 30+ days."""

    STRATEGY_KEY = "str"

    def score(self, ctx: ScoringContext) -> ScoreResult:
        cfg = ctx.config.mid_term
        prop = ctx.property
        valuation = ctx.valuation
        flags: List[str] = []
        factors: dict = {}
        risks: List[str] = []

        if ctx.config.skip_placeholder_prices and prop.price_is_placeholder:
            return self.with_breakdown(ctx, self.unscoreable(
                "Not scored — the listed price is a placeholder, not a purchase price.",
                ["price_is_a_placeholder_not_a_market_asking_price"],
            ))

        if valuation is None or not valuation.rent_estimate_monthly:
            return self.with_breakdown(ctx, self.unscoreable(
                "Couldn't score mid-term letting — no long-term rent estimate to build a "
                "seasonal premium from.",
                ["no_rent_estimate_cannot_score_mid_term"],
            ))

        location = self._location_score(ctx, factors, flags)
        revenue, uplift = self._revenue_score(ctx, factors, flags)
        suitability = self._suitability_score(ctx, factors, flags, risks)

        blended, coverage = weighted_blend([
            (location, cfg.location_weight),
            (revenue, cfg.revenue_weight),
            (suitability, cfg.suitability_weight),
        ])
        if blended is None:
            return self.with_breakdown(ctx, self.unscoreable(
                "Couldn't score mid-term letting — no location, revenue or suitability "
                "inputs resolved.",
                flags + ["insufficient_data_for_mid_term"], factors,
            ))

        # A minimum lease longer than a snowbird season defeats the strategy.
        min_lease = ctx.amenity("min_lease_months")
        if min_lease and min_lease >= cfg.long_minimum_lease_months:
            blended -= cfg.long_minimum_lease_penalty
            factors["minimum_lease_months"] = min_lease
            flags.append("minimum_lease_exceeds_snowbird_season")
            risks.append(f"Minimum lease {min_lease} months exceeds a snowbird season.")

        # Where an annual lease earns more, mid-term is the wrong strategy for
        # this property and the score must not suggest otherwise.
        cap = None
        if uplift is not None and uplift < 0:
            cap = cfg.annual_lease_wins_cap
            flags.append("annual_lease_beats_mid_term_here")
            risks.insert(0, f"An annual lease earns about {abs(uplift):.0f}% more than seasonal letting here.")
        blended = apply_gate(blended, cap)

        confidence = coverage * (0.5 + 0.5 * float(valuation.rent_confidence or 0.3))
        final = clamp(confidence_adjusted(blended, confidence))
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
            component_scores={
                "location": location, "revenue": revenue, "suitability": suitability,
            },
            score_adjustments=[
                {"type": "penalty", "label": "Minimum lease exceeds snowbird season",
                 "value": cfg.long_minimum_lease_penalty,
                 "applied": bool(min_lease and min_lease >= cfg.long_minimum_lease_months)},
                {"type": "gate", "label": "Annual lease earns more",
                 "value": cfg.annual_lease_wins_cap, "applied": uplift is not None and uplift < 0},
                {"type": "cap", "label": "Effective strategy-fit cap",
                 "value": cap or 100.0, "applied": cap is not None},
                {"type": "confidence_shrink", "label": "Rent and evidence confidence",
                 "value": confidence, "floor": 0.55, "applied": True},
            ],
        )
        return self.with_breakdown(ctx, result)

    def breakdown(self, ctx: ScoringContext, result: ScoreResult) -> dict:
        cfg = ctx.config.mid_term
        f, scores = result.factors, result.component_scores
        fit = f.get("suitability_factors") or []
        top = list(result.score_adjustments) or [
            {"type": "penalty", "label": "Minimum lease exceeds snowbird season",
             "value": cfg.long_minimum_lease_penalty, "applied": False},
            {"type": "gate", "label": "Annual lease earns more",
             "value": cfg.annual_lease_wins_cap, "applied": False},
            {"type": "cap", "label": "Effective strategy-fit cap", "value": 100.0,
             "applied": False},
            {"type": "confidence_shrink", "label": "Rent and evidence confidence",
             "value": 0.0, "floor": 0.55, "applied": False},
        ]
        adjustments = [
            {"type": "component_bonus", "label": "Age-restricted snowbird community",
             "value": cfg.age_restricted_bonus,
             "applied": "age_restricted_community_matches_snowbird_demand" in fit,
             "component": "suitability"},
            {"type": "component_bonus", "label": "Furnished",
             "value": cfg.furnished_bonus,
             "applied": any(item.startswith("furnished:") for item in fit),
             "component": "suitability", "reason": "Scaled by furnishing rank"},
            {"type": "component_bonus", "label": "Pool access", "value": cfg.pool_bonus,
             "applied": "pool_access" in fit, "component": "suitability"},
            {"type": "component_bonus", "label": "Hospital within three miles",
             "value": cfg.hospital_bonus,
             "applied": "hospital_within_3_miles_travel_nurse_demand" in fit,
             "component": "suitability"},
            *top,
        ]
        components = [
            component("location", "Mid-term resident location", cfg.location_weight,
                      scores.get("location"), [
                {"label": "Lifestyle dimensions", "value": f.get("location_dimensions_0_10", {}),
                 "field": "location_scores"},
            ]),
            component("revenue", "Seasonal rental economics", cfg.revenue_weight,
                      scores.get("revenue"), [
                {"label": "In-season monthly rent", "value": f.get("in_season_monthly_rent"),
                 "field": "rent_estimate_monthly"},
                {"label": "Net yield", "value": f.get("net_yield_pct"), "field": "net_yield_pct"},
                {"label": "Uplift versus annual lease", "value": f.get("uplift_vs_annual_lease_pct"),
                 "field": "uplift_vs_annual_lease_pct"},
            ]),
            component("suitability", "Seasonal tenant suitability", cfg.suitability_weight,
                      scores.get("suitability"), [
                {"label": "Property fit signals", "value": fit, "field": "property_features"},
                {"label": "Nearest hospital miles", "value": f.get("nearest_hospital_miles"),
                 "field": "points_of_interest.distance_miles"},
            ]),
        ]
        return make_breakdown(lens=self.STRATEGY_KEY, version=ctx.config.version,
                              result=result, components=components, adjustments=adjustments)

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

    def _season_multiplier(self, ctx) -> float:
        cfg = ctx.config.mid_term
        key = f"{(ctx.property.city or '').lower()},{ctx.property.state or ''}"
        return cfg.season_multiplier_by_market.get(key, cfg.season_multiplier)

    def _revenue_score(self, ctx, factors: dict, flags: list):
        """
        Seasonal net yield versus the same property let annually.

        Returns (score, uplift_pct). The uplift is the actual question — is
        mid-term better here — and carries most of the weight; net yield
        keeps a cheap high-yield property ahead of an expensive one when
        both clear the annual-lease bar.
        """
        cfg = ctx.config.mid_term
        valuation = ctx.valuation
        price = ctx.property.price
        base_rent = float(valuation.rent_estimate_monthly)
        if not price:
            return None, None

        season_mult = self._season_multiplier(ctx)
        vacant_months = max(0.0, 12.0 - cfg.season_months - cfg.shoulder_months)
        gross = base_rent * (cfg.season_months * season_mult
                             + cfg.shoulder_months * cfg.shoulder_multiplier)
        management = gross * cfg.management_pct
        operating = float(valuation.annual_operating_expenses or 0)

        # Furnishing is real up-front capital, amortised over five years.
        furnishing = (ctx.property.sqft or 1200) * cfg.furnishing_cost_per_sqft
        furnishing_annual = furnishing / 5.0

        net = gross - management - operating - furnishing_annual
        basis = ctx.all_in_basis() or price
        net_yield = net / basis

        # The same property on an annual lease: 8% vacancy, 9% management.
        annual_net = (base_rent * 12 * 0.92) - operating - (base_rent * 12 * 0.09)
        uplift = ((net - annual_net) / abs(annual_net) * 100) if annual_net else None

        factors.update({
            "monthly_rent_long_term": round(base_rent),
            "in_season_monthly_rent": round(base_rent * season_mult),
            "in_season_multiplier": season_mult,
            "season_months": cfg.season_months,
            "shoulder_months": cfg.shoulder_months,
            "vacant_months": vacant_months,
            "gross_seasonal_revenue": round(gross),
            "furnishing_cost": round(furnishing),
            "net_annual_income": round(net),
            "all_in_basis": round(basis),
            "net_yield_pct": round(net_yield * 100, 2),
            "annual_lease_net_income": round(annual_net),
            "uplift_vs_annual_lease_pct": round(uplift, 1) if uplift is not None else None,
        })
        if valuation.rent_method != "source_avm":
            flags.append("mid_term_revenue_built_on_a_modelled_rent")

        uplift_score = linear_ramp(uplift, 0.0, cfg.uplift_pct_for_full_score) if uplift is not None else None
        yield_score = linear_ramp(net_yield, 0.02, 0.08)
        score, _ = weighted_blend([
            (uplift_score, cfg.uplift_weight),
            (yield_score, cfg.net_yield_weight),
        ])
        return score, uplift

    def _suitability_score(self, ctx, factors: dict, flags: list, risks: list) -> Optional[float]:
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
            applied.append("unfurnished_furnishing_cost_included")

        if ctx.amenity("has_private_pool") or ctx.amenity("has_community_pool"):
            score += cfg.pool_bonus
            applied.append("pool_access")

        hospital = nearest_hospital_miles(ctx.pois)
        if hospital is not None:
            factors["nearest_hospital_miles"] = round(hospital, 2)
            if hospital <= cfg.hospital_radius_miles:
                score += cfg.hospital_bonus
                applied.append("hospital_within_3_miles_travel_nurse_demand")

        # An HOA rental restriction is far less likely to bite at 30+ days,
        # but where the listing states one it is still a real risk.
        if ctx.property.is_rental_restricted or ctx.amenity("lease_restricted"):
            score -= 12.0
            applied.append("listing_states_lease_restrictions")
            flags.append("lease_restrictions_verify_minimum_term_with_hoa")
            risks.append("Listing states lease restrictions — confirm the HOA minimum term allows a seasonal let.")

        if ctx.amenity("pets_allowed") is True:
            score += 4.0
            applied.append("pets_allowed")

        factors["suitability_factors"] = applied
        return clamp(score)

    # ------------------------------------------------------------------

    def _rationale(self, ctx, f: dict, score: float) -> str:
        parts = []
        if f.get("in_season_monthly_rent"):
            parts.append(
                f"Est. ${f['in_season_monthly_rent']:,.0f}/month in season "
                f"({f['in_season_multiplier']}x the ${f['monthly_rent_long_term']:,.0f} annual rate) "
                f"for {f['season_months']:.0f} months, shoulder rates for {f['shoulder_months']:.0f}, "
                f"vacant {f['vacant_months']:.0f}: nets ${f.get('net_annual_income', 0):,.0f} "
                f"at a {f.get('net_yield_pct')}% yield."
            )
        uplift = f.get("uplift_vs_annual_lease_pct")
        if uplift is not None:
            if uplift > 5:
                parts.append(f"About {uplift}% better than letting it annually.")
            elif uplift < 0:
                parts.append(
                    f"Roughly {abs(uplift)}% WORSE than an annual lease — the seasonal "
                    "void and running costs outweigh the premium here, so the score is capped."
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
        if "hospital_within_3_miles_travel_nurse_demand" in applied:
            parts.append(f"Hospital {f.get('nearest_hospital_miles')} miles away — travelling-nurse demand.")

        risks = f.get("top_risks") or []
        if risks:
            parts.append("Key risks: " + " ".join(risks))

        parts.append(
            "Lettings of 30+ days are not regulated as vacation rentals in Florida, so the "
            "licensing risk that applies to nightly letting does not apply here."
        )
        return f"Mid-term/snowbird grade {letter_grade(score)} ({round(score)}/100). " + " ".join(parts)
