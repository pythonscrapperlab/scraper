"""
The valuation pass: derive one `property_valuation` row per property.

Runs between ingestion and scoring. Every scorer reads from here rather than
computing its own view of value, rent or cost — which is both cheaper (the
work happens once, not five times) and more coherent (all five strategies
price the same property off the same numbers).

ORDER MATTERS: `market-stats` must run first, because valuation resolves each
property to a market baseline for its tax rate, yield prior and, where comps
fail, its $/sqft.
"""

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aevorex.db.models import Property, PropertyComp, PropertyFeature, PropertyValuation
from aevorex.market.stats import resolve_market
from aevorex.normalizers.amenities import parse_amenities
from aevorex.valuation.carrying_costs import (
    DEFAULT_CARRYING_CONFIG,
    compute_noi,
    estimate_carrying_costs,
)
from aevorex.valuation.comps import estimate_value_and_arv, scale_arv_by_condition
from aevorex.valuation.insurance import estimate_annual_insurance
from aevorex.valuation.rehab import estimate_rehab
from aevorex.valuation.rent import build_zip_bed_rent_index, estimate_rent

logger = logging.getLogger("aevorex.valuation.engine")

VALUATION_VERSION = "v1"

# The classic flipper's rule of thumb, generalised: offer no more than this
# share of ARV once repairs are deducted. 70% is the textbook figure; it
# already embeds profit and cost allowances, so it is used here as a
# sanity-check offer price rather than a full underwrite.
MAO_ARV_FACTOR = 0.70

# An auction listing priced below this share of our own market value is
# publishing an opening bid, not an asking price. 0.60 is deliberately
# generous — genuine distressed sales do transact at 60-70% of market — so
# this only fires where the gap is too large to be a discount.
AUCTION_PRICE_SUSPICION_RATIO = 0.60

# How far the comp-derived $/sqft may sit from the market's own class-specific
# median before the two are treated as disagreeing rather than merely
# differing. Measured across the corpus the median ratio is 1.005 and the 95th
# percentile 1.60, so 2.0x is well outside normal variation and catches the
# comp-mismatch cases without touching genuinely premium properties.
MARKET_PPSF_DIVERGENCE_HIGH = 2.0
MARKET_PPSF_DIVERGENCE_LOW = 0.5


class ValuationEngine:
    """
    Usage:
        engine = ValuationEngine()
        stats = await engine.run(session)          # everything needing analysis
        stats = await engine.run(session, all_properties=True)
    """

    def __init__(self, valuation_version: str = VALUATION_VERSION):
        self.version = valuation_version
        self._rent_index: Dict[tuple, tuple] = {}
        self._market_cache: Dict[tuple, Optional[dict]] = {}

    async def run(
        self,
        session: AsyncSession,
        all_properties: bool = False,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Value every property that needs it, committing in batches."""
        stats = {"total": 0, "valued": 0, "errors": 0, "no_value": 0}

        # Built once for the whole run: 7,300 individual rent queries would
        # dominate the runtime of an otherwise CPU-bound pass.
        self._rent_index = await build_zip_bed_rent_index(session)

        query = select(Property)
        if not all_properties:
            query = query.where(Property.needs_analysis.is_(True))
        if limit:
            query = query.limit(limit)

        properties = (await session.execute(query)).scalars().all()
        stats["total"] = len(properties)
        logger.info("Valuing %d properties...", len(properties))

        for index, prop in enumerate(properties, 1):
            try:
                valued = await self._value_one(session, prop)
                stats["valued"] += 1
                if valued.get("market_value") is None:
                    stats["no_value"] += 1
            except Exception:
                stats["errors"] += 1
                logger.error("Failed to value property %s", prop.id, exc_info=True)

            # Batch commits: per-property would be 7,300 round trips, one
            # commit at the end would lose the whole run on a single failure.
            if index % 200 == 0:
                await session.commit()
                logger.info("  ... %d/%d", index, len(properties))

        await session.commit()
        logger.info(
            "Valuation complete: %d valued, %d without a value, %d errors.",
            stats["valued"], stats["no_value"], stats["errors"],
        )
        return stats

    async def _value_one(self, session: AsyncSession, prop: Property) -> Dict[str, Any]:
        flags: List[str] = []

        comps = (await session.execute(
            select(PropertyComp).where(PropertyComp.property_id == prop.id)
        )).scalars().all()
        features = await session.get(PropertyFeature, prop.id)
        amenities = parse_amenities(features.raw_amenities if features else None)
        market = await self._market_for(session, prop)

        # ---- value and ARV from comps ----
        valuation = estimate_value_and_arv(comps, prop, source_avm=prop.avm_value)
        flags.extend(valuation.pop("flags", []))

        market_value = valuation["market_value"]
        # Last-resort value: the market's own median $/sqft. Weak, but it keeps
        # a property scoreable rather than dropping it entirely.
        if market_value is None and prop.sqft and market and market.get("median_sold_ppsf"):
            market_value = round(float(market["median_sold_ppsf"]) * prop.sqft)
            valuation["market_value"] = market_value
            valuation["market_value_method"] = "market_ppsf"
            valuation["valuation_confidence"] = round(0.25 * float(market.get("confidence") or 0.5), 4)
            flags.append("value_from_market_median_ppsf_no_comps")

        # ---- cross-check the comp value against the market's own $/sqft ----
        # Comps carry no property type, so a small Pinecrest CONDO can end up
        # valued against Pinecrest HOUSES and come out at $967/sqft. The market
        # baseline IS class-specific, which makes it the natural second opinion.
        # Overall calibration is good (median ratio 1.005), so this only fires
        # on the 2% where the two methods genuinely disagree.
        market_value = self._reconcile_with_market_ppsf(prop, market_value, market, valuation, flags)

        # ---- renovation scope ----
        rehab = estimate_rehab(
            sqft=prop.sqft,
            year_built=prop.year_built,
            year_renovated=prop.year_renovated,
            condition_class=amenities.get("condition_class"),
            roof_class=amenities.get("roof_class"),
            has_impact_glazing=amenities.get("has_impact_glazing"),
            description=prop.description,
            ai_summary=prop.ai_summary,
        )
        flags.extend(rehab["flags"])

        # ARV depends on how much renovation there is to do, so it can only be
        # finalised once condition is known. The comp layer supplies the
        # ceiling; this scales it. Without this a 2023-built house was credited
        # with a 7% uplift for renovating nothing, and a 2022 townhouse showed
        # a 73% "return" on work that does not exist.
        valuation["arv"] = scale_arv_by_condition(
            market_value, valuation.pop("arv_ceiling", None), rehab["condition_class"]
        )

        # An auction's published figure is an opening bid or deposit, not an
        # asking price, and the actual purchase price is decided in the room.
        # The normalizer catches the obvious cases ($5,000 against a $556k
        # assessment) but cannot catch a plausible-looking one — a $60,000
        # opening bid on a $205,000 house clears every absolute threshold and
        # then produces a fictional 968% flip return. Comparing against our own
        # valuation is what catches it.
        if prop.is_auction and market_value and prop.price:
            if prop.price < market_value * AUCTION_PRICE_SUSPICION_RATIO:
                flags.append("auction_price_is_an_opening_bid_not_a_purchase_price")

        # ---- insurance ----
        insurance = estimate_annual_insurance(
            sqft=prop.sqft,
            year_built=prop.year_built,
            year_renovated=prop.year_renovated,
            property_type=prop.property_type,
            construction_class=amenities.get("construction_class"),
            roof_class=amenities.get("roof_class"),
            has_impact_glazing=amenities.get("has_impact_glazing"),
            has_storm_shutters=amenities.get("has_storm_shutters"),
            flood_factor=prop.flood_factor,
            market_value=market_value,
            for_rental=True,
        )
        flags.extend(insurance["flags"])

        # ---- rent ----
        rent = estimate_rent(
            source_rent=prop.rental_est_mid,
            zip_code=prop.zip_code,
            bedrooms=prop.bedrooms,
            price=prop.price,
            market=market,
            rent_index=self._rent_index,
            property_type=prop.property_type,
        )
        flags.extend(rent["flags"])

        # ---- carrying costs ----
        # Prefer the amenity-derived HOA where the property column is empty:
        # the amenity blob carries monthly-specific keys the column never saw.
        hoa_monthly = prop.hoa_monthly or amenities.get("hoa_monthly_amenity")
        costs = estimate_carrying_costs(
            price=prop.price,
            market_value=market_value,
            sqft=prop.sqft,
            year_built=prop.year_built,
            property_type=prop.property_type,
            tax_annual=prop.tax_annual,
            hoa_monthly=hoa_monthly,
            has_cdd=amenities.get("has_cdd"),
            annual_insurance=insurance["annual"],
            market=market,
        )
        flags.extend(costs["flags"])

        # ---- returns ----
        noi = compute_noi(
            monthly_rent=rent["monthly"],
            operating_expenses=costs["operating_expenses"],
            vacancy_rate=DEFAULT_CARRYING_CONFIG.vacancy_rate,
            management_pct=DEFAULT_CARRYING_CONFIG.management_pct,
        )
        # Cap rate is against price paid, which is what an investor actually
        # commits — not against our estimate of what it is worth.
        cap_rate = (noi / prop.price) if (noi is not None and prop.price) else None

        gross_yield = None
        if rent["monthly"] and prop.price:
            gross_yield = round(rent["monthly"] * 12 / prop.price, 5)

        price_to_value = None
        if prop.price and market_value:
            price_to_value = round(prop.price / market_value, 4)

        mao = None
        if valuation.get("arv") and rehab["mid"] is not None:
            mao = round(valuation["arv"] * MAO_ARV_FACTOR - rehab["mid"])

        payload = {
            **{k: v for k, v in valuation.items() if k != "flags"},
            "price_to_value_ratio": price_to_value,
            "rehab_cost_low": rehab["low"],
            "rehab_cost_mid": rehab["mid"],
            "rehab_cost_high": rehab["high"],
            "rehab_basis": rehab["basis"],
            "condition_class": rehab["condition_class"],
            "rent_estimate_monthly": rent["monthly"],
            "rent_method": rent["method"],
            "rent_confidence": rent["confidence"],
            "gross_yield": gross_yield,
            "annual_taxes": costs["annual_taxes"],
            "annual_insurance": costs["annual_insurance"],
            "insurance_basis": insurance["basis"],
            "annual_hoa": costs["annual_hoa"],
            "annual_cdd": costs["annual_cdd"],
            "annual_maintenance": costs["annual_maintenance"],
            "annual_operating_expenses": costs["operating_expenses"],
            "noi_annual": round(noi, 2) if noi is not None else None,
            "cap_rate": round(cap_rate, 5) if cap_rate is not None else None,
            "max_allowable_offer": mao,
            "market_geo_level": market.get("geo_level") if market else None,
            "market_geo_key": market.get("geo_key") if market else None,
            # De-duplicated but order-preserving, so the most important flag
            # (usually the first raised) stays at the top.
            "data_quality_flags": list(dict.fromkeys(flags)) or None,
            "valuation_version": self.version,
        }

        existing = await session.get(PropertyValuation, prop.id)
        if existing:
            for key, value in payload.items():
                setattr(existing, key, value)
        else:
            session.add(PropertyValuation(property_id=prop.id, **payload))

        return payload

    @staticmethod
    def _reconcile_with_market_ppsf(prop, market_value, market, valuation, flags):
        """
        Pull an outlying comp value back toward the market's own $/sqft.

        Comps arrive without a property type, so nothing stops a condo being
        valued against detached houses in the same expensive zip. The market
        baseline resolves per property class, which makes it an independent
        check the comp set cannot contaminate.

        Blends rather than replaces: the comp set knows things the market
        median does not (this specific street, this specific vintage), so a
        disagreement means both are suspect, not that one is right.
        """
        if not market_value or not prop.sqft or not market:
            return market_value
        market_ppsf = market.get("median_sold_ppsf")
        if not market_ppsf or market_ppsf <= 0:
            return market_value

        implied_ppsf = market_value / prop.sqft
        ratio = implied_ppsf / float(market_ppsf)
        if MARKET_PPSF_DIVERGENCE_LOW <= ratio <= MARKET_PPSF_DIVERGENCE_HIGH:
            return market_value

        market_implied = float(market_ppsf) * prop.sqft
        blended = round((market_value + market_implied) / 2)
        flags.append("comp_value_diverges_from_market_ppsf_blended_toward_market")
        valuation["market_value"] = blended
        valuation["market_value_method"] = "comps_market_reconciled"
        # Two methods this far apart is exactly the case where a confident
        # number would be most misleading.
        if valuation.get("valuation_confidence"):
            valuation["valuation_confidence"] = round(
                float(valuation["valuation_confidence"]) * 0.6, 4
            )
        return blended

    async def _market_for(self, session: AsyncSession, prop: Property) -> Optional[dict]:
        """Resolve and cache the market baseline for a property."""
        from aevorex.market.stats import classify_property

        key = (prop.zip_code, prop.city, prop.county, prop.state,
               classify_property(prop.property_type))
        if key not in self._market_cache:
            self._market_cache[key] = await resolve_market(
                session, prop.zip_code, prop.city, prop.county, prop.state, prop.property_type
            )
        return self._market_cache[key]
