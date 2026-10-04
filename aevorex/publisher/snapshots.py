"""Strictly redacted public demo-snapshot construction."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, cast

from aevorex.db.models import MarketFreshness, Property
from aevorex.publisher.reasons import (
    MIN_NUMERIC_REASONS,
    build_reasons,
    build_summary,
    headline,
    number,
)
from aevorex.publisher.types import MarketDefinition
from aevorex.scoring.recompose import recompose

SNAPSHOT_VERSION = 3
OPEN_COUNT = 3
TEASER_COUNT = 5
PHOTO_COUNT = 5
MIN_PHOTOS = 3
TOP_TIER_PERCENTILE = 90
STRONG_TIER_PERCENTILE = 70

_SUFFIXES = (
    "st|street|ave|avenue|dr|drive|rd|road|blvd|boulevard|ter|terrace|ct|court|ln|lane|way|"
    "pl|place|cir|circle|pkwy|parkway|trl|trail|loop|plz|sq|path|walk|pt|cv|xing"
)
# "State Rd 7" / "County Rd 2" end in a route number, not a unit.
_ROUTE_PREFIXES = {"state", "county", "us", "sr", "cr", "highway", "route"}
_BARE_UNIT = re.compile(rf"\b({_SUFFIXES})\s+((?:[a-z]\d+|\d+[a-z]?))$", re.IGNORECASE)
# Not homes: cards show beds, baths and a renovation or rental case, which land and timeshares lack.
_NOT_SHOWCASE_TYPES = {"vacant land", "timeshare"}
_PROPERTY_TYPES = {
    "single family residential": "Single-family",
    "condo/co-op": "Condo",
    "townhouse": "Townhouse",
    "multi-family (2-4 unit)": "Multi-family (2–4 units)",
    "multi-family (5+ unit)": "Multi-family (5+ units)",
    "vacant land": "Vacant land",
    "mobile/manufactured home": "Mobile home",
}


def format_address(address: str) -> str:
    """Format the stored street and unit without inventing missing address data."""
    address = re.sub(r"\b(?:unit|apt|suite|ste)\s+", "#", address, flags=re.I)

    def add_hash(match: re.Match[str]) -> str:
        before = address[: match.start()].split()
        if before and before[-1].lower() in _ROUTE_PREFIXES:
            return match.group(0)
        return f"{match.group(1)} #{match.group(2)}"

    address = _BARE_UNIT.sub(add_hash, address)
    words = address.split()
    uppercase = {"n", "s", "e", "w", "ne", "nw", "se", "sw", "us"}
    formatted = " ".join(
        word.upper() if word.lower() in uppercase or word.startswith("#") else word.title()
        for word in words
    )
    formatted = re.sub(r"(\d)(St|Nd|Rd|Th)\b", lambda m: m[1] + m[2].lower(), formatted)
    return re.sub(r"\bMc([a-z])", lambda m: "Mc" + m[1].upper(), formatted)


def full_address(item: Property) -> str | None:
    """Street (with unit), city, state and zip, or None when any part is unusable.

    Redfin hides some street addresses ("undisclosed address"); those listings also tend to be
    merged records of several homes, so they are never shown.
    """
    street = format_address(str(item.address or "").strip())
    city = str(item.city or "").strip().title()
    state = str(item.state or "").strip().upper()
    zip_code = str(item.zip_code or "").strip()
    # A real street number: "00 Cinnabar Hills Rd" is an unaddressed parcel, not a home.
    if not re.match(r"^(?!0+[A-Za-z]?\s)\d+[A-Za-z]?\s+\S", street) or "undisclosed" in street.lower():
        return None
    if not city or len(state) != 2 or not re.fullmatch(r"\d{5}(-\d{4})?", zip_code):
        return None
    return f"{street}, {city}, {state} {zip_code}"


def photo_urls(item: Property) -> list[str]:
    """The first five usable photos, in listing order, without repeats."""
    ordered = sorted(item.property_images, key=lambda image: (image.sort_order or 0, image.url or ""))
    urls = [image.url for image in ordered if image.url and image.url.startswith(("https://", "http://"))]
    return list(dict.fromkeys(urls))[:PHOTO_COUNT]


def _whole(value: Any) -> int | float | None:
    value = number(value)
    if value is None:
        return None
    return int(value) if value == int(value) else value


def _price_band(price: int | None) -> dict[str, int] | None:
    if price is None:
        return None
    width = 50_000
    low = (price // width) * width
    return {"low": low, "high": low + width - 1}


def _tier(percentile: float) -> str:
    if percentile >= TOP_TIER_PERCENTILE:
        return "top"
    return "strong" if percentile >= STRONG_TIER_PERCENTILE else "rest"


def _open_property(
    rank: int, pool_size: int, item: Property, analysis: Any, breakdown: dict[str, Any],
    lens: str, address: str, photos: list[str], head: dict[str, Any], reasons: list[str],
) -> dict[str, Any]:
    rationale = getattr(analysis, f"{lens}_rationale") or breakdown.get("rationale")
    raw_type = str(item.property_type or "").strip()
    return {
        "address": address,
        "photos": photos,
        "price": item.price,
        "beds": item.bedrooms,
        "baths": _whole(item.bathrooms),
        "sqft": item.sqft,
        "days_listed": item.days_on_market,
        "property_type": _PROPERTY_TYPES.get(raw_type.lower(), raw_type or None),
        "year_built": item.year_built,
        "rank": rank,
        "pool_size": pool_size,
        "tier": _tier(float(getattr(analysis, f"{lens}_percentile"))),
        "headline": head,
        "reasons": reasons,
        "summary": build_summary(rationale, str(item.state or "").upper()),
    }


def build_demo_snapshot(
    market: MarketDefinition,
    lens: str,
    properties: Sequence[Property],
    freshness: MarketFreshness,
) -> dict[str, Any]:
    """Build three open properties and five locked teasers for one lens.

    The pool is every active, scored listing in the city that passes the recomposition check,
    ordered by this lens's score. Open properties and teasers are the highest-ranked members
    of that pool that are also presentable (real price, days listed, a full street address and
    at least three photos, three numeric reasons), so ``rank`` is an honest position in the
    whole city pool.
    """
    ranked: list[tuple[Property, Any, dict[str, Any]]] = []
    for item in properties:
        if item.delisted_at is not None or item.listing_status_normalized not in {"active", "new"}:
            continue
        analysis = item.analysis
        if analysis is None:
            continue
        score = getattr(analysis, f"{lens}_score")
        percentile = getattr(analysis, f"{lens}_percentile")
        breakdown = getattr(analysis, f"{lens}_breakdown")
        if score is None or percentile is None or not isinstance(breakdown, dict):
            continue
        try:
            if abs(recompose(breakdown) - float(score)) > 0.01:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        ranked.append((item, analysis, breakdown))
    ranked.sort(
        key=lambda value: (
            -float(getattr(value[1], f"{lens}_score")),
            -float(getattr(value[1], f"{lens}_percentile")),
            str(value[0].id),
        )
    )
    pool_size = len(ranked)

    presentable: list[
        tuple[int, Property, Any, dict[str, Any], str, list[str], dict[str, Any], list[str]]
    ] = []
    for rank, (item, analysis, breakdown) in enumerate(ranked, 1):
        if not item.price or item.price <= 0 or item.price_is_placeholder:
            continue
        if item.days_on_market is None or item.days_on_market < 0:
            continue
        if str(item.property_type or "").strip().lower() in _NOT_SHOWCASE_TYPES:
            continue
        address, photos = full_address(item), photo_urls(item)
        if address is None or len(photos) < MIN_PHOTOS:
            continue
        factors = getattr(analysis, f"{lens}_factors") or {}
        head = headline(item, lens, factors)
        if head is None:
            continue
        # A card needs real, favourable, numeric evidence behind it. A high rank without three
        # such statements (e.g. a flip whose asking price is above our maximum offer) is not shown.
        reasons = build_reasons(item, lens, factors, breakdown)
        if sum(any(ch.isdigit() for ch in text) for text in reasons) < MIN_NUMERIC_REASONS:
            continue
        presentable.append((rank, item, analysis, breakdown, address, photos, head, reasons))
        if len(presentable) == OPEN_COUNT + TEASER_COUNT:
            break
    if len(presentable) < OPEN_COUNT + TEASER_COUNT:
        raise ValueError("DemoSnapshotNeedsEightScoredRows")

    full = [
        _open_property(rank, pool_size, item, analysis, breakdown, lens, address, photos, head, reasons)
        for rank, item, analysis, breakdown, address, photos, head, reasons in presentable[:OPEN_COUNT]
    ]
    if any(row["tier"] == "rest" for row in full):
        raise ValueError("DemoSnapshotNeedsThreeTopOrStrongRows")
    stubs = [
        {"price_band": _price_band(cast(int | None, item.price)), "days_listed": item.days_on_market}
        for _, item, *_ in presentable[OPEN_COUNT:]
    ]

    return {
        "version": SNAPSHOT_VERSION,
        "market": {"slug": market.slug, "city": market.city, "state": market.state},
        "lens": lens,
        "full": full,
        "stubs": stubs,
        "freshness": {
            "last_checked_at": freshness.last_checked_at.isoformat()
            if freshness.last_checked_at else None,
            "next_check_at": freshness.next_check_at.isoformat()
            if freshness.next_check_at else None,
            "last_refreshed_at": freshness.last_refreshed_at.isoformat()
            if freshness.last_refreshed_at else None,
            "check_status": freshness.check_status,
        },
    }
