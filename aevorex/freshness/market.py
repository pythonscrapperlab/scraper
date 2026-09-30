"""Configured city-market resolution for freshness commands."""

from __future__ import annotations

from dataclasses import dataclass

from aevorex.scrapers.constants import REDFIN


@dataclass(frozen=True)
class Market:
    """One Redfin city region and its stable product slug."""

    slug: str
    city: str
    state: str
    region_id: str


def resolve_market(slug: str) -> Market:
    """Resolve a product market slug against the checked-in Redfin region IDs."""
    normalized = slug.strip().lower()
    for state, cities in REDFIN.items():
        for city, region_id in cities.items():
            candidate = f"{city}-{state}".lower()
            if candidate == normalized:
                return Market(
                    slug=candidate,
                    city=city.replace("-", " "),
                    state=state,
                    region_id=str(region_id),
                )
    raise ValueError("Unknown configured market slug")


def market_slug(city: str, state: str) -> str:
    """Return the canonical city-state slug used by product and publisher layers."""
    words = "-".join(city.lower().replace("-", " ").split())
    return f"{words}-{state.lower()}"
