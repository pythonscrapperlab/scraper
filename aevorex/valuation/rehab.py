"""
Renovation cost estimation.

WHY THIS CHANGED
----------------
The previous estimate was a $/sqft rate bucketed by property age and nothing
else, with a flat multiplier if the remarks used distress language. That
treats a 1975 house that was fully remodelled last year identically to a 1975
house that has never been touched, which is the difference between a cosmetic
refresh and a gut.

The data supports much better. Available now, and previously unused:

- `Property Condition` — structured, 59.2% coverage after coalescing:
  fixer / updated / new / standard.
- `Year Renovated` — 85.8%. A recent renovation removes most of the scope that
  age alone would imply.
- `Roof` material and implied age — 71.4%. A roof replacement is a discrete
  five-figure line, not a smear across $/sqft.
- `Window Features` / `Storm Protection` — 42.6%. Impact glazing on a Florida
  property is $25-45k if it has to be added.
- `Construction Materials` — 94.1%.
- `ai_summary` and `description` — condition language on 98.2% of properties,
  mentioning roof (25.4%), HVAC (10.7%) and updates (33.3%).

STRUCTURE
---------
A base $/sqft for the general scope, plus discrete system adders for the
things that are replaced as whole units. That mirrors how a contractor
actually bids: so much per square foot for finishes, plus a line for the roof,
plus a line for the HVAC.

Three figures are returned — low, mid, high. A single point estimate for
something this uncertain conveys false precision, and the spread is itself
useful: a flip whose margin survives the high case is a materially better deal
than one that only works at the low case.

This is a heuristic, not a bid. Every result carries a flag saying so.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

HEURISTIC_FLAG = "rehab_cost_is_a_heuristic_estimate_not_a_contractor_bid"


@dataclass(frozen=True)
class RehabConfig:
    """Cost assumptions, all tunable per market."""

    # Base scope by condition class, $/sqft. These describe the general
    # finish-level work: paint, flooring, kitchen/bath refresh, fixtures.
    base_psf_by_condition: Dict[str, float] = field(default_factory=lambda: {
        "new": 0.0,
        "updated": 8.0,
        "standard": 22.0,
        "fixer": 55.0,
    })

    # Used when condition is unstated (40.8%), keyed on age instead — the old
    # behaviour, retained purely as the fallback it should always have been.
    base_psf_by_age: Tuple[Tuple[int, float], ...] = (
        (10, 6.0),
        (30, 18.0),
        (60, 28.0),
        (999, 40.0),
    )

    # Discrete system replacements, priced as whole units.
    roof_cost_per_sqft: float = 9.0          # tear-off and replace, FL shingle
    roof_min_cost: float = 9_000.0
    hvac_cost_per_ton: float = 3_200.0
    hvac_sqft_per_ton: float = 500.0
    impact_window_cost_per_sqft: float = 14.0  # of floor area, as a proxy
    impact_window_min: float = 18_000.0

    # A roof this old is assumed to need replacing within the hold period.
    roof_replace_age_shingle: int = 18
    roof_replace_age_premium: int = 40
    hvac_replace_age: int = 15

    # Uncertainty band around the mid estimate.
    low_multiplier: float = 0.70
    high_multiplier: float = 1.45

    # Contingency on top of identified scope. Standard practice; the things
    # you find after demolition are not in any listing description.
    contingency_pct: float = 0.12


DEFAULT_REHAB_CONFIG = RehabConfig()

# Language implying scope beyond what the structured fields say.
_HEAVY_SCOPE = re.compile(
    r"gut|tear.?down|total renovation|needs everything|down to the studs|"
    r"uninhabitable|fire damage|water damage|mold", re.I
)
_MODERATE_SCOPE = re.compile(
    r"needs? (work|tlc|updating|repair)|dated|original condition|handyman|"
    r"bring your (vision|contractor)|sold as.?is|fixer", re.I
)
_RECENTLY_DONE = re.compile(
    r"new roof|roof (is )?new|newly renovated|fully renovated|completely remodel|"
    r"brand new|just renovated|new hvac|new a/?c", re.I
)


def estimate_rehab(
    *,
    sqft: Optional[int],
    year_built: Optional[int],
    year_renovated: Optional[int] = None,
    condition_class: Optional[str] = None,
    roof_class: Optional[str] = None,
    has_impact_glazing: Optional[bool] = None,
    description: Optional[str] = None,
    ai_summary: Optional[str] = None,
    config: RehabConfig = DEFAULT_REHAB_CONFIG,
) -> Dict[str, Any]:
    """
    Estimate renovation cost to bring a property to the comp-set exit standard.

    Returns {"low","mid","high","basis","condition_class","flags"}.
    """
    flags = [HEURISTIC_FLAG]
    basis: Dict[str, Any] = {}

    if not sqft or sqft <= 0:
        return {
            "low": None, "mid": None, "high": None, "basis": {},
            "condition_class": condition_class,
            "flags": flags + ["no_sqft_cannot_estimate_rehab"],
        }

    text_blob = " ".join(filter(None, [description or "", ai_summary or ""]))

    # ---- resolve condition, preferring the structured field ----
    resolved = condition_class
    if _HEAVY_SCOPE.search(text_blob):
        # Explicit heavy-damage language overrides a bland "Resale" tag.
        resolved = "fixer"
        basis["condition_source"] = "text_heavy_scope"
    elif resolved:
        basis["condition_source"] = "structured_property_condition"
    elif _MODERATE_SCOPE.search(text_blob):
        resolved = "fixer"
        basis["condition_source"] = "text_moderate_scope"
    elif _RECENTLY_DONE.search(text_blob):
        resolved = "updated"
        basis["condition_source"] = "text_recently_renovated"

    # A renovation within the last decade caps how bad the condition can be,
    # regardless of build year.
    age = _age(year_built)
    renovation_age = _age(year_renovated)
    if renovation_age is not None and renovation_age <= 8 and resolved in (None, "standard", "fixer"):
        resolved = "updated"
        basis["condition_source"] = "recent_year_renovated"

    # ---- base scope ----
    if resolved and resolved in config.base_psf_by_condition:
        base_psf = config.base_psf_by_condition[resolved]
        basis["base_psf_basis"] = f"condition:{resolved}"
    else:
        base_psf = _psf_for_age(age, config)
        basis["base_psf_basis"] = f"age:{age if age is not None else 'unknown'}"
        flags.append("condition_unknown_rehab_estimated_from_age")

    base_cost = base_psf * sqft
    basis["base_psf"] = base_psf
    basis["base_cost"] = round(base_cost)

    # ---- discrete system adders ----
    adders: Dict[str, float] = {}

    if resolved != "new":
        roof_age = renovation_age if renovation_age is not None else age
        replace_at = (config.roof_replace_age_premium if roof_class == "premium"
                      else config.roof_replace_age_shingle)
        if roof_age is not None and roof_age >= replace_at and not _RECENTLY_DONE.search(text_blob):
            adders["roof_replacement"] = max(
                config.roof_min_cost, config.roof_cost_per_sqft * sqft
            )

        hvac_age = renovation_age if renovation_age is not None else age
        if hvac_age is not None and hvac_age >= config.hvac_replace_age:
            tons = max(1.5, sqft / config.hvac_sqft_per_ton)
            adders["hvac_replacement"] = tons * config.hvac_cost_per_ton

        # Impact glazing only counts as scope where it is known to be absent.
        # Unknown is left alone rather than assumed missing, which would add a
        # five-figure line to well over half the corpus on no evidence.
        if has_impact_glazing is False and (year_built or 0) < 2002:
            adders["impact_windows"] = max(
                config.impact_window_min, config.impact_window_cost_per_sqft * sqft
            )

    subtotal = base_cost + sum(adders.values())
    contingency = subtotal * config.contingency_pct
    mid = subtotal + contingency

    basis["adders"] = {k: round(v) for k, v in adders.items()}
    basis["contingency_pct"] = config.contingency_pct
    basis["contingency"] = round(contingency)

    return {
        "low": round(mid * config.low_multiplier),
        "mid": round(mid),
        "high": round(mid * config.high_multiplier),
        "basis": basis,
        "condition_class": resolved,
        "flags": flags,
    }


def _age(year: Optional[int]) -> Optional[int]:
    if not year or year < 1800:
        return None
    return max(0, datetime.now().year - year)


def _psf_for_age(age: Optional[int], config: RehabConfig) -> float:
    if age is None:
        # Mid-bracket rather than optimistic: an unknown-age property is more
        # likely to need work than not, given the corpus median build year is 1986.
        return config.base_psf_by_age[1][1]
    for max_age, psf in config.base_psf_by_age:
        if age <= max_age:
            return psf
    return config.base_psf_by_age[-1][1]
