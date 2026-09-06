"""
Annual ownership costs — taxes, HOA, CDD, maintenance, and the operating total.

WHY IT MATTERS THAT THIS IS COMPLETE
------------------------------------
A cap rate is only as honest as its expense line. The previous buy-and-hold
scorer assumed a flat $1,800 insurance figure and a 1%-of-price maintenance
reserve, and modelled nothing else. In Florida that understates real operating
cost badly enough to invert the ranking between two properties: a $400k condo
with a $700/month HOA and a $400k house without one are not the same deal, and
under a flat-expense model they score almost identically.

Everything here comes from real data where it exists:

- **Taxes** — `tax_annual` is populated on 94.6% of properties. Where missing,
  the market's median effective rate is applied (corpus median 1.13% of price,
  p90 1.68%), which is far better than a national guess.
- **HOA** — `hoa_monthly` on 59.5%, lifted by the amenity parser's frequency
  handling. Genuinely absent for most single-family, so a missing value is
  treated as zero for houses and flagged for condos, where it never is.
- **CDD** — the Florida Community Development District bond, on 27.8% of
  properties via `CDD Y/N`. An annual assessment on top of taxes and HOA,
  typically $1,000-3,000, and routinely missed in underwriting. Nobody else
  models this.
- **Insurance** — modelled; see `insurance.py`.
- **Maintenance, vacancy, management** — genuine assumptions, stated as such.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class CarryingCostConfig:
    """Ownership-cost assumptions, all tunable."""

    # Fallback effective tax rate when `tax_annual` is missing and no market
    # rate resolves. Corpus median is 1.13%.
    default_tax_rate: float = 0.0113

    # Annual maintenance reserve as a share of property value. 1% is the
    # conventional rule; older stock realistically runs higher, so age scales it.
    maintenance_pct_base: float = 0.010
    maintenance_pct_old_building: float = 0.015
    old_building_age: int = 40

    # Estimated annual CDD assessment where the flag is set but no amount is
    # published — which is every case, as the amount is never in the feed.
    default_cdd_annual: float = 1_800.0

    # Share of gross rent lost to vacancy and turnover. 8% is roughly one
    # month a year, which is normal for Florida long-term letting.
    vacancy_rate: float = 0.08
    # Third-party management. Long-term letting runs 8-10%; the nightly and
    # mid-term strategies override this with their own much higher figures.
    management_pct: float = 0.09


DEFAULT_CARRYING_CONFIG = CarryingCostConfig()


def estimate_carrying_costs(
    *,
    price: Optional[int],
    market_value: Optional[float],
    sqft: Optional[int],
    year_built: Optional[int],
    property_type: Optional[str],
    tax_annual: Optional[float],
    hoa_monthly: Optional[float],
    has_cdd: Optional[bool],
    annual_insurance: Optional[float],
    market: Optional[dict] = None,
    config: CarryingCostConfig = DEFAULT_CARRYING_CONFIG,
) -> Dict[str, Any]:
    """
    Annual ownership costs excluding debt service and excluding rent-linked
    costs (vacancy and management), which scale with income and belong to the
    strategy that assumes them.

    Returns the individual lines plus `operating_expenses`.
    """
    flags: list = []
    basis: Dict[str, Any] = {}
    value = market_value or price or 0

    # ---- property tax ----
    if tax_annual and tax_annual > 0:
        taxes = float(tax_annual)
        basis["tax_source"] = "actual"
    elif value:
        rate = config.default_tax_rate
        if market and market.get("median_tax_rate"):
            candidate = float(market["median_tax_rate"])
            # Guard: a market cell built off bad data could carry a silly rate.
            if 0.002 < candidate < 0.04:
                rate = candidate
                basis["tax_source"] = "market_median_rate"
            else:
                basis["tax_source"] = "default_rate"
        else:
            basis["tax_source"] = "default_rate"
        taxes = value * rate
        basis["tax_rate_used"] = round(rate, 5)
        flags.append("property_tax_estimated_not_actual")
    else:
        taxes = None
        flags.append("property_tax_not_estimable")

    # ---- HOA ----
    type_text = (property_type or "").lower()
    is_attached = any(t in type_text for t in ("condo", "co-op", "townhouse"))
    if hoa_monthly and hoa_monthly > 0:
        hoa = float(hoa_monthly) * 12
        basis["hoa_source"] = "actual"
    elif is_attached:
        # A condo without a stated HOA fee is missing data, not a condo with
        # no fee. Scoring it as zero would make it look materially cheaper to
        # own than its neighbours.
        hoa = None
        flags.append("attached_property_missing_hoa_fee")
    else:
        hoa = 0.0
        basis["hoa_source"] = "assumed_none_detached"

    # ---- CDD ----
    if has_cdd:
        cdd = config.default_cdd_annual
        flags.append("cdd_amount_estimated_flag_only_in_source")
    elif has_cdd is False:
        cdd = 0.0
    else:
        cdd = 0.0
        # Only ~28% of listings state this either way, so absence is unknown
        # rather than confirmed-none.
        flags.append("cdd_status_unknown")

    # ---- maintenance reserve ----
    if value:
        from datetime import datetime
        age = (datetime.now().year - year_built) if year_built and year_built > 1800 else None
        pct = (config.maintenance_pct_old_building
               if age is not None and age >= config.old_building_age
               else config.maintenance_pct_base)
        maintenance = value * pct
        basis["maintenance_pct"] = pct
    else:
        maintenance = None

    lines = [taxes, annual_insurance, hoa, cdd, maintenance]
    if any(line is None for line in (taxes, annual_insurance, maintenance)):
        # The three that always apply. HOA being unknown is flagged above but
        # does not block a total, since it is genuinely zero for most houses.
        operating = None
        flags.append("operating_expenses_incomplete")
    else:
        operating = sum(line for line in lines if line is not None)

    return {
        "annual_taxes": _r(taxes),
        "annual_insurance": _r(annual_insurance),
        "annual_hoa": _r(hoa),
        "annual_cdd": _r(cdd),
        "annual_maintenance": _r(maintenance),
        "operating_expenses": _r(operating),
        "basis": basis,
        "flags": flags,
    }


def compute_noi(
    *,
    monthly_rent: Optional[float],
    operating_expenses: Optional[float],
    vacancy_rate: float,
    management_pct: float,
) -> Optional[float]:
    """
    Net operating income: gross rent less vacancy, management and operating
    costs. Excludes debt service by definition, which is what makes it
    comparable across differently-financed deals.
    """
    if monthly_rent is None or operating_expenses is None:
        return None
    gross = monthly_rent * 12
    effective_gross = gross * (1 - vacancy_rate)
    management = effective_gross * management_pct
    return effective_gross - management - operating_expenses


def _r(value: Optional[float]) -> Optional[float]:
    return round(float(value), 2) if value is not None else None
