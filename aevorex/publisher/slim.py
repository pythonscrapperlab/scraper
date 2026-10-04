"""What the web renders, and nothing more: curated features, capped child lists, slim breakdowns.

Everything here is a pure function of local rows so it can be tested without a database. The
numbers (25 features, 15 events / 7 years, 5 tax years, 6 comps, 600 characters) are the owner's
size budget for the Supabase free plan (E6 step 5); ``docs/schema.md`` documents them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

MAX_HISTORY_EVENTS = 15
MAX_HISTORY_YEARS = 7
MAX_TAX_YEARS = 5
MAX_COMPS = 6
MAX_DESCRIPTION_CHARS = 600
FEATURE_VALUE_CHARS = 160

# Structured columns of local ``property_features`` that are published (13).
FEATURE_COLUMNS: tuple[str, ...] = (
    "heating", "cooling", "flooring", "construction_material", "roof", "foundation",
    "interior_features", "appliances", "laundry_features", "water_source", "sewer",
    "utilities", "furnished",
)
# Published name -> first non-empty source key in the raw amenities blob (12).
FEATURE_RAW: dict[str, tuple[str, ...]] = {
    "style": ("Style",),
    "levels": ("Levels",),
    "exterior_features": ("Exterior Features",),
    "parking_features": ("Parking Features",),
    "garage_spaces": ("Garage Spaces", "# of Garage Spaces"),
    "pool": ("Pool Features", "POOL_PRIVATE_YN"),
    "fireplace": ("FIREPLACE_YN",),
    "waterfront": ("Waterfront Features",),
    "pets_allowed": ("Pets Allowed",),
    "hoa_includes": ("Association Fee Includes",),
    "hoa_amenities": ("Association Amenities",),
    "property_condition": ("Property Condition",),
}
FEATURE_NAMES: tuple[str, ...] = (*FEATURE_COLUMNS, *FEATURE_RAW)
assert len(FEATURE_NAMES) <= 25  # the contract; raising the cap is an owner decision

_EMPTY = {"", "none", "n/a", "na", "unknown", "null", "-", "0"}


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if text.casefold() in _EMPTY:
        return None
    if len(text) > FEATURE_VALUE_CHARS:
        text = text[: FEATURE_VALUE_CHARS - 1].rstrip(" ,;") + "…"
    return text


def curated_features(columns: Mapping[str, Any], raw_amenities: Mapping[str, Any] | None) -> dict[str, str]:
    """The allowlisted features that have a value; never any other key of the raw blob."""
    raw = raw_amenities or {}
    features: dict[str, str] = {}
    for name in FEATURE_COLUMNS:
        value = _clean(columns.get(name))
        if value is not None:
            features[name] = value
    for name, keys in FEATURE_RAW.items():
        for key in keys:
            value = _clean(raw.get(key))
            if value is not None:
                features[name] = value
                break
    return features


def recent_history(
    events: Iterable[Any], now: datetime
) -> list[Any]:
    """Sale/listing events (never rentals): newest first, within 7 years, at most 15."""
    cutoff = now.replace(tzinfo=None) - timedelta(days=365 * MAX_HISTORY_YEARS)
    kept = [
        event for event in events
        if not event.is_rental_event
        and event.event_date is not None
        and event.event_date.replace(tzinfo=None) >= cutoff
    ]
    kept.sort(key=lambda event: (event.event_date, str(event.id)), reverse=True)
    return kept[:MAX_HISTORY_EVENTS]


def recent_tax_years(rows: Iterable[Any]) -> list[Any]:
    """One row per tax year, the latest five years."""
    by_year = {row.tax_year: row for row in rows if row.tax_year is not None}
    return [by_year[year] for year in sorted(by_year, reverse=True)[:MAX_TAX_YEARS]]


def latest_comps(comps: Iterable[Any]) -> list[Any]:
    """At most six sold comps, most recent sale first (undated last)."""
    dated = sorted(
        comps,
        key=lambda comp: (comp.sold_date is not None, comp.sold_date or datetime.min, str(comp.id)),
        reverse=True,
    )
    return dated[:MAX_COMPS]


def slim_breakdown(breakdown: Mapping[str, Any], keep_fields: frozenset[str]) -> dict[str, Any]:
    """Drop null-valued drivers and every driver ``field`` the web has no column for.

    Components, weights, subscores and adjustments are untouched, so ``recompose`` gives the
    same score before and after. ``field`` is kept only when it names something the web already
    holds for the property (a ``serving.properties``/``valuation`` column or a flag), so it can
    link a driver to the fact it explains.
    """
    slim: dict[str, Any] = {key: value for key, value in breakdown.items()}
    components = breakdown.get("components")
    if isinstance(components, Sequence) and not isinstance(components, str):
        slimmed: list[Any] = []
        for component in components:
            if not isinstance(component, Mapping):
                slimmed.append(component)
                continue
            item = dict(component)
            drivers = component.get("drivers")
            if isinstance(drivers, Sequence) and not isinstance(drivers, str):
                kept: list[dict[str, Any]] = []
                for driver in drivers:
                    if not isinstance(driver, Mapping) or driver.get("value") is None:
                        continue
                    row = dict(driver)
                    if row.get("field") not in keep_fields:
                        row.pop("field", None)
                    kept.append(row)
                item["drivers"] = kept
            slimmed.append(item)
        slim["components"] = slimmed
    return slim
