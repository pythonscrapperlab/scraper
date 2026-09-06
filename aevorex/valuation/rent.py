"""
Long-term rent estimation, with an explicit method and confidence.

WHY A CASCADE
-------------
The source rent estimate covers 56.5% of properties and that is a verified
ceiling. Every income strategy — buy & hold, mid-term, nightly — is priced off
rent, so accepting 56.5% coverage means declining to score 43.5% of the
portfolio. Worse, the gap is not random: multi-family, the category with the
best income economics, has 1.1% coverage.

So rent falls back through three methods, each recorded on the row so a
consumer knows which one produced the number:

1. `source_avm` — the platform's own estimate. Best available; treated as
   near-truth.
2. `zip_bed_model` — median observed asking rent for the same zip and bedroom
   count, from rental listings in our own price_history. 241 zip x bedroom
   cells have 3+ observations. Real local evidence, thinner sample.
3. `market_yield_prior` — the market's median gross yield applied to this
   property's price. Weakest: it assumes the property is typical of its
   market, which is exactly what a scorer is trying to determine. Confidence
   is set low enough that a score built on it reads as provisional.

A rent that came from a prior must never be presented like a rent that came
from a comparable, which is what a single unlabelled number would do.
"""

import logging
from typing import Any, Dict, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("aevorex.valuation.rent")

# Rent observations outside this band are not long-term residential rents —
# they are data errors, daily rates, or commercial leases.
MIN_RENT = 500
MAX_RENT = 30_000

MIN_CELL_OBSERVATIONS = 3

METHOD_CONFIDENCE = {
    "source_avm": 0.90,
    "zip_bed_model": 0.60,
    "market_yield_prior": 0.30,
}

# Query the observed rental market from our own price history. `is_rental_event`
# exists precisely so these can be separated from sale prices, which average
# ~$675k against ~$4.9k for rents.
ZIP_BED_RENT_SQL = text("""
    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY ph.price) AS median_rent,
           count(*) AS n
    FROM price_history ph
    JOIN properties p ON p.id = ph.property_id
    WHERE ph.is_rental_event
      AND ph.event_type = 'listed_for_rent'
      AND ph.price BETWEEN :min_rent AND :max_rent
      AND ph.event_date >= now() - interval '24 months'
      AND p.zip_code = :zip_code
      AND p.bedrooms = :bedrooms
""")


async def build_zip_bed_rent_index(session: AsyncSession) -> Dict[tuple, tuple]:
    """
    Precompute median asking rent by (zip, bedrooms) in one query.

    Loaded once per valuation run rather than queried per property — the
    per-property version would be 7,300 round trips for a table that fits
    comfortably in memory.
    """
    rows = (await session.execute(text("""
        SELECT p.zip_code, p.bedrooms,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY ph.price) AS median_rent,
               count(*) AS n
        FROM price_history ph
        JOIN properties p ON p.id = ph.property_id
        WHERE ph.is_rental_event
          AND ph.event_type = 'listed_for_rent'
          AND ph.price BETWEEN :min_rent AND :max_rent
          AND ph.event_date >= now() - interval '24 months'
          AND p.zip_code IS NOT NULL AND p.bedrooms IS NOT NULL
        GROUP BY 1, 2
        HAVING count(*) >= :min_obs
    """), {
        "min_rent": MIN_RENT, "max_rent": MAX_RENT, "min_obs": MIN_CELL_OBSERVATIONS,
    })).mappings().all()

    index = {
        (row["zip_code"], int(row["bedrooms"])): (float(row["median_rent"]), int(row["n"]))
        for row in rows
    }
    logger.info("Built zip x bedroom rent index: %d cells.", len(index))
    return index


def estimate_rent(
    *,
    source_rent: Optional[float],
    zip_code: Optional[str],
    bedrooms: Optional[int],
    price: Optional[int],
    market: Optional[dict],
    rent_index: Optional[Dict[tuple, tuple]] = None,
    property_type: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Best available monthly rent, plus the method and confidence behind it.

    Returns {"monthly", "method", "confidence", "flags"}.
    """
    flags: list = []

    # ---- 1. the platform's own estimate ----
    if source_rent and MIN_RENT <= source_rent <= MAX_RENT:
        return {
            "monthly": round(float(source_rent), 2),
            "method": "source_avm",
            "confidence": METHOD_CONFIDENCE["source_avm"],
            "flags": flags,
        }

    # ---- 2. observed rents for this zip and bedroom count ----
    if rent_index and zip_code and bedrooms is not None:
        cell = rent_index.get((zip_code, int(bedrooms)))
        if cell:
            median_rent, n = cell
            # Confidence scales with the cell's sample; a 3-observation cell
            # is real evidence but not the same as a 60-observation one.
            confidence = METHOD_CONFIDENCE["zip_bed_model"] * min(1.0, n / 20.0)
            flags.append("rent_modelled_from_local_rental_listings")
            return {
                "monthly": round(median_rent, 2),
                "method": "zip_bed_model",
                "confidence": round(max(confidence, 0.25), 3),
                "flags": flags,
            }

    # ---- 3. the market's gross yield applied to this price ----
    if price and price > 0 and market and market.get("median_gross_yield"):
        yield_pct = float(market["median_gross_yield"])
        if 0.01 < yield_pct < 0.30:
            monthly = price * yield_pct / 12.0
            if MIN_RENT <= monthly <= MAX_RENT:
                flags.append("rent_inferred_from_market_yield_not_observed")
                return {
                    "monthly": round(monthly, 2),
                    "method": "market_yield_prior",
                    # Discounted further when the market cell itself is thin.
                    "confidence": round(
                        METHOD_CONFIDENCE["market_yield_prior"]
                        * float(market.get("confidence") or 0.5), 3
                    ),
                    "flags": flags,
                }

    # Multi-family is the systematic gap: 1.1% source coverage, and rents are
    # per-unit rather than per-property so a whole-building figure would be
    # meaningless anyway. Say so rather than inventing one.
    type_text = (property_type or "").lower()
    if "multi" in type_text:
        flags.append("multi_family_rent_not_available_excluded_from_income_scoring")
    else:
        flags.append("no_rent_estimate_available")

    return {"monthly": None, "method": None, "confidence": 0.0, "flags": flags}
