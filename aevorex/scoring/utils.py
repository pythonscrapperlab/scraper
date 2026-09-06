"""
Shared pure-function helpers for the scorers.

Nothing here touches a database session — everything operates on the
already-loaded ScoringContext, which is what keeps the scorers themselves
easy to unit test without a live DB (see tests/scoring/).
"""

from datetime import datetime, timezone
from statistics import median
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from aevorex.db.event_types import (
    LISTED,
    OFF_MARKET_TYPES,
    REDUCED,
    RELISTED,
    RENTAL_EVENT_TYPES,
    canonical_event_type,
)
from aevorex.db.models import PriceHistory, Property, PropertyComp, TaxHistory

# ------------------------------------------------------------------
# Property.meta helpers
# ------------------------------------------------------------------


def meta_get(property_obj: Property, key: str, default: Any = None) -> Any:
    meta = property_obj.meta or {}
    value = meta.get(key)
    return value if value is not None else default


def meta_float(property_obj: Property, key: str) -> Optional[float]:
    value = meta_get(property_obj, key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------
# Basic valuation helpers
# ------------------------------------------------------------------


def price_per_sqft(price: Optional[float], sqft: Optional[float]) -> Optional[float]:
    if not price or not sqft:
        return None
    return price / sqft


def property_age(year_built: Optional[int]) -> Optional[int]:
    if not year_built:
        return None
    return max(datetime.now(timezone.utc).year - year_built, 0)


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def step_score(value: float, breakpoints: Sequence[Tuple[float, float]]) -> float:
    """
    Piecewise step function: breakpoints is an ascending list of
    (threshold, score). Returns the score of the highest threshold <=
    value, or the lowest breakpoint's score if value is below all of them.
    """
    ordered = sorted(breakpoints, key=lambda pair: pair[0])
    result = ordered[0][1]
    for threshold, score in ordered:
        if value >= threshold:
            result = score
        else:
            break
    return result


def weighted_sum(components: Iterable[Tuple[Optional[float], float]]) -> Optional[float]:
    """
    Weighted average of (score, weight) pairs, renormalized over whichever
    components actually have a score. Returns None if every component is None
    (nothing to score from).
    """
    total_weight = 0.0
    total_score = 0.0
    for score, weight in components:
        if score is None:
            continue
        total_weight += weight
        total_score += score * weight
    if total_weight == 0:
        return None
    return total_score / total_weight


# ------------------------------------------------------------------
# price_history helpers
# ------------------------------------------------------------------


def event_kind(event: PriceHistory) -> Optional[str]:
    """
    Canonical type of a price-history row.

    Prefer the stored `event_type` slug; fall back to canonicalizing the raw
    `event` wording for rows written before that column existed. The scorers
    used to compare `event` directly against lowercase slugs ("relisted",
    "reduced"), which never matched anything, because Redfin writes
    "Relisted" and "Price Changed" — the comparison was silently False on
    every row in the database rather than merely rare.
    """
    return getattr(event, "event_type", None) or canonical_event_type(event.event)


def is_rental_event(event: PriceHistory) -> bool:
    """
    Whether this row belongs to the property's rental history, not its sale
    history.

    Reads the stored `is_rental_event` flag, falling back to the event type
    for rows written before that column existed. The fallback can only catch
    explicitly rental-typed events ("Listed for Rent", "Rental Removed") — a
    directionless "Price Changed" inside a rental cycle is indistinguishable
    without walking the whole timeline, which is the normalizer's job.
    """
    stored = getattr(event, "is_rental_event", None)
    if stored is not None:
        return bool(stored)
    return event_kind(event) in RENTAL_EVENT_TYPES


def sale_events(price_history: List[PriceHistory]) -> List[PriceHistory]:
    """
    The sale-side subset of a property's history.

    Every function below that does arithmetic on price goes through this
    first. Redfin files rental events in the same timeline, where prices are
    monthly rents (~$4,855 average) sitting alongside sale prices (~$674,901
    average). Mixing them makes "original list price" occasionally a rent
    figure, which then makes the cumulative price-drop percentage ~99% and
    turns an ordinary listing into a top motivated-seller lead.
    """
    return [e for e in price_history if not is_rental_event(e)]


def original_list_price(price_history: List[PriceHistory]) -> Optional[int]:
    """
    Earliest listing event's price, falling back to the earliest priced event.

    Unpriced events (Listing Removed, Pending, Relisted, ...) are skipped:
    they're real history, but they can't stand in for an original list price.
    Rental events are excluded outright — see sale_events().
    """
    events = sale_events(price_history)
    if not events:
        return None
    listed = [e for e in events if event_kind(e) == LISTED and e.price is not None]
    if listed:
        return listed[0].price
    priced = [e for e in events if e.price is not None]
    return priced[0].price if priced else None


def count_price_reductions(price_history: List[PriceHistory]) -> int:
    """Sale-side price cuts only — a rent reduction is not a seller cutting their asking price."""
    return sum(1 for e in sale_events(price_history) if event_kind(e) == REDUCED)


def has_relisted_event(price_history: List[PriceHistory]) -> bool:
    return any(event_kind(e) == RELISTED for e in sale_events(price_history))


def has_off_market_event(price_history: List[PriceHistory]) -> bool:
    """
    Whether this listing has ever been pulled from the market (removed or
    delisted) — i.e. an owner who wanted to sell and didn't. Not yet consumed
    by a scorer; exposed here because the underlying events only started
    reaching the table with the ingestion fixes, so no scoring weight has
    been calibrated against them yet.
    """
    return any(event_kind(e) in OFF_MARKET_TYPES for e in sale_events(price_history))


def price_drop_pct(current_price: Optional[int], original_price: Optional[int]) -> Optional[float]:
    """Fraction (not percent) the current price has dropped from the original list price."""
    if not current_price or not original_price or original_price <= 0:
        return None
    if current_price >= original_price:
        return 0.0
    return (original_price - current_price) / original_price


# ------------------------------------------------------------------
# tax_history helpers
# ------------------------------------------------------------------


def latest_tax_row(tax_history: List[TaxHistory]) -> Optional[TaxHistory]:
    if not tax_history:
        return None
    return max(tax_history, key=lambda t: t.tax_year)


def discount_vs_reference_pct(price: Optional[float], reference_value: Optional[float]) -> Optional[float]:
    """
    Fraction `price` sits below `reference_value` (negative = price is above
    the reference). Shared math for "below assessed value" (tax_history) and
    "below AVM" (motivated-seller) — same shape, different reference number.
    """
    if not price or not reference_value or reference_value <= 0:
        return None
    return (reference_value - price) / reference_value


# ------------------------------------------------------------------
# comps helpers
# ------------------------------------------------------------------


def comp_median_ppsf(comps: List[PropertyComp]) -> Optional[float]:
    ppsf_values = [
        c.price / c.sqft for c in comps if c.price and c.sqft
    ]
    if not ppsf_values:
        return None
    return median(ppsf_values)


# ------------------------------------------------------------------
# rental / yield helpers
# ------------------------------------------------------------------


def rental_estimate_monthly(property_obj: Property) -> Optional[float]:
    """Redfin's long-term rental estimate — mid preferred, else avg(low, high)."""
    mid = meta_float(property_obj, "rental_est_mid")
    if mid:
        return mid
    low = meta_float(property_obj, "rental_est_low")
    high = meta_float(property_obj, "rental_est_high")
    if low and high:
        return (low + high) / 2
    return low or high


def gross_rental_yield_pct(annual_rent: Optional[float], price: Optional[int]) -> Optional[float]:
    if not annual_rent or not price:
        return None
    return annual_rent / price


# ------------------------------------------------------------------
# text scanning
# ------------------------------------------------------------------


def scan_keywords(text: Optional[str], keywords: Sequence[str]) -> List[str]:
    """Case-insensitive substring match; returns the list of keywords found."""
    if not text:
        return []
    lowered = text.lower()
    return [kw for kw in keywords if kw in lowered]


# ------------------------------------------------------------------
# location_scores helpers
# ------------------------------------------------------------------


def location_score_avg(location_score, fields: Sequence[str]) -> Optional[float]:
    if location_score is None:
        return None
    values = [getattr(location_score, f) for f in fields]
    values = [v for v in values if v is not None]
    if not values:
        return None
    return sum(values) / len(values)
