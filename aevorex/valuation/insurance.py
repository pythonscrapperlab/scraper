"""
Modelled annual property insurance — the Florida-specific line item.

WHY THIS IS MODELLED AND NOT SOURCED
------------------------------------
There is no insurance field anywhere in the schema, and no listing feed
publishes one. Leaving it out is not neutral: in Florida it is routinely the
largest operating expense after the mortgage, frequently exceeding property
tax, and it is the single most common reason a deal that pencils on paper
fails in reality. A cash-flow model for a Florida rental that omits insurance
is not conservative, it is wrong.

So this produces an explicit estimate with its reasoning attached, rather than
a silent zero. Every output carries an `insurance_basis` dict naming each
multiplier applied, so a user can see precisely why a number is what it is and
override it with a real quote.

THE MODEL
---------
A base rate per $1,000 of replacement cost, multiplied by risk factors that
Florida carriers actually price on:

- **Construction class.** Masonry (block/CBS/concrete) versus wood frame is
  the biggest single wind-premium driver, and post-2022 carrier withdrawals it
  also affects whether cover is available at all in some counties. Available
  on 94.1% of properties once amenity keys are coalesced.
- **Roof.** Age and material. Carriers increasingly refuse or heavily surcharge
  shingle roofs past roughly 15 years; tile, metal and concrete are
  underwritten far more favourably and last 30-50 years. Roof class is known
  for 71.4% of the corpus.
- **Wind mitigation.** Impact glazing earns a documented credit that commonly
  runs 20-40% of the wind portion. Shutters earn a smaller one.
- **Building code era.** The 2002 Florida Building Code was a step change in
  wind standards, and carriers price pre- and post-2002 construction very
  differently. Only 31% of this corpus is post-2002; the median year built
  is 1986.
- **Flood.** Priced separately from wind in reality. `flood_factor` has real
  spread here — 3,752 properties at the minimum, 860 at 9-10 — so a flood
  loading is applied on top rather than folded in.
- **Condo.** A condo owner insures the interior only; the association's master
  policy covers the structure and arrives through the HOA fee instead, so
  charging a full structural premium would double-count.

CALIBRATION AND ITS LIMITS
--------------------------
Rates are set from published Florida market averages for 2025-26 and are
configurable. They are a *modelled estimate*, never a quote, and the flag
`insurance_is_modelled_not_quoted` is attached to every property so that never
gets lost downstream. Actual premiums vary by carrier, county, claims history
and elevation certificate — none of which is in this dataset.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# Annual premium per $1,000 of insured replacement cost, before any factor.
# Florida statewide average sits far above the national figure; this is the
# baseline for masonry, post-code, standard roof, no flood loading.
BASE_RATE_PER_1000 = 7.5

# Replacement cost is construction cost, not market value — land does not
# burn. Using market value would badly overstate premiums in expensive
# markets where land is most of the price.
REPLACEMENT_COST_PER_SQFT = 185.0

# Floor and ceiling on the final figure, to keep a freak input from producing
# a nonsense operating expense.
MIN_ANNUAL_PREMIUM = 600.0
MAX_ANNUAL_PREMIUM = 60_000.0

# The Florida Building Code revision that changed wind standards.
FLORIDA_CODE_YEAR = 2002
# Carriers commonly non-renew shingle roofs around this age.
SHINGLE_SCRUTINY_AGE = 15


@dataclass(frozen=True)
class InsuranceConfig:
    """Every rate and multiplier, exposed for per-market tuning."""

    base_rate_per_1000: float = BASE_RATE_PER_1000
    replacement_cost_per_sqft: float = REPLACEMENT_COST_PER_SQFT

    # Construction: masonry is the reference at 1.0.
    construction_multipliers: Dict[str, float] = field(default_factory=lambda: {
        "masonry": 1.0,
        "mixed": 1.20,
        "frame": 1.45,
    })

    # Roof material class.
    roof_multipliers: Dict[str, float] = field(default_factory=lambda: {
        "premium": 0.90,   # tile / metal / concrete / slate
        "standard": 1.0,   # shingle / composition
        "flat": 1.30,      # short life, ponding, often excluded
    })
    # Applied on top when a shingle roof is past the age carriers scrutinise.
    aged_roof_multiplier: float = 1.35

    # Wind mitigation credits.
    impact_glazing_multiplier: float = 0.75
    storm_shutter_multiplier: float = 0.90

    # Pre-2002 construction, absent evidence of a code-compliant retrofit.
    pre_code_multiplier: float = 1.25

    # Flood, keyed on the source's 1-10 factor. Applied as an additive
    # loading on the base premium because flood is a separate policy in
    # reality, not a wind-policy rating factor.
    flood_loading: Dict[int, float] = field(default_factory=lambda: {
        1: 0.0, 2: 0.02, 3: 0.05, 4: 0.09, 5: 0.14,
        6: 0.20, 7: 0.28, 8: 0.38, 9: 0.52, 10: 0.70,
    })

    # A condo owner insures interior/contents only; the master policy is paid
    # for through the HOA fee and would otherwise be counted twice.
    condo_multiplier: float = 0.35
    # Non-owner-occupied policies (DP-3) price above owner-occupied (HO-3).
    landlord_multiplier: float = 1.15

    # Applied when the property is old enough to need a roof but its age is
    # unknown — an explicit uncertainty loading rather than silent optimism.
    unknown_roof_multiplier: float = 1.10


DEFAULT_INSURANCE_CONFIG = InsuranceConfig()

MODELLED_FLAG = "insurance_is_modelled_not_quoted"


def estimate_annual_insurance(
    *,
    sqft: Optional[int],
    year_built: Optional[int],
    year_renovated: Optional[int] = None,
    property_type: Optional[str] = None,
    construction_class: Optional[str] = None,
    roof_class: Optional[str] = None,
    has_impact_glazing: Optional[bool] = None,
    has_storm_shutters: Optional[bool] = None,
    flood_factor: Optional[int] = None,
    market_value: Optional[float] = None,
    for_rental: bool = True,
    config: InsuranceConfig = DEFAULT_INSURANCE_CONFIG,
) -> Dict[str, Any]:
    """
    Estimate the annual premium and show the working.

    Returns {"annual": float|None, "basis": {...}, "flags": [...]}.
    `basis` names every multiplier applied so the number can be audited or
    argued with, which matters because it is an estimate standing in for a
    quote nobody has obtained.
    """
    basis: Dict[str, Any] = {}
    flags = [MODELLED_FLAG]

    # ---- insured value: replacement cost, not market price ----
    if sqft and sqft > 0:
        replacement_cost = sqft * config.replacement_cost_per_sqft
        basis["replacement_cost_source"] = "sqft_x_rate"
    elif market_value and market_value > 0:
        # Without an area, fall back to a share of market value. Crude, and
        # flagged, but better than declining to price insurance at all in a
        # state where it decides the deal.
        replacement_cost = market_value * 0.55
        basis["replacement_cost_source"] = "market_value_proxy"
        flags.append("insurance_replacement_cost_estimated_from_price")
    else:
        return {"annual": None, "basis": {}, "flags": flags + ["insurance_not_estimable"]}

    basis["replacement_cost"] = round(replacement_cost)
    premium = (replacement_cost / 1000.0) * config.base_rate_per_1000
    basis["base_rate_per_1000"] = config.base_rate_per_1000

    multipliers: Dict[str, float] = {}

    # ---- construction ----
    if construction_class:
        factor = config.construction_multipliers.get(construction_class)
        if factor:
            multipliers["construction_" + construction_class] = factor
    else:
        flags.append("construction_class_unknown")

    # ---- roof material and age ----
    if roof_class:
        multipliers["roof_" + roof_class] = config.roof_multipliers.get(roof_class, 1.0)
    else:
        flags.append("roof_class_unknown")

    roof_age = _roof_age(year_built, year_renovated)
    if roof_age is None:
        multipliers["roof_age_unknown"] = config.unknown_roof_multiplier
    elif roof_class in (None, "standard", "flat") and roof_age >= SHINGLE_SCRUTINY_AGE:
        # The commercially important case: an ageing shingle roof is the most
        # common reason a Florida policy is declined or non-renewed.
        multipliers["roof_past_scrutiny_age"] = config.aged_roof_multiplier
        flags.append("roof_likely_past_insurer_scrutiny_age")

    # ---- wind mitigation ----
    if has_impact_glazing:
        multipliers["impact_glazing_credit"] = config.impact_glazing_multiplier
    elif has_storm_shutters:
        multipliers["storm_shutter_credit"] = config.storm_shutter_multiplier

    # ---- building code era ----
    effective_year = max(year_built or 0, year_renovated or 0)
    if effective_year and effective_year < FLORIDA_CODE_YEAR:
        multipliers["pre_2002_building_code"] = config.pre_code_multiplier
    elif not effective_year:
        flags.append("year_built_unknown")

    # ---- occupancy / structure type ----
    type_text = (property_type or "").lower()
    if "condo" in type_text or "co-op" in type_text:
        multipliers["condo_interior_only"] = config.condo_multiplier
    if for_rental:
        multipliers["landlord_policy"] = config.landlord_multiplier

    for factor in multipliers.values():
        premium *= factor
    basis["multipliers"] = {k: round(v, 3) for k, v in multipliers.items()}

    # ---- flood, as an additive loading ----
    if flood_factor is not None:
        loading = config.flood_loading.get(int(flood_factor), 0.0)
        if loading:
            basis["flood_loading"] = loading
            basis["flood_factor"] = int(flood_factor)
            premium *= (1.0 + loading)
            if flood_factor >= 8:
                flags.append("high_flood_risk_separate_policy_likely_required")
    else:
        flags.append("flood_factor_unknown")

    premium = min(max(premium, MIN_ANNUAL_PREMIUM), MAX_ANNUAL_PREMIUM)
    basis["annual"] = round(premium)

    return {"annual": round(premium, 2), "basis": basis, "flags": flags}


def _roof_age(year_built: Optional[int], year_renovated: Optional[int]) -> Optional[int]:
    """
    Best available proxy for roof age.

    There is no roof-age field. A renovation year is the closest signal — a
    renovation usually includes or postdates the roof — falling back to the
    build year. Deliberately a proxy, and the caller flags it as such.
    """
    from datetime import datetime

    reference = max(filter(None, [year_built, year_renovated]), default=None)
    if not reference or reference < 1800:
        return None
    return max(0, datetime.now().year - reference)
