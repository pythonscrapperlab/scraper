"""
Typed parsers over the MLS amenity blob (`property_features.raw_amenities`).

WHY
---
The amenity dict is the richest thing in the database and the least usable:
706 distinct keys across the corpus, all free-text strings, spelled
differently by every MLS feed. Nothing downstream can filter on it, and every
consumer that tried ended up doing its own ad-hoc substring match.

This module turns the ~20 keys that carry real investment meaning into typed
values with explicit `None` for "not stated". Fill rates below are measured
against the live corpus (7,329 properties), so a caller knows what coverage
to expect before designing around a field.

FLORIDA CONTEXT
---------------
Several of these are not generic property attributes — they are the specific
inputs that decide whether a Florida deal works at all:

- **Construction class** (60.3%). Masonry (block/CBS/concrete) versus wood
  frame is the single biggest driver of Florida wind premiums, and after the
  2022-23 carrier withdrawals it also decides whether a property is insurable
  at any price in some counties.
- **Roof material** (64.6%). Shingle carries a ~15-20 year life and insurers
  increasingly refuse or surcharge roofs past ~15 years; tile, metal and
  concrete are underwritten far more favourably.
- **Impact glazing** (39.1% windows + 2.7% explicit storm protection).
  Impact windows earn a wind-mitigation credit that can be 20-40% of premium,
  and their absence on a pre-2002 building is a five-figure rehab line.
- **CDD** (27.8%). A Community Development District bond is a Florida-specific
  annual assessment on top of taxes and HOA. It is routinely missed in
  underwriting and can be $1,000-3,000/yr.
- **Lease restrictions** (26.4%) and **HOA approval** (20.7%). The structured
  form of the question every rental strategy depends on, and far more reliable
  than mining the listing remarks for it.
- **Private versus community pool.** `Pool Features` says "Association" on 708
  properties — a shared amenity, not a private pool. Conflating the two would
  badly overstate short-term-rental appeal, where a private pool is a primary
  booking driver.
"""

import re
from typing import Any, Dict, Optional

# --- condition -------------------------------------------------------------
# "Resale" (2,002) is the overwhelming default and means nothing on its own.
# Only the deviations from it carry signal.
_CONDITION_RULES = (
    ("fixer", r"fixer|handyman|tear.?down|needs? work"),
    ("new", r"new construction|under construction|construction complete|completed"),
    ("updated", r"updated|remodel|renovat"),
    ("standard", r"resale"),
)

# --- roof ------------------------------------------------------------------
# Ordered: the first match wins, so the longer-lived material in a combined
# value like "Concrete, Tile" is what gets recorded.
_ROOF_RULES = (
    ("concrete", r"concrete"),
    ("tile", r"\btile|clay|spanish"),
    ("metal", r"metal|aluminum|steel|copper"),
    ("slate", r"slate"),
    ("flat", r"\bflat\b|built.?up|rolled|membrane|tar|gravel|tpo|modified"),
    ("shingle", r"shingle|composition|asphalt|architectural"),
    ("wood", r"wood|shake"),
)

# Insurance/longevity class. `premium` roofs are underwritten favourably in
# Florida and last 30-50 years; `standard` (shingle) is the 15-20 year
# workhorse insurers now scrutinise hardest; `flat` is the problem category —
# short life, ponding, and frequently excluded or surcharged.
_ROOF_CLASS = {
    "concrete": "premium", "tile": "premium", "metal": "premium", "slate": "premium",
    "shingle": "standard", "wood": "standard",
    "flat": "flat",
}

# --- construction ----------------------------------------------------------
# Masonry values seen live: "Block, Stucco" 597, "Block" 561, "CBS" 477,
# "Stucco" 254, "Stucco, CBS" 193, "Block, Concrete, Stucco" 113.
# Frame: 501. A value naming both is treated as mixed, not masonry — the
# frame portion is what the wind rating is limited by.
_MASONRY = r"\bcbs\b|block|concrete|masonry|stucco|brick|icf"
_FRAME = r"\bframe\b|wood\s*frame|siding|vinyl"

# --- furnished -------------------------------------------------------------
# Ordered most- to least-furnished; "Turnkey" is the strongest signal for any
# rental strategy because it means the unit can be let without capex.
_FURNISHED_RULES = (
    ("turnkey", r"turn.?key"),
    ("partial", r"partial|some furnish"),
    ("negotiable", r"negotiab|option"),
    ("furnished", r"^furnished|^yes\b|\bfully furnish"),
    ("unfurnished", r"unfurnish|^no\b"),
)

_FURNISHED_RANK = {"turnkey": 1.0, "furnished": 0.85, "partial": 0.5,
                   "negotiable": 0.35, "unfurnished": 0.0}

# Keys carrying an MLS yes/no flag, in preference order per concept. MLS feeds
# express booleans as "1"/"0", "Yes"/"No", or a restated label
# ("Has Private Pool"), so all three shapes have to parse.
_TRUE_TOKENS = ("1", "yes", "y", "true", "has ", "on waterfront", "required")
_FALSE_TOKENS = ("0", "no", "n", "false", "none", "not ")


def _first(amenities: Dict[str, Any], *keys: str) -> Optional[str]:
    """First non-empty value among `keys`, as a stripped string."""
    for key in keys:
        value = amenities.get(key)
        if value is None:
            continue
        text = str(value).strip()
        # "—" is Redfin's em-dash placeholder for an absent value.
        if text and text not in ("—", "--", "N/A", "n/a", "Unknown"):
            return text
    return None


def _flag(amenities: Dict[str, Any], *keys: str) -> Optional[bool]:
    """
    Parse an MLS yes/no flag, or None when no key is populated.

    None is deliberately distinct from False: "this listing did not state
    whether pets are allowed" and "pets are not allowed" are different facts,
    and a scorer must be able to tell them apart rather than penalising a
    silent listing.
    """
    raw = _first(amenities, *keys)
    if raw is None:
        return None
    lowered = raw.lower()
    # Check false first: "No Restrictions" starts with "no" but the leading
    # token is what the flag means, and true-tokens like "has " would not
    # match it anyway.
    for token in _FALSE_TOKENS:
        if lowered.startswith(token):
            return False
    for token in _TRUE_TOKENS:
        if token in lowered:
            return True
    return None


def _match(rules, text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    lowered = text.lower()
    for label, pattern in rules:
        if re.search(pattern, lowered):
            return label
    return None


def _to_float(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", value)
    if not cleaned or cleaned in ("-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


# Fee frequency -> annual multiplier. Verified against the live corpus:
# Monthly 2,076 / Annually 981 / Quarterly 380 / Semi-Annually 48.
_FEE_FREQUENCY = {
    "monthly": 12, "annually": 1, "annual": 1, "yearly": 1,
    "quarterly": 4, "semi-annually": 2, "semiannually": 2, "semi annual": 2,
    "weekly": 52, "biweekly": 26, "one time": 0, "voluntary": 0,
}

# Above this, a "monthly" HOA is not credible and is almost certainly an
# annual figure mislabelled by the feed, or plain bad data. The live corpus
# has exactly one such row ($215,567/mo against a $1.325M condo); genuine
# ultra-luxury Miami condos reach ~$26k/mo, so this leaves ample headroom.
MAX_CREDIBLE_HOA_MONTHLY = 50_000


def parse_amenities(amenities: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Extract typed investment-relevant fields from the MLS amenity blob.

    Every value is None when the source did not state it. Returns a flat dict
    so it can be merged straight into a normalized property payload.
    """
    if not isinstance(amenities, dict) or not amenities:
        return {key: None for key in AMENITY_FIELDS}

    out: Dict[str, Any] = {}

    # ---- condition (35.8%) ----
    out["condition_class"] = _match(
        _CONDITION_RULES, _first(amenities, "Property Condition", "PROPERTY_CONDITION",
                                 "Year Built Details")
    )

    # ---- roof (64.6%) ----
    roof_raw = _first(amenities, "Roof", "Roof Details", "Roof Type")
    out["roof_material"] = _match(_ROOF_RULES, roof_raw)
    out["roof_class"] = _ROOF_CLASS.get(out["roof_material"] or "")

    # ---- construction (60.3%) ----
    construction_raw = _first(amenities, "Construction Materials", "Construction Details",
                              "Construction", "Exterior Construction")
    if construction_raw:
        lowered = construction_raw.lower()
        has_masonry = bool(re.search(_MASONRY, lowered))
        has_frame = bool(re.search(_FRAME, lowered))
        # Mixed counts as frame-limited for wind rating purposes.
        out["construction_class"] = (
            "mixed" if has_masonry and has_frame
            else "masonry" if has_masonry
            else "frame" if has_frame
            else None
        )
    else:
        out["construction_class"] = None

    # ---- wind mitigation (39.1% windows / 2.7% storm protection) ----
    glazing = " ".join(filter(None, [
        _first(amenities, "Window Features", "Windows", "Window Treatments") or "",
        _first(amenities, "Storm Protection", "Storm Protection Features") or "",
    ])).lower()
    if glazing.strip():
        out["has_impact_glazing"] = bool(re.search(r"impact|hurricane", glazing))
        out["has_storm_shutters"] = bool(re.search(r"shutter|storm (window|panel)|accordion", glazing))
    else:
        out["has_impact_glazing"] = None
        out["has_storm_shutters"] = None

    out["foundation"] = _first(amenities, "Foundation Details", "Foundation")
    out["is_new_construction"] = _flag(amenities, "New Construction", "NEW_CONSTRUCTION_YN")

    # ---- rental strategy gates ----
    out["furnished_level"] = _match(
        _FURNISHED_RULES,
        _first(amenities, "Furnished", "FURNISHED", "Furnished Description",
               "Furnished Information"),
    )
    out["furnished_rank"] = _FURNISHED_RANK.get(out["furnished_level"] or "")
    out["lease_restricted"] = _flag(amenities, "Lease Restrictions YN", "Lease Restrictions")
    out["hoa_approval_required"] = _flag(
        amenities, "Association Approval Required Y/N", "Association Approval Required"
    )
    out["pets_allowed"] = _flag(amenities, "Pets Allowed", "PetsAllowedYN", "Pets Allowed Details")
    out["min_lease_months"] = _parse_min_lease(
        _first(amenities, "Minimum Lease Term", "Minimum Lease", "Minimum Lease Term Source")
    )
    out["is_land_lease"] = _flag(amenities, "Land Lease YN", "Land Lease")

    # ---- amenities that drive nightly-rental demand ----
    # "Association"/"Community" in Pool Features means a shared pool, not a
    # private one — 708 live properties say exactly that. Only a private pool
    # is a booking driver, so the two are kept apart.
    pool_raw = (_first(amenities, "Pool Features", "Pool", "Private Pool") or "").lower()
    private_flag = _flag(amenities, "POOL_PRIVATE_YN", "Private Pool YN", "POOL_YN")
    if pool_raw or private_flag is not None:
        community_only = bool(re.search(r"association|community|shared", pool_raw))
        explicit_private = bool(re.search(r"in ground|inground|private|gunite|screen enclosure|heated", pool_raw))
        out["has_private_pool"] = bool(private_flag) if private_flag is not None else (
            explicit_private and not community_only
        )
        out["has_community_pool"] = community_only
    else:
        out["has_private_pool"] = None
        out["has_community_pool"] = None

    out["has_spa"] = _flag(amenities, "SPA_YN", "Spa YN") if _first(
        amenities, "SPA_YN", "Spa YN", "Spa Features") else (
        True if _first(amenities, "Spa Features") else None)
    out["is_waterfront"] = _flag(amenities, "Waterfront YN", "WATERFRONT_YN", "Water Access Y/N")
    out["has_water_view"] = _flag(amenities, "Water View Y/N", "VIEW_YN")
    out["waterfront_feet"] = _to_float(_first(amenities, "Waterfront Feet Total"))
    out["garage_spaces"] = _to_float(
        _first(amenities, "Garage Spaces", "# of Garage Spaces", "Covered Spaces")
    )

    # ---- carrying costs ----
    out["hoa_monthly_amenity"] = _parse_hoa_monthly(amenities)
    out["has_cdd"] = _flag(amenities, "CDD Y/N", "CDD")
    out["assessed_value_amenity"] = _to_float(_first(amenities, "Tax Assessed Value"))
    out["association_amenities"] = _first(amenities, "Association Amenities")

    # ---- family-rental signal ----
    out["elementary_school"] = _first(amenities, "Elementary School")
    out["high_school"] = _first(amenities, "High School")

    return out


def _parse_hoa_monthly(amenities: Dict[str, Any]) -> Optional[float]:
    """
    Monthly HOA cost, normalising whatever frequency the feed used.

    Prefers a field that is already monthly; otherwise converts
    `Association Fee` using `Association Fee Frequency`. Verified against the
    corpus: frequency handling in the existing pipeline is already correct, so
    this exists to *raise coverage* (59.5% today) using the monthly-specific
    keys, not to fix a conversion bug.
    """
    direct = _to_float(_first(
        amenities, "Monthly HOA Amount", "Total Monthly Fees",
        "Association & Fees: HOA Amt (Monthly)", "Monthly Condo Fee Amount",
    ))
    if direct is not None and 0 <= direct <= MAX_CREDIBLE_HOA_MONTHLY:
        return direct

    fee = _to_float(_first(amenities, "Association Fee", "Condo Fees", "Association Fee 3"))
    if fee is None:
        return None
    frequency_raw = (_first(amenities, "Association Fee Frequency",
                            "Association Fee Payment Frequency", "Condo Fees Term") or "").lower()
    per_year = next((v for k, v in _FEE_FREQUENCY.items() if k in frequency_raw), None)
    if per_year is None:
        # Frequency unstated on ~8.7% of fee-bearing rows. Guessing monthly
        # would inflate an annual fee 12x, which is worse than declining to
        # answer, so this returns None and lets the caller fall back.
        return None
    if per_year == 0:
        return 0.0
    monthly = fee * per_year / 12
    return monthly if 0 <= monthly <= MAX_CREDIBLE_HOA_MONTHLY else None


def _parse_min_lease(value: Optional[str]) -> Optional[int]:
    """
    Minimum lease term in months.

    Only ~0.1% of listings state this in structured form, which is precisely
    why the Airbnb regulatory gate cannot be resolved per-property from
    listing data alone. Parsed anyway: where it IS present it is decisive,
    because a minimum of 1 month or more rules out nightly letting.
    """
    if not value:
        return None
    lowered = value.lower()
    match = re.search(r"(\d+)\s*(day|week|month|year)", lowered)
    if not match:
        return None
    count, unit = int(match.group(1)), match.group(2)
    if unit == "day":
        return max(1, round(count / 30))
    if unit == "week":
        return max(1, round(count / 4.3))
    if unit == "year":
        return count * 12
    return count


# Declared so a caller (and the None-path above) knows the full key set.
AMENITY_FIELDS = (
    "condition_class", "roof_material", "roof_class", "construction_class",
    "has_impact_glazing", "has_storm_shutters", "foundation", "is_new_construction",
    "furnished_level", "furnished_rank", "lease_restricted", "hoa_approval_required",
    "pets_allowed", "min_lease_months", "is_land_lease",
    "has_private_pool", "has_community_pool", "has_spa", "is_waterfront",
    "has_water_view", "waterfront_feet", "garage_spaces",
    "hoa_monthly_amenity", "has_cdd", "assessed_value_amenity", "association_amenities",
    "elementary_school", "high_school",
)
