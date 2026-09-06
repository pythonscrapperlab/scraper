"""
Build `market_stats` — the baseline every market-relative score is measured against.

WHY
---
The previous scorers compared every property to fixed thresholds. Median sold
$/sqft in this corpus runs $171 (Pensacola 32526) to $651 (Brickell 33131), a
3.8x spread. Against that, a single "$/sqft is good below X" rule is not a
weak signal, it is a wrong one: it ranks every Pensacola listing above every
Miami listing regardless of the actual deal.

The same applies to time on market. 90 days is unremarkable for Naples luxury
and a strong distress signal for an Orlando starter home. Percentile-within-
market is the only way to compare them.

SOURCE
------
Sold-side statistics come from `property_comps`, not from our own price
history. Comps ARE sold transactions, 100% of them carry a zip in the address,
and there are 39,735 of them sold within the last twelve months across 274
zips with 20+ each. Our own `price_history` holds only ~1,242 sold events over
24 months, so comps are roughly 30x the sample at better recency — and they
need no additional scraping.

Listing-side statistics (time on market, price-cut prevalence, asking $/sqft)
come from our own `properties` corpus, which is the live inventory.

GRAIN AND FALLBACK
------------------
One row per (geo_level, geo_key, property_class, as_of_date). Lookups resolve
zip -> city -> county -> state and take the first level meeting a minimum
sample, so every property gets a baseline; `confidence` degrades as the match
gets coarser. A property in a thin zip is scored against its city rather than
against nothing, and the score carries the fact that it had to.
"""

import logging
from datetime import date
from typing import Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("aevorex.market.stats")

# Below this many observations a cell is too thin to be a baseline and the
# caller should fall back to a coarser geography. 8 is deliberately low: a
# handful of local sales still beats a state-wide median for a market as
# heterogeneous as Florida.
MIN_SAMPLE = 8

# Sample size at which a cell is considered fully trustworthy. Confidence
# ramps linearly to this and then flattens.
FULL_CONFIDENCE_SAMPLE = 40

# Each coarser geography multiplies confidence down — a state-level median is
# a real number but it is barely evidence about one property.
GEO_CONFIDENCE = {"zip": 1.0, "city": 0.75, "county": 0.5, "state": 0.25}

GEO_LEVELS = ("zip", "city", "county", "state")

# Coarse on purpose. Splitting finer starves most cells: multi_family is only
# 206 properties corpus-wide, so a per-zip multi_family cell would almost
# never clear MIN_SAMPLE.
PROPERTY_CLASS_SQL = """
    CASE
        WHEN {col} ILIKE '%%single family%%' THEN 'single_family'
        WHEN {col} ILIKE '%%condo%%' OR {col} ILIKE '%%co-op%%' THEN 'condo'
        WHEN {col} ILIKE '%%townhouse%%' THEN 'townhouse'
        WHEN {col} ILIKE '%%multi-family%%' OR {col} ILIKE '%%multi family%%' THEN 'multi_family'
        ELSE 'other'
    END
"""

# Guards against the junk that survives in any scraped price series: $100
# quitclaim deeds, and $/sqft values no Florida market produces.
SOLD_SANITY = "c.price BETWEEN 10000 AND 100000000 AND c.sqft BETWEEN 200 AND 30000"
PPSF_SANITY = "(c.price::numeric / c.sqft) BETWEEN 20 AND 3000"


def _confidence(sample: int, geo_level: str) -> float:
    """Sample-driven confidence, discounted for how coarse the geography is."""
    if sample <= 0:
        return 0.0
    ramp = min(1.0, sample / FULL_CONFIDENCE_SAMPLE)
    return round(ramp * GEO_CONFIDENCE.get(geo_level, 0.25), 4)


# --------------------------------------------------------------------------
# The geography expression for each level, for comps (zip parsed out of the
# comp address) and for our own listings (columns already present).
# --------------------------------------------------------------------------

_COMP_GEO = {
    # 100% of comp addresses end in a zip; verified 40,882 of 40,885.
    "zip": r"substring(c.comp_address from '(\d{5})(?:-\d{4})?\s*$')",
    # Comps carry no city/county of their own, so coarser levels are
    # attributed from the subject property they were pulled for. A comp is by
    # construction near its subject, so this is sound.
    "city": "lower(p.city) || ',' || p.state",
    "county": "lower(coalesce(p.county, '')) || ',' || p.state",
    "state": "p.state",
}

_LISTING_GEO = {
    "zip": "p.zip_code",
    "city": "lower(p.city) || ',' || p.state",
    "county": "lower(coalesce(p.county, '')) || ',' || p.state",
    "state": "p.state",
}


def _sold_sql(geo_level: str) -> str:
    """Sold-price baseline for one geography level, from comps."""
    return f"""
        SELECT {_COMP_GEO[geo_level]} AS geo_key,
               {PROPERTY_CLASS_SQL.format(col='p.property_type')} AS property_class,
               count(*)                                                       AS sold_count,
               percentile_cont(0.5)  WITHIN GROUP (ORDER BY c.price::numeric / c.sqft) AS median_sold_ppsf,
               percentile_cont(0.25) WITHIN GROUP (ORDER BY c.price::numeric / c.sqft) AS p25_sold_ppsf,
               percentile_cont(0.75) WITHIN GROUP (ORDER BY c.price::numeric / c.sqft) AS p75_sold_ppsf,
               percentile_cont(0.5)  WITHIN GROUP (ORDER BY c.price)          AS median_sold_price
        FROM property_comps c
        JOIN properties p ON p.id = c.property_id
        WHERE c.sold_date >= now() - interval '12 months'
          AND {SOLD_SANITY} AND {PPSF_SANITY}
          AND {_COMP_GEO[geo_level]} IS NOT NULL
        GROUP BY 1, 2
        HAVING count(*) >= :min_sample
    """


def _listing_sql(geo_level: str) -> str:
    """
    Listing-side baseline for one geography level, from our own inventory.

    `pct_listings_with_cut` is the denominator that makes a price cut
    interpretable: in a market where 60% of sellers have cut, one cut says
    little; where 8% have, it says a lot.
    """
    return f"""
        WITH cuts AS (
            SELECT property_id, count(*) AS n_cuts
            FROM price_history
            WHERE event_type = 'reduced' AND NOT is_rental_event
            GROUP BY 1
        )
        SELECT {_LISTING_GEO[geo_level]} AS geo_key,
               {PROPERTY_CLASS_SQL.format(col='p.property_type')} AS property_class,
               count(*) AS listing_count,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY p.price::numeric / nullif(p.sqft, 0))
                   FILTER (WHERE p.sqft > 0)                       AS median_list_ppsf,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY p.price)  AS median_list_price,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY p.days_on_market) AS median_dom,
               percentile_cont(0.75) WITHIN GROUP (ORDER BY p.days_on_market) AS p75_dom,
               avg(CASE WHEN cuts.n_cuts > 0 THEN 1.0 ELSE 0.0 END) AS pct_listings_with_cut,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY p.tax_annual / nullif(p.price, 0))
                   FILTER (WHERE p.tax_annual > 0 AND p.price > 0) AS median_tax_rate,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY p.hoa_monthly)
                   FILTER (WHERE p.hoa_monthly > 0 AND p.hoa_monthly < 50000) AS median_hoa_monthly,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY p.rental_est_mid * 12.0 / nullif(p.price, 0))
                   FILTER (WHERE p.rental_est_mid > 0 AND p.price > 0) AS median_gross_yield,
               count(*) FILTER (WHERE p.rental_est_mid > 0)         AS rent_sample
        FROM properties p
        LEFT JOIN cuts ON cuts.property_id = p.id
        WHERE p.price > 0
          AND coalesce(p.price_is_placeholder, false) = false
          AND {_LISTING_GEO[geo_level]} IS NOT NULL
        GROUP BY 1, 2
        HAVING count(*) >= :min_sample
    """


UPSERT_SQL = text("""
    INSERT INTO market_stats (
        id, geo_level, geo_key, property_class, as_of_date,
        median_sold_ppsf, p25_sold_ppsf, p75_sold_ppsf, median_sold_price, sold_count,
        median_list_ppsf, median_list_price, median_dom, p75_dom,
        pct_listings_with_cut, median_price_cut_pct, listing_count,
        median_gross_yield, median_rent_per_bed, rent_sample,
        median_tax_rate, median_hoa_monthly, confidence, created_at
    ) VALUES (
        gen_random_uuid(), :geo_level, :geo_key, :property_class, :as_of_date,
        :median_sold_ppsf, :p25_sold_ppsf, :p75_sold_ppsf, :median_sold_price, :sold_count,
        :median_list_ppsf, :median_list_price, :median_dom, :p75_dom,
        :pct_listings_with_cut, :median_price_cut_pct, :listing_count,
        :median_gross_yield, :median_rent_per_bed, :rent_sample,
        :median_tax_rate, :median_hoa_monthly, :confidence, now()
    )
    ON CONFLICT (geo_level, geo_key, property_class, as_of_date) DO UPDATE SET
        median_sold_ppsf = EXCLUDED.median_sold_ppsf,
        p25_sold_ppsf = EXCLUDED.p25_sold_ppsf,
        p75_sold_ppsf = EXCLUDED.p75_sold_ppsf,
        median_sold_price = EXCLUDED.median_sold_price,
        sold_count = EXCLUDED.sold_count,
        median_list_ppsf = EXCLUDED.median_list_ppsf,
        median_list_price = EXCLUDED.median_list_price,
        median_dom = EXCLUDED.median_dom,
        p75_dom = EXCLUDED.p75_dom,
        pct_listings_with_cut = EXCLUDED.pct_listings_with_cut,
        median_price_cut_pct = EXCLUDED.median_price_cut_pct,
        listing_count = EXCLUDED.listing_count,
        median_gross_yield = EXCLUDED.median_gross_yield,
        median_rent_per_bed = EXCLUDED.median_rent_per_bed,
        rent_sample = EXCLUDED.rent_sample,
        median_tax_rate = EXCLUDED.median_tax_rate,
        median_hoa_monthly = EXCLUDED.median_hoa_monthly,
        confidence = EXCLUDED.confidence
""")


async def build_market_stats(
    session: AsyncSession, as_of: Optional[date] = None
) -> Dict[str, int]:
    """
    Recompute every market_stats cell and upsert it.

    Idempotent: re-running for the same `as_of` overwrites that day's rows
    rather than accumulating duplicates.
    """
    as_of = as_of or date.today()
    stats = {"cells_written": 0, "sold_cells": 0, "listing_cells": 0}

    # geo_key -> merged row. Sold and listing statistics are computed
    # separately (different sources, different filters) and merged on the
    # grain, so a cell can exist with only one side populated.
    merged: Dict[tuple, dict] = {}

    for geo_level in GEO_LEVELS:
        sold_rows = (await session.execute(
            text(_sold_sql(geo_level)), {"min_sample": MIN_SAMPLE}
        )).mappings().all()
        for row in sold_rows:
            key = (geo_level, row["geo_key"], row["property_class"])
            merged.setdefault(key, {})["sold"] = dict(row)
        stats["sold_cells"] += len(sold_rows)

        listing_rows = (await session.execute(
            text(_listing_sql(geo_level)), {"min_sample": MIN_SAMPLE}
        )).mappings().all()
        for row in listing_rows:
            key = (geo_level, row["geo_key"], row["property_class"])
            merged.setdefault(key, {})["listing"] = dict(row)
        stats["listing_cells"] += len(listing_rows)

        logger.info(
            "%s: %d sold cells, %d listing cells", geo_level, len(sold_rows), len(listing_rows)
        )

    # Also emit an 'all' property_class per geography, so a property whose
    # type is unusual (or missing) still resolves to something.
    for geo_level in GEO_LEVELS:
        await _add_all_class_rows(session, geo_level, merged)

    for (geo_level, geo_key, property_class), parts in merged.items():
        sold = parts.get("sold") or {}
        listing = parts.get("listing") or {}
        sample = max(int(sold.get("sold_count") or 0), int(listing.get("listing_count") or 0))
        await session.execute(UPSERT_SQL, {
            "geo_level": geo_level,
            "geo_key": geo_key,
            "property_class": property_class,
            "as_of_date": as_of,
            "median_sold_ppsf": _f(sold.get("median_sold_ppsf")),
            "p25_sold_ppsf": _f(sold.get("p25_sold_ppsf")),
            "p75_sold_ppsf": _f(sold.get("p75_sold_ppsf")),
            "median_sold_price": _i(sold.get("median_sold_price")),
            "sold_count": int(sold.get("sold_count") or 0),
            "median_list_ppsf": _f(listing.get("median_list_ppsf")),
            "median_list_price": _i(listing.get("median_list_price")),
            "median_dom": _f(listing.get("median_dom")),
            "p75_dom": _f(listing.get("p75_dom")),
            "pct_listings_with_cut": _f(listing.get("pct_listings_with_cut")),
            # Depth of cut is per-property, not a market aggregate we can read
            # off this query; left NULL until a scorer needs it.
            "median_price_cut_pct": None,
            "listing_count": int(listing.get("listing_count") or 0),
            "median_gross_yield": _f(listing.get("median_gross_yield")),
            "median_rent_per_bed": None,
            "rent_sample": int(listing.get("rent_sample") or 0),
            "median_tax_rate": _f(listing.get("median_tax_rate")),
            "median_hoa_monthly": _f(listing.get("median_hoa_monthly")),
            "confidence": _confidence(sample, geo_level),
        })
        stats["cells_written"] += 1

    await session.commit()
    logger.info("Wrote %d market_stats cells for %s.", stats["cells_written"], as_of)
    return stats


async def _add_all_class_rows(session: AsyncSession, geo_level: str, merged: dict) -> None:
    """Compute the property-class-agnostic cell for a geography level."""
    sold_sql = _sold_sql(geo_level).replace(
        PROPERTY_CLASS_SQL.format(col="p.property_type"), "'all'"
    )
    listing_sql = _listing_sql(geo_level).replace(
        PROPERTY_CLASS_SQL.format(col="p.property_type"), "'all'"
    )
    for kind, sql in (("sold", sold_sql), ("listing", listing_sql)):
        rows = (await session.execute(text(sql), {"min_sample": MIN_SAMPLE})).mappings().all()
        for row in rows:
            merged.setdefault((geo_level, row["geo_key"], "all"), {})[kind] = dict(row)


def _f(value) -> Optional[float]:
    return float(value) if value is not None else None


def _i(value) -> Optional[int]:
    return int(value) if value is not None else None


# --------------------------------------------------------------------------
# Lookup
# --------------------------------------------------------------------------

LOOKUP_SQL = text("""
    SELECT * FROM market_stats
     WHERE geo_level = :geo_level AND geo_key = :geo_key
       AND property_class = ANY(:classes)
     ORDER BY as_of_date DESC,
              CASE WHEN property_class = :preferred_class THEN 0 ELSE 1 END
     LIMIT 1
""")


async def resolve_market(
    session: AsyncSession,
    zip_code: Optional[str],
    city: Optional[str],
    county: Optional[str],
    state: Optional[str],
    property_type: Optional[str],
) -> Optional[dict]:
    """
    Find the most specific market baseline available for a property.

    Walks zip -> city -> county -> state and returns the first hit, preferring
    the property's own class over the 'all' cell at the same level. Returns
    None only when even the state cell is missing, which means market_stats
    has not been built.
    """
    preferred = classify_property(property_type)
    classes = [preferred, "all"]
    candidates = [
        ("zip", zip_code),
        ("city", f"{city.lower()},{state}" if city and state else None),
        ("county", f"{county.lower()},{state}" if county and state else None),
        ("state", state),
    ]
    for geo_level, geo_key in candidates:
        if not geo_key:
            continue
        row = (await session.execute(LOOKUP_SQL, {
            "geo_level": geo_level, "geo_key": geo_key,
            "classes": classes, "preferred_class": preferred,
        })).mappings().first()
        if row:
            return dict(row)
    return None


def classify_property(property_type: Optional[str]) -> str:
    """Map a source property type onto the coarse market_stats classes."""
    text_value = (property_type or "").lower()
    if "single family" in text_value:
        return "single_family"
    if "condo" in text_value or "co-op" in text_value:
        return "condo"
    if "townhouse" in text_value:
        return "townhouse"
    if "multi-family" in text_value or "multi family" in text_value:
        return "multi_family"
    return "other"
