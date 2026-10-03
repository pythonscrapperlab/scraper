"""Publisher market configuration, timezone resolution, and demo flags."""

from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from aevorex.freshness.market import resolve_market
from aevorex.publisher.types import MarketDefinition

ROOT = Path(__file__).resolve().parents[2]
CITY_DATA = ROOT / "uscities.csv"
DEMO_CONFIG = ROOT / "config" / "demo_markets.yaml"


@lru_cache(maxsize=1)
def demo_market_slugs() -> frozenset[str]:
    """Load the owner-approved demo markets from checked-in configuration."""
    if not DEMO_CONFIG.exists():
        return frozenset()
    raw: Any = yaml.safe_load(DEMO_CONFIG.read_text(encoding="utf-8")) or {}
    values = raw.get("markets", []) if isinstance(raw, dict) else []
    return frozenset(str(value).strip().lower() for value in values)


@lru_cache(maxsize=64)
def market_definition(slug: str) -> MarketDefinition:
    """Resolve a configured Redfin market and fail closed on timezone ambiguity."""
    market = resolve_market(slug)
    matches: set[str] = set()
    with CITY_DATA.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if (
                row.get("city_ascii", "").casefold() == market.city.casefold()
                and row.get("state_id", "").upper() == market.state
            ):
                timezone = row.get("timezone", "").strip()
                if timezone:
                    matches.add(timezone)
    if len(matches) != 1:
        raise ValueError("Configured market has ambiguous or missing timezone")
    return MarketDefinition(
        slug=market.slug,
        city=market.city,
        state=market.state,
        region_id=market.region_id,
        timezone=next(iter(matches)),
        is_demo=market.slug in demo_market_slugs(),
    )
