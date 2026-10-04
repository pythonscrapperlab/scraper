"""Strictly redacted public demo-snapshot construction."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import Any, cast

from aevorex.db.models import MarketFreshness, Property
from aevorex.publisher.types import MarketDefinition
from aevorex.scoring.recompose import recompose


def format_address(address: str) -> str:
    """Format the stored street and unit without inventing missing address data."""
    address = re.sub(r"\b(?:unit|apt|suite)\s+", "#", address, flags=re.I)
    address = re.sub(r"\b(st|ave|dr|rd|blvd|ter|ct|ln|way|pl)\s+(\d+[a-z]?)$",
                     r"\1 #\2", address, flags=re.I)
    words = address.split()
    uppercase = {"n", "s", "e", "w", "ne", "nw", "se", "sw", "us"}
    formatted = " ".join(
        word.upper() if word.lower() in uppercase or word.startswith("#") else word.title()
        for word in words
    )
    return re.sub(r"(\d)(St|Nd|Rd|Th)\b", lambda m: m[1] + m[2].lower(), formatted)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _headline(item: Property, lens: str, factors: dict[str, Any]) -> dict[str, Any]:
    valuation = item.valuation
    if lens == "motivated_seller":
        return {"label": "Total price cut", "value": _number(factors.get("cumulative_price_cut_pct")),
                "unit": "percent", "estimated": False}
    if lens == "fix_flip":
        mao = _number(factors.get("max_allowable_offer"))
        arv = _number(factors.get("arv"))
        if arv is None and valuation is not None:
            arv = _number(valuation.arv)
        return {"label": "Estimated ARV", "value": arv, "unit": "usd", "estimated": True,
                "gap_to_max_offer": round(mao - item.price) if mao is not None else None}
    if lens == "buy_hold":
        # The lens underwrites all-in cost, not the generic valuation cap rate.
        return {"label": "Estimated all-in cap rate", "value": _number(factors.get("cap_rate_all_in_pct")),
                "unit": "percent", "estimated": True}
    if lens == "str":
        return {"label": "Estimated in-season monthly rent",
                "value": _number(factors.get("in_season_monthly_rent")),
                "unit": "usd_per_month", "estimated": True}
    return {"label": "Estimated nightly revenue (proxy)",
            "value": _number(factors.get("proxy_nightly_rate")),
            "unit": "usd_per_night", "estimated": True}


def _reasons(item: Property, lens: str, factors: dict[str, Any], city: str) -> list[str]:
    """Public copy uses only allowlisted numeric facts, never free-text rationale."""
    reasons: list[str] = []
    cuts = _number(factors.get("price_reduction_count"))
    if cuts and cuts > 0:
        reasons.append(f"{cuts:g} price {'cut' if cuts == 1 else 'cuts'} in {item.days_on_market} days listed")
    ratio = _number(factors.get("dom_vs_market_median"))
    if ratio and ratio > 1:
        reasons.append(f"Listed {item.days_on_market} days, {ratio:g}× the local market median")
    discount = _number(factors.get("pct_below_estimated_value"))
    if discount and discount > 0:
        reasons.append(f"{discount:g}% below estimated value")
    if lens == "motivated_seller":
        for field, text in (
            ("is_reo", "Listing marked REO"),
            ("is_short_sale", "Listing marked short sale"),
            ("is_probate_or_estate", "Estate or probate wording in listing"),
            ("is_foreclosure", "Foreclosure wording in listing"),
        ):
            if getattr(item, field, False):
                reasons.append(text)
    elif lens == "fix_flip":
        gap = _number(factors.get("offer_vs_asking_pct"))
        if gap and gap > 0:
            reasons.append(f"{gap:g}% below estimated max offer")
        profit = _number(factors.get("projected_profit"))
        if profit and profit > 0:
            reasons.append(f"Estimated profit ${profit:,.0f} after modelled costs")
        roi = _number(factors.get("roi_pct"))
        if roi and roi > 0:
            reasons.append(f"Estimated {roi:g}% levered return")
        if not reasons:
            # High relative rank does not imply profitable economics. Keep the
            # signed underwriting gap visible rather than inventing upside.
            gap = _number(factors.get("offer_vs_asking_pct"))
            if gap is not None:
                reasons.append(f"Asking price is {abs(gap):g}% {'below' if gap >= 0 else 'above'} estimated max offer")
    elif lens == "buy_hold":
        cap = _number(factors.get("cap_rate_all_in_pct"))
        if cap and cap > 0:
            reasons.append(f"Estimated {cap:g}% all-in cap rate")
        yield_pct = _number(factors.get("gross_yield_pct"))
        if yield_pct and yield_pct > 0:
            reasons.append(f"Estimated {yield_pct:g}% gross rental yield")
    elif lens == "str":
        uplift = _number(factors.get("in_season_multiplier"))
        if uplift and uplift > 1:
            reasons.append(f"Estimated seasonal rent is {uplift:g}× the long-term baseline")
    elif lens == "airbnb":
        rate = _number(factors.get("proxy_nightly_rate"))
        if rate and rate > 0:
            reasons.append(f"Estimated nightly revenue proxy ${rate:,.0f}")
    return reasons[:3]


def _price_band(price: int | None) -> dict[str, int] | None:
    if price is None:
        return None
    width = 50_000
    low = (price // width) * width
    return {"low": low, "high": low + width - 1}


def build_demo_snapshot(
    market: MarketDefinition,
    lens: str,
    properties: Sequence[Property],
    freshness: MarketFreshness,
) -> dict[str, Any]:
    """Build exactly three full rows and five anonymous stubs for one lens."""
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
            -float(getattr(value[1], f"{lens}_percentile")),
            -float(getattr(value[1], f"{lens}_score")),
            str(value[0].id),
        )
    )
    pool_size = len(ranked)
    eligible = [
        (rank, item, analysis, breakdown)
        for rank, (item, analysis, breakdown) in enumerate(ranked, 1)
        if item.price is not None and item.price > 0
        and item.days_on_market is not None and item.days_on_market >= 0
        and any(image.url and image.url.startswith(("https://", "http://"))
                for image in item.property_images)
        and _headline(item, lens, getattr(analysis, f"{lens}_factors") or {})["value"] is not None
    ]
    if len(eligible) < 8:
        raise ValueError("DemoSnapshotNeedsEightScoredRows")

    full: list[dict[str, Any]] = []
    for rank, item, analysis, breakdown in eligible[:3]:
        percentile = float(getattr(analysis, f"{lens}_percentile"))
        if percentile < 80:
            raise ValueError("DemoSnapshotNeedsThreeTopOrStrongRows")
        factors = getattr(analysis, f"{lens}_factors") or {}
        components = breakdown.get("components", [])
        labels = [
            str(component.get("label"))
            for component in components[:2]
            if isinstance(component, dict) and component.get("label")
        ]
        full.append({
            "address": format_address(str(item.address)),
            "photo_url": next(image.url for image in sorted(
                item.property_images, key=lambda image: (image.sort_order or 0, image.url)
            ) if image.url and image.url.startswith(("https://", "http://"))),
            "rank": rank,
            "pool_size": pool_size,
            "headline": _headline(item, lens, factors),
            "reasons": _reasons(item, lens, factors, market.city),
            "facts": {
                "price": item.price,
                "beds": item.bedrooms,
                "baths": item.bathrooms,
                "sqft": item.sqft,
                "dom": item.days_on_market,
            },
            "score": getattr(analysis, f"{lens}_score"),
            "grade": getattr(analysis, f"{lens}_grade"),
            "percentile": percentile,
            "tier": "top" if percentile >= 95 else "strong" if percentile >= 80 else "rest",
            "component_labels": labels,
        })

    stubs: list[dict[str, Any]] = []
    for _, item, _, _ in eligible[3:8]:
        stubs.append({
            "price_band": _price_band(cast(int | None, item.price)),
            "dom": item.days_on_market,
        })

    return {
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
