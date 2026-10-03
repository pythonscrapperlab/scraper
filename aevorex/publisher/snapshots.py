"""Strictly redacted public demo-snapshot construction."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from aevorex.db.models import MarketFreshness, Property
from aevorex.publisher.types import MarketDefinition
from aevorex.scoring.recompose import recompose


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
    if len(ranked) < 8:
        raise ValueError("DemoSnapshotNeedsEightScoredRows")

    full: list[dict[str, Any]] = []
    for item, analysis, breakdown in ranked[:3]:
        percentile = float(getattr(analysis, f"{lens}_percentile"))
        components = breakdown.get("components", [])
        labels = [
            str(component.get("label"))
            for component in components[:2]
            if isinstance(component, dict) and component.get("label")
        ]
        full.append({
            "address": item.address,
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
    for item, analysis, _ in ranked[3:8]:
        percentile = float(getattr(analysis, f"{lens}_percentile"))
        stubs.append({
            "tier": "top" if percentile >= 95 else "strong" if percentile >= 80 else "rest",
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
