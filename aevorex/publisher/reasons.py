"""Public-facing headline, reasons and summary for demo snapshots.

Everything here is derived from numbers the scorers and the valuation already
computed. Nothing is read from listing free text except a short allowlist of
distress/urgency phrases the normalizer matched, and those are re-validated
character by character before they can appear. Reasons are phrased as plain
sentences with the real figure in them, strongest first, and a reason is only
emitted when the fact it states is actually favourable and true: an asking
price that sits *above* our maximum offer is never dressed up as a bargain.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, cast

from aevorex.db.models import Property

MINUS = "−"
MAX_REASONS = 5
MIN_NUMERIC_REASONS = 3
# A year-over-year price change above this is a data artefact, not something to put in front of
# an investor (the live data holds values like +154%).
MAX_PLAUSIBLE_YOY_PCT = 20.0

# Anything that smells like contact data is dropped rather than shipped.
_FORBIDDEN = re.compile(
    r"@|https?:|www\.|\b(?:agent|broker|brokerage|phone|e-?mail)\b"
    r"|\d{3}[\s.\-)]+\d{3}[\s.\-]+\d{4}",
    re.IGNORECASE,
)
_SAFE_PHRASE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9' \-/&]{1,38}[A-Za-z0-9]$")

_DISTRESS_LABELS = {
    "reo": "bank-owned (REO)",
    "probate_or_estate": "a probate or estate sale",
    "foreclosure": "in foreclosure",
    "short_sale": "a short sale",
    "auction": "headed to auction",
}
# Property columns whose matched phrases explain the distress component, in priority order.
_DISTRESS_FIELDS = (
    "is_reo", "is_probate_or_estate", "is_foreclosure", "is_short_sale", "is_auction", "is_as_is",
)
_RENT_METHODS = {
    "source_avm": "a published estimate",
    "zip_bed_model": "modelled from local rental listings",
    "market_yield_prior": "modelled from market rent yields",
}
_LOCATION_NAMES = {
    "quiet_score": "quiet", "wellness_score": "wellness", "groceries_score": "groceries",
    "restaurants_score": "restaurants", "parks_score": "parks", "pedestrian_score": "walkability",
    "nightlife_score": "nightlife", "vibrant_score": "vibrancy", "cafes_score": "cafes",
    "shopping_score": "shopping",
}


@dataclass(frozen=True)
class Reason:
    text: str
    strength: float


def number(value: Any) -> float | None:
    """A finite float, or None for missing, boolean or non-finite values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def usd(value: float) -> str:
    return f"${round(value):,}"


def plain(value: float, digits: int = 1) -> str:
    """Trim trailing zeros after the point: 5.0 -> '5', 5.9 -> '5.9', 60 -> '60'."""
    text = f"{value:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def signed(value: float, digits: int = 1) -> str:
    """Like plain(), with a typographic minus for negatives (a loss-making cap rate)."""
    text = plain(abs(value), digits)
    return f"{MINUS}{text}" if value < 0 and text != "0" else text


def percent(value: float) -> str:
    """One decimal below 10 (5.9%), whole numbers above (11%)."""
    return f"{plain(value, 1 if abs(value) < 10 else 0)}%"


def _a(text: str) -> str:
    """Prefix 'a' or 'an' by how the figure is read aloud (an 8%, an 11%, a 110%)."""
    return f"an {text}" if re.match(r"8|1[18](?!\d)", text.lstrip("$")) else f"a {text}"


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def _times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def _contributions(breakdown: dict[str, Any]) -> dict[str, float]:
    """Weight x sub-score per available component: how much each one moved the score."""
    out: dict[str, float] = {}
    for component in breakdown.get("components", []):
        if not isinstance(component, dict) or component.get("available") is False:
            continue
        weight, sub = number(component.get("weight")), number(component.get("subscore"))
        if weight is not None and sub is not None and component.get("key"):
            out[str(component["key"])] = weight * sub
    return out


def _quote(phrases: list[str]) -> str:
    quoted = [f"'{phrase}'" for phrase in phrases]
    return quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + f" and {quoted[-1]}"


def _safe_phrases(raw: Any, limit: int) -> list[str]:
    out: list[str] = []
    for phrase in raw if isinstance(raw, list) else []:
        text = str(phrase).strip().lower()
        if _SAFE_PHRASE.match(text) and not _FORBIDDEN.search(text) and text not in out:
            out.append(text)
    return out[:limit]


# ------------------------------------------------------------------ headlines


def headline(item: Property, lens: str, factors: dict[str, Any]) -> dict[str, Any] | None:
    """The one number a visitor sees first. None means it cannot be computed honestly."""
    valuation = item.valuation
    if lens == "motivated_seller":
        cut = number(factors.get("cumulative_price_cut_pct"))
        count = number(factors.get("price_reduction_count")) or 0
        if cut and cut > 0:
            return {"label": "Total price cut", "value": f"{MINUS}{percent(cut)}", "estimate": False}
        if count > 0:
            return {"label": "Price cuts", "value": f"{int(count)}", "estimate": False}
        return {"label": "Price cuts", "value": "None yet", "estimate": False}
    if lens == "fix_flip":
        arv = number(factors.get("arv")) or (number(valuation.arv) if valuation else None)
        if not arv or not item.price:
            return None
        mao = number(factors.get("max_allowable_offer")) or (
            number(valuation.max_allowable_offer) if valuation else None)
        out: dict[str, Any] = {"label": "Estimated ARV", "value": usd(arv), "estimate": True}
        if mao:
            gap = round(mao - cast(int, item.price))
            out["gap_to_max_offer"] = gap
            out["detail"] = (
                f"Asking is {usd(gap)} under our max offer of {usd(mao)}" if gap >= 0
                else f"Asking is {usd(-gap)} over our max offer of {usd(mao)}"
            )
        return out
    if lens == "buy_hold":
        cap = number(factors.get("cap_rate_all_in_pct"))
        return None if cap is None else {
            "label": "Cap rate (all-in)", "value": f"{signed(cap)}%", "estimate": True}
    if lens == "str":
        rent = number(factors.get("in_season_monthly_rent"))
        return None if not rent else {
            "label": "Estimated monthly rent (in season)", "value": f"{usd(rent)}/mo", "estimate": True}
    rate = number(factors.get("proxy_nightly_rate"))
    return None if not rate else {
        "label": "Estimated nightly revenue", "value": f"{usd(rate)}/night", "estimate": True}


# -------------------------------------------------------------------- reasons


def _motivated(item: Property, f: dict[str, Any], c: dict[str, float]) -> list[Reason]:
    out: list[Reason] = []
    price = cast(int | None, item.price)
    dom = cast(int | None, item.days_on_market)
    valuation = item.valuation

    flagged = [k for k in (f.get("distress_signals") or []) if isinstance(k, str)]
    matched: dict[str, Any] = item.distress_signals if isinstance(item.distress_signals, dict) else {}
    phrases: list[str] = []
    if flagged:
        for field in _DISTRESS_FIELDS:
            phrases += _safe_phrases(matched.get(field), 2)
    phrases = list(dict.fromkeys(phrases))[:3]
    if phrases:
        out.append(Reason(f"Listing says {_quote(phrases)}", c.get("distress", 0)))
    elif flagged and flagged[0] in _DISTRESS_LABELS:
        out.append(Reason(f"Listed as {_DISTRESS_LABELS[flagged[0]]}", c.get("distress", 0)))

    count = int(number(f.get("price_reduction_count")) or 0)
    cut, original = number(f.get("cumulative_price_cut_pct")), number(f.get("original_list_price"))
    if count > 0:
        since = number(f.get("days_since_last_price_cut"))
        # Days-on-market can be reset by a relist, so "in N days" is only said when the latest
        # cut demonstrably falls inside it.
        within = f" in {_days(dom)} on market" if dom and (since is None or since <= dom) else ""
        text = f"Price cut {_times(count)}{within}"
        if cut and cut > 0 and original:
            text += f", now {percent(cut)} below the original ask of {usd(original)}"
        out.append(Reason(text, c.get("price_cuts", 0)))
        if since is not None and since <= 45:
            days = int(since)
            when = "today" if days == 0 else f"{_days(days)} ago"
            out.append(Reason(f"Latest price cut came {when}, so the seller is still adjusting",
                              c.get("price_cuts", 0) * 0.85))
    agreements = int(number(f.get("listing_agreements_in_window")) or 0)
    history: str | None = None
    if f.get("previously_withdrawn"):
        history = "Previously withdrawn from the market without selling"
    elif f.get("relisted"):
        history = "Relisted after coming off the market"
    if agreements >= 2:
        history = (f"{history}, now on its {_ordinal(agreements)} listing agreement" if history
                   else f"Now on its {_ordinal(agreements)} listing agreement")
    if history:
        out.append(Reason(history, c.get("price_cuts", 0) * 0.7))

    ratio, median = number(f.get("dom_vs_market_median")), number(f.get("market_median_dom"))
    if dom is not None and ratio and median and ratio >= 1.2:
        out.append(Reason(
            f"Listed {_days(dom)}, {plain(ratio, 1)}× the local median of {_days(round(median))}",
            c.get("days_on_market", 0)))
    elif dom is not None and dom >= 60:
        out.append(Reason(f"Still unsold after {_days(dom)} on the market", c.get("days_on_market", 0)))

    value = number(f.get("estimated_market_value")) or (
        number(valuation.market_value) if valuation else None)
    if value and price:
        discount = (value - price) / value * 100
        if discount >= 2:
            out.append(Reason(
                f"Asking {percent(discount)} below our estimated market value of {usd(value)}",
                c.get("discount", 0)))

    last, years = number(f.get("last_sold_price")), number(f.get("last_sold_years_ago"))
    equity = number(f.get("implied_equity_pct"))
    if last and years is not None and price:
        ago = f"{int(round(years))} years ago" if years >= 1.5 else f"{max(1, int(round(years * 12)))} months ago"
        if f.get("loss_seller"):
            out.append(Reason(
                f"Asking {usd(price)} is below the {usd(last)} it sold for {ago}, so the seller "
                "is likely taking a loss", c.get("equity", 0)))
        elif equity is not None and equity >= 50:
            out.append(Reason(
                f"Last sold for {usd(last)} {ago}, so the seller likely holds "
                f"{'little or no debt' if equity >= 90 else f'about {percent(equity)} equity'}"
                " and has room to negotiate", c.get("equity", 0)))

    keywords = _safe_phrases(f.get("high_signal_keywords"), 2) or _safe_phrases(
        f.get("medium_signal_keywords"), 2)
    if keywords:
        out.append(Reason(f"Urgency wording in the listing: {_quote(keywords)}", c.get("keywords", 0)))
    if item.is_vacant:
        out.append(Reason("Listed as vacant, so no tenant is holding up a sale", 1.0))
    return out


def _fix_flip(item: Property, f: dict[str, Any], c: dict[str, float]) -> list[Reason]:
    out: list[Reason] = []
    price = cast(int | None, item.price)
    valuation, sqft = item.valuation, cast(int | None, item.sqft)
    if not price:
        return out
    gap_strength = c.get("offer_gap", 0)
    mao = number(f.get("max_allowable_offer")) or (
        number(valuation.max_allowable_offer) if valuation else None)
    if mao and mao >= price:
        out.append(Reason(
            f"Asking {usd(price)} sits {percent((mao - price) / price * 100)} under our maximum "
            f"offer of {usd(mao)}", gap_strength))
    arv = number(f.get("arv"))
    confidence = number(f.get("valuation_confidence"))
    if arv and arv >= price * 1.05:
        text = f"Estimated after-repair value of {usd(arv)} is {percent((arv / price - 1) * 100)} above the ask"
        if confidence is not None and confidence < 0.4:
            text += f", but comp confidence is only {round(confidence * 100)}% so verify the comps"
        out.append(Reason(text, gap_strength * 0.9))
    profit, cash, roi = (number(f.get(k)) for k in ("projected_profit", "cash_invested", "roi_pct"))
    hold = number(f.get("hold_months"))
    if profit and profit > 0 and cash:
        text = f"Modelled profit of {usd(profit)} on {usd(cash)} cash invested"
        if roi and roi > 0 and hold:
            text += f", {_a(percent(roi))} return over {plain(hold, 0)} months"
        out.append(Reason(text, c.get("levered_return", 0)))
    rehab = number(f.get("rehab_cost_mid"))
    low_high = f.get("rehab_cost_range")
    if rehab and rehab > 0:
        text = f"Renovation budgeted at about {usd(rehab)}"
        if isinstance(low_high, list) and len(low_high) == 2 and all(number(x) for x in low_high):
            text += f" (range {usd(low_high[0])} to {usd(low_high[1])})"
        out.append(Reason(text + ", already deducted from the maximum offer", gap_strength * 0.3))
    if valuation is not None and (valuation.comp_count or 0) >= 3 and valuation.comp_median_ppsf:
        text = (f"Value rests on {valuation.comp_count} sold comps at a median "
                f"${valuation.comp_median_ppsf:,.0f}/sqft")
        if valuation.comp_median_age_days is not None:
            text += f", typically sold {_days(valuation.comp_median_age_days)} ago"
        out.append(Reason(text, gap_strength * 0.25))
        if sqft and price / sqft <= valuation.comp_median_ppsf * 0.95:
            out.append(Reason(
                f"Asking ${price / sqft:,.0f}/sqft against a ${valuation.comp_median_ppsf:,.0f}/sqft "
                "comp median", gap_strength * 0.5))
    value = number(valuation.market_value) if valuation else None
    if value and (value - price) / value * 100 >= 3:
        out.append(Reason(
            f"Asking {percent((value - price) / value * 100)} below our estimated as-is value "
            f"of {usd(value)}", gap_strength * 0.4))
    pressure = number(f.get("seller_pressure_score"))
    if pressure is not None and pressure >= 40:
        out.append(Reason(f"Seller-pressure score of {round(pressure)}/100 suggests room to negotiate "
                          "below ask", gap_strength * 0.2))
    return out


def _buy_hold(item: Property, f: dict[str, Any], c: dict[str, float]) -> list[Reason]:
    out: list[Reason] = []
    price = cast(int, item.price or 0)
    cap, all_in = number(f.get("cap_rate_all_in_pct")), number(f.get("all_in_basis"))
    if cap and cap > 0 and all_in:
        text = f"Estimated {plain(cap, 1)}% cap rate on an all-in cost of {usd(all_in)}"
        if all_in > price * 1.01:
            text += f" ({usd(price)} price plus about {usd(all_in - price)} of renovation)"
        out.append(Reason(text, c.get("cap_rate", 0)))
    gross, median = number(f.get("gross_yield_pct")), number(f.get("market_median_gross_yield_pct"))
    if gross and median and gross > median:
        out.append(Reason(f"Gross rent yield of {plain(gross, 1)}% versus a local median of "
                          f"{plain(median, 1)}%", c.get("yield_vs_market", 0)))
    rent = number(f.get("monthly_rent_estimate"))
    if rent:
        method = _RENT_METHODS.get(str(f.get("rent_method")), "an estimate")
        out.append(Reason(f"Estimated rent of {usd(rent)}/month, {method}", c.get("cap_rate", 0) * 0.5))
    flow, dscr, coc = (number(f.get(k)) for k in
                       ("monthly_cash_flow_after_debt", "dscr", "cash_on_cash_pct"))
    if flow and flow > 0:
        text = f"Positive cash flow of about {usd(flow)}/month after debt service at 25% down"
        extras = [t for t in (f"DSCR {plain(dscr, 2)}" if dscr else None,
                              f"cash-on-cash {plain(coc, 1)}%" if coc and coc > 0 else None) if t]
        if extras:
            text += f" ({', '.join(extras)})"
        out.append(Reason(text, c.get("cap_rate", 0) * 0.6))
    yoy = number(f.get("market_yoy_price_change_pct"))
    if yoy and 0 < yoy <= MAX_PLAUSIBLE_YOY_PCT:
        out.append(Reason(f"Local home prices are up {percent(yoy)} year over year",
                          c.get("appreciation", 0)))
    school = number(f.get("school_score_0_10"))
    if school and school >= 6.5:
        out.append(Reason(f"School score of {plain(school, 1)}/10 supports family-tenant demand",
                          c.get("location", 0)))
    if f.get("tenant_in_place"):
        out.append(Reason("A tenant is already in place, so rent starts on day one", 2.0))
    return out


def _dimension_reason(prefix: str, dims: Any, strength: float) -> Reason | None:
    if not isinstance(dims, dict):
        return None
    scored = sorted(
        ((_LOCATION_NAMES[k], v) for k, raw in dims.items()
         if k in _LOCATION_NAMES and (v := number(raw)) is not None and v >= 6),
        key=lambda pair: -pair[1],
    )[:3]
    if not scored:
        return None
    parts = [f"{name} {plain(value, 1)}/10" for name, value in scored]
    joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f" and {parts[-1]}"
    return Reason(f"{prefix}: {joined}", strength)


def _mid_term(item: Property, f: dict[str, Any], c: dict[str, float]) -> list[Reason]:
    out: list[Reason] = []
    revenue = c.get("revenue", 0)
    season, long_rent = number(f.get("in_season_monthly_rent")), number(f.get("monthly_rent_long_term"))
    mult, months = number(f.get("in_season_multiplier")), number(f.get("season_months"))
    if season and long_rent and mult and mult > 1:
        window = f"{plain(months, 0)}-month " if months else ""
        out.append(Reason(f"Estimated {usd(season)}/month in the {window}high season, "
                          f"{plain(mult, 1)}× the {usd(long_rent)} annual-lease rent", revenue))
    net, yield_pct, all_in = (number(f.get(k)) for k in
                              ("net_annual_income", "net_yield_pct", "all_in_basis"))
    if net and net > 0 and yield_pct and all_in:
        out.append(Reason(f"Seasonal letting nets about {usd(net)} a year, {_a(percent(yield_pct))} "
                          f"yield on {usd(all_in)}", revenue * 0.9))
    uplift = number(f.get("uplift_vs_annual_lease_pct"))
    if uplift and uplift > 0:
        out.append(Reason(f"Beats a standard annual lease by {percent(uplift)} after furnishing and "
                          "management costs", revenue * 0.8))
    location = _dimension_reason("Neighborhood scores", f.get("location_dimensions_0_10"),
                                 c.get("location", 0))
    if location:
        out.append(location)
    extras = [str(x).replace("_", " ") for x in (f.get("suitability_factors") or [])
              if isinstance(x, str) and x in {"pool_access", "pets_allowed", "furnished"}]
    if extras:
        out.append(Reason(f"Amenities that suit seasonal tenants: {', '.join(extras)}",
                          c.get("suitability", 0)))
    hospital = number(f.get("nearest_hospital_miles"))
    if hospital is not None and hospital <= 3:
        out.append(Reason(f"Hospital {plain(hospital, 1)} miles away, which appeals to traveling "
                          "medical staff", c.get("suitability", 0) * 0.8))
    return out


def _airbnb(item: Property, f: dict[str, Any], c: dict[str, float]) -> list[Reason]:
    out: list[Reason] = []
    revenue = c.get("revenue", 0)
    rate, occupancy, gross = (number(f.get(k)) for k in
                              ("proxy_nightly_rate", "proxy_occupancy", "proxy_gross_annual_revenue"))
    if rate and occupancy and gross:
        out.append(Reason(f"Modelled {usd(rate)}/night at {round(occupancy * 100)}% occupancy comes to "
                          f"about {usd(gross)} a year gross (estimate)", revenue))
    net, yield_pct, all_in = (number(f.get(k)) for k in
                              ("proxy_net_annual_income", "proxy_net_yield_pct", "all_in_basis"))
    if net and net > 0 and yield_pct and all_in:
        out.append(Reason(f"Roughly {usd(net)} a year after management, cleaning and furnishing, "
                          f"{_a(percent(yield_pct))} yield on {usd(all_in)} (estimate)", revenue * 0.9))
    location = c.get("location", 0)
    beach, park = number(f.get("nearest_beach_miles")), number(f.get("nearest_theme_park_miles"))
    if beach is not None and beach <= 3:
        out.append(Reason(f"Nearest beach in our map data is {plain(beach, 1)} miles away",
                          location * 0.9))
    if park is not None and park <= 10:
        out.append(Reason(f"Nearest theme park in our map data is {plain(park, 1)} miles away",
                          location * 0.8))
    dims = _dimension_reason("Guest-appeal scores", f.get("location_dimensions_0_10"), location)
    if dims:
        out.append(dims)
    bedrooms = number(f.get("bedrooms")) or (item.bedrooms if item.bedrooms else None)
    if bedrooms and bedrooms >= 3:
        out.append(Reason(f"{int(bedrooms)} bedrooms, which suits group stays and higher nightly rates",
                          c.get("property_fit", 0)))
    fit = " ".join(str(x) for x in (f.get("property_fit_factors") or [])).lower()
    perks = [name for key, name in (("pool", "a pool"), ("waterfront", "waterfront")) if key in fit]
    if f.get("waterfront") and "waterfront" not in perks:
        perks.append("waterfront")
    if perks:
        out.append(Reason(f"Guest-friendly perks: {' and '.join(perks)}", c.get("property_fit", 0) * 0.9))
    if f.get("regulatory_verdict") == "permitted":
        out.append(Reason("Short-term rentals are permitted under the local rule", 2.0))
    return out


_BUILDERS = {
    "motivated_seller": _motivated, "fix_flip": _fix_flip, "buy_hold": _buy_hold,
    "str": _mid_term, "airbnb": _airbnb,
}


def _has_number(text: str) -> bool:
    return any(ch.isdigit() for ch in text)


def select_reasons(candidates: list[Reason]) -> list[str]:
    """Up to five reasons, strongest first, keeping at least three numeric ones when possible."""
    unique: list[Reason] = []
    for candidate in candidates:
        if not _FORBIDDEN.search(candidate.text) and all(candidate.text != r.text for r in unique):
            unique.append(candidate)
    ordered = sorted(enumerate(unique), key=lambda pair: (-pair[1].strength, pair[0]))
    chosen, rest = ordered[:MAX_REASONS], ordered[MAX_REASONS:]
    for extra in (p for p in rest if _has_number(p[1].text)):
        if sum(_has_number(p[1].text) for p in chosen) >= MIN_NUMERIC_REASONS:
            break
        plain_ones = [p for p in chosen if not _has_number(p[1].text)]
        if not plain_ones:
            break
        chosen.remove(plain_ones[-1])
        chosen.append(extra)
    chosen.sort(key=lambda pair: (-pair[1].strength, pair[0]))
    return [r.text for _, r in chosen]


def build_reasons(item: Property, lens: str, factors: dict[str, Any],
                  breakdown: dict[str, Any]) -> list[str]:
    return select_reasons(_BUILDERS[lens](item, factors, _contributions(breakdown)))


# -------------------------------------------------------------------- summary

_HEADER = re.compile(r"\bgrade [A-F] \([\d.]+/100\)\.?$", re.IGNORECASE)
_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")
# The Airbnb scorer's regulatory sentence is written for Florida; it must not caption a California home.
_STATE_NAMES = {"florida": "FL", "california": "CA"}


def _names_other_state(sentence: str, state: str | None) -> bool:
    lowered = sentence.lower()
    return any(name in lowered and code != state for name, code in _STATE_NAMES.items())


def build_summary(rationale: Any, state: str | None = None, limit: int = 320) -> str:
    """One or two sentences lifted from the scorer's own rationale, minus its grade header.

    Skipped: the header, instructions to the reader ("Confirm ..."), the "Key risks" tail,
    anything that could carry contact data, and sentences about another state's rules.
    """
    if not isinstance(rationale, str) or not rationale.strip():
        return ""
    sentences = [s.strip() for s in _SPLIT.split(rationale.strip()) if s.strip()]
    if sentences and _HEADER.search(sentences[0]):
        sentences = sentences[1:]
    kept: list[str] = []
    for sentence in sentences:
        if sentence.startswith("Key risks"):
            break
        if (_FORBIDDEN.search(sentence) or sentence.startswith("Confirm ")
                or _names_other_state(sentence, state)):
            continue
        if len(" ".join(kept + [sentence])) > limit and kept:
            break
        kept.append(sentence)
        if len(kept) == 2:
            break
    return " ".join(kept)
