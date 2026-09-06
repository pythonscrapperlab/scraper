"""
Weights, thresholds and cost assumptions for the five investment strategies.

Nothing scoring-relevant is hardcoded in the scorer modules — every number a
reasonable analyst might argue with lives here, so it can be tuned (or
overridden per market later) without touching scoring logic.

Numbers are grounded in the live corpus wherever the corpus can ground them;
those cases are noted inline. The rest are stated market assumptions.
"""

from dataclasses import dataclass, field
from typing import Dict, Tuple

CONFIG_VERSION = "v2"


# ---------------------------------------------------------------------------
# Motivated seller
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MotivatedSellerConfig:
    """
    Measures seller PRESSURE, not property quality.

    Weights are ordered by how well each signal actually predicts a
    below-market sale. Verified distress leads because it is a fact about the
    seller's situation rather than an inference from their behaviour — and it
    was completely unused by the previous version despite being structured and
    available on every property.
    """

    distress_weight: float = 0.34
    price_cut_weight: float = 0.26
    dom_weight: float = 0.18
    discount_weight: float = 0.14
    keyword_weight: float = 0.08

    # --- distress status -> points. Not interchangeable: these differ in how
    # motivated the seller is AND in how executable the deal is.
    distress_points: Dict[str, float] = field(default_factory=lambda: {
        # Lender already owns it. Unemotional institutional seller, wants it
        # off the books, normal closing process. The best combination of
        # motivation and executability available.
        "reo": 100.0,
        # Heirs, often out of state, emotionally detached, want a clean fast
        # close, property usually has deferred maintenance.
        "probate_or_estate": 92.0,
        # Genuinely motivated but the lender must approve: 60-180 day
        # closings and they can still walk. Motivated is not the same as fast,
        # and scoring it like REO would misrepresent the deal.
        "short_sale": 74.0,
        # Real distress, but a courthouse auction is cash-only, no inspection,
        # possible occupants and junior liens. Scored below REO for that
        # reason, not above it.
        "foreclosure": 80.0,
        "auction": 70.0,
    })

    # --- price-cut behaviour ---
    # Velocity matters more than count: three cuts in 60 days is capitulation,
    # three over two years is a stubborn seller slowly discovering the market.
    cuts_per_30d_for_full_score: float = 0.75
    cumulative_cut_pct_for_full_score: float = 15.0
    # A cut in the last month is live pressure; one from a year ago is stale.
    recent_cut_days: int = 45
    recent_cut_bonus: float = 12.0
    relisted_bonus: float = 14.0
    off_market_bonus: float = 18.0   # previously withdrawn without selling

    cut_velocity_weight: float = 0.45
    cut_depth_weight: float = 0.40
    cut_recency_weight: float = 0.15

    # --- days on market ---
    # Scored against the market's own median rather than absolute days, since
    # median DOM in the corpus ranges from 1 to 29 by zip.
    dom_ratio_full_score: float = 3.0   # 3x the local median
    dom_absolute_fallback_days: int = 120

    # --- price vs our derived market value ---
    # Listed below what the comps say it is worth. A rational unpressured
    # seller prices at or above market.
    discount_pct_for_full_score: float = 12.0

    high_signal_keywords: Tuple[str, ...] = (
        "motivated seller", "must sell", "bring all offers", "priced to sell",
        "bring offers", "make an offer", "seller says sell", "won't last",
    )
    medium_signal_keywords: Tuple[str, ...] = (
        "investor special", "handyman special", "needs tlc", "bring your contractor",
        "quick close", "flexible seller", "relocating", "downsizing",
    )
    high_keyword_points: float = 60.0
    medium_keyword_points: float = 30.0

    # Occupancy modifiers, applied to the blended score.
    vacant_multiplier: float = 1.08        # carrying cost with no income
    tenant_occupied_multiplier: float = 0.94  # complicates a sale


# ---------------------------------------------------------------------------
# Fix and flip
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FixFlipConfig:
    """
    Deal arithmetic. The output an investor actually wants is a Maximum
    Allowable Offer and a projected return, so the score is a function of
    return rather than an abstract index.
    """

    # --- transaction costs ---
    purchase_closing_pct: float = 0.02
    # Commission plus doc stamps, title and concessions. Florida doc stamps
    # alone are 0.70% of the sale price.
    selling_cost_pct: float = 0.08

    # --- financing: hard money is how most flips are actually funded, and it
    # is the conservative case. Configurable to model cash.
    use_financing: bool = True
    loan_to_cost: float = 0.85
    annual_interest_rate: float = 0.12
    origination_points: float = 0.02
    hold_months: float = 6.0

    # --- scoring curve ---
    # Logistic centred near the threshold where a flip becomes worth doing.
    # 90% of retail listings have negative margin, so a linear ramp would
    # spend nearly all its range on deals nobody would take; an S-curve
    # centred at 15% ROI spreads the top decile out where decisions are made.
    roi_midpoint_pct: float = 15.0
    roi_steepness: float = 9.0
    # A pure logistic saturates: 100% and 296% ROI both round to the same
    # score, leaving the very best deals unorderable. A small linear tail
    # restores strict ranking at the extremes without disturbing the curve
    # around the decision band. 400 spans any ROI that can occur in practice.
    roi_tail_weight: float = 0.06
    roi_tail_span: float = 400.0

    # Below this the deal is not worth modelling further.
    min_viable_roi_pct: float = 5.0

    # A wide comp set means an uncertain exit. This scales the ARV actually
    # used, so dispersion reduces the score rather than merely annotating it.
    dispersion_haircut_at_cv: float = 0.35
    max_dispersion_haircut: float = 0.12

    # Florida exit-liquidity drag: a frame-built pre-code house with an old
    # roof is harder to sell and harder for a buyer to insure.
    uninsurable_risk_penalty: float = 0.10

    # 70% rule, used as a sanity benchmark alongside the full underwrite.
    mao_arv_factor: float = 0.70


# ---------------------------------------------------------------------------
# Buy and hold
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BuyHoldConfig:
    """Long-term rental. Cap rate carries it, tempered by market and location."""

    cap_rate_weight: float = 0.50
    yield_vs_market_weight: float = 0.20
    appreciation_weight: float = 0.15
    location_weight: float = 0.15

    # Pre-debt cap rate. 6% is a solid Florida long-term hold in this rate
    # environment; 8%+ is excellent and usually signals either a rough
    # submarket or genuine mispricing.
    cap_rate_floor: float = 0.02
    cap_rate_ceiling: float = 0.085

    vacancy_rate: float = 0.08
    management_pct: float = 0.09

    # School quality is the strongest single predictor of family-rental
    # demand and tenant stability, and primary_schools_score is the
    # highest-variance location dimension available (sd 3.41 on a 0-10 scale).
    school_weight_within_location: float = 0.45

    # Financing, for cash-on-cash alongside the unlevered cap rate.
    down_payment_pct: float = 0.25
    mortgage_rate: float = 0.0675
    mortgage_years: int = 30

    # A lease already in place removes lease-up cost and void risk.
    tenant_in_place_bonus: float = 6.0
    # Association approval adds weeks and can reject an investor buyer.
    hoa_approval_penalty: float = 4.0


# ---------------------------------------------------------------------------
# Mid-term / snowbird letting (30+ days)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MidTermRentalConfig:
    """
    The legally quiet income strategy.

    Florida Statute 509 defines a regulated "vacation rental" as letting for
    periods of less than 30 days (or more than three times a year). At 30+
    days a property is simply a residential tenancy: no state vacation-rental
    licence, and most HOA minimum-lease rules — commonly one or three months —
    permit it. That is the entire reason this is scored separately from
    nightly letting rather than folded into it.

    Demand is snowbirds, travelling healthcare staff and relocations. They
    want quiet, comfortable, furnished, near healthcare and amenities — close
    to the opposite of the nightly-rental profile.
    """

    location_weight: float = 0.30
    revenue_weight: float = 0.40
    suitability_weight: float = 0.30

    # Mid-term commands a premium over an annual lease but is not nightly
    # money. 1.35x is a realistic Florida seasonal blend.
    rent_premium_multiplier: float = 1.35
    # Snowbird season is roughly November to April; the rest of the year is
    # softer. 8 months of occupancy at the premium rate is a fair year.
    occupied_months_per_year: float = 8.0
    # Higher than long-term (turnover, furnishing, utilities) but far below
    # nightly.
    management_pct: float = 0.15
    furnishing_cost_per_sqft: float = 12.0

    # Lifestyle dimensions that matter to a 3-month tenant, and their weights.
    location_dimensions: Dict[str, float] = field(default_factory=lambda: {
        "quiet_score": 0.26,
        "wellness_score": 0.20,
        "groceries_score": 0.18,
        "restaurants_score": 0.14,
        "parks_score": 0.12,
        "pedestrian_score": 0.10,
    })

    # A 55+ community is a POSITIVE here — it is the snowbird market — and
    # disqualifying for nightly letting. Same field, opposite sign.
    age_restricted_bonus: float = 10.0
    furnished_bonus: float = 14.0
    pool_bonus: float = 5.0
    # Only bites when the minimum lease exceeds a season.
    long_minimum_lease_months: int = 7
    long_minimum_lease_penalty: float = 25.0


# ---------------------------------------------------------------------------
# Nightly vacation letting (under 30 days)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AirbnbConfig:
    """
    Nightly letting. The strategy with the highest ceiling and the hardest gate.

    REGULATORY REALITY: sub-30-day letting IS regulated in Florida, both by
    the state (DBPR vacation-rental licence) and — decisively — by
    municipalities, many of which restrict or prohibit it. That varies
    street by street and cannot be read off a listing.

    This dataset cannot resolve it: only ~60 of 7,329 listings state a
    minimum lease term in any form. So the gate is a curated municipality
    table that ships EMPTY, and every property carries
    `regulatory_status_unverified` until it is populated. Hardcoding legal
    conclusions about specific cities would be both wrong and a liability.

    REVENUE REALITY: there is no ADR or occupancy data anywhere in this
    schema. Revenue here is a PROXY built from bedroom capacity, location
    tier and amenities. It ships with a wide band and an explicit flag, and
    must never be presented as a forecast.
    """

    location_weight: float = 0.40
    revenue_weight: float = 0.30
    property_fit_weight: float = 0.30

    # --- regulatory gates. Caps, not weights: no amount of beachfront charm
    # makes a prohibited property a good nightly rental.
    prohibited_cap: float = 5.0
    restricted_cap: float = 45.0
    unverified_cap: float = 78.0     # the default, until the table is populated
    hoa_restriction_cap: float = 25.0
    age_restricted_cap: float = 10.0

    # --- revenue proxy ---
    # Nightly rate as a multiple of the property's daily long-term rent.
    # 2.6x is a conservative mid-range for Florida leisure markets.
    adr_multiple_of_daily_ltr: float = 2.6
    base_occupancy: float = 0.60
    peak_location_occupancy: float = 0.75
    weak_location_occupancy: float = 0.42
    # Nightly management, cleaning and platform fees. Far above long-term.
    management_pct: float = 0.28
    furnishing_cost_per_sqft: float = 18.0

    # Lifestyle dimensions a leisure traveller books on — deliberately the
    # near-inverse of the mid-term profile.
    location_dimensions: Dict[str, float] = field(default_factory=lambda: {
        "restaurants_score": 0.24,
        "nightlife_score": 0.22,
        "vibrant_score": 0.18,
        "cafes_score": 0.14,
        "shopping_score": 0.12,
        "pedestrian_score": 0.10,
    })

    # --- property fit ---
    # A private pool is one of the strongest booking drivers in Florida; a
    # shared association pool is not remotely the same thing, which is why
    # the amenity parser keeps them apart.
    private_pool_bonus: float = 18.0
    waterfront_bonus: float = 16.0
    water_view_bonus: float = 8.0
    furnished_bonus: float = 10.0
    # Group capacity drives revenue more than floor area does.
    bedroom_scores: Dict[int, float] = field(default_factory=lambda: {
        0: 25.0, 1: 40.0, 2: 60.0, 3: 78.0, 4: 92.0, 5: 100.0,
    })

    # Curated from local ordinances. "city,ST" (lowercased) -> one of
    # prohibited | restricted | permitted. Ships empty ON PURPOSE.
    municipal_rules: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoringConfig:
    motivated_seller: MotivatedSellerConfig = field(default_factory=MotivatedSellerConfig)
    fix_flip: FixFlipConfig = field(default_factory=FixFlipConfig)
    buy_hold: BuyHoldConfig = field(default_factory=BuyHoldConfig)
    mid_term: MidTermRentalConfig = field(default_factory=MidTermRentalConfig)
    airbnb: AirbnbConfig = field(default_factory=AirbnbConfig)
    version: str = CONFIG_VERSION

    # Properties whose price is not a real asking price (auction deposits and
    # the like) are excluded from every strategy. 42 live properties list at
    # $5,000 against assessed values of $167k-$556k; taken at face value they
    # top every ranking for entirely the wrong reason.
    skip_placeholder_prices: bool = True


DEFAULT_CONFIG = ScoringConfig()
