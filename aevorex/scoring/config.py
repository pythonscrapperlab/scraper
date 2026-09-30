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

CONFIG_VERSION = "v3"


# ---------------------------------------------------------------------------
# Letter grades
# ---------------------------------------------------------------------------

# Investors do not trade on five 0-100 numbers. Each strategy also emits a
# letter grade on these thresholds so a list can be filtered to "A and B
# deals" and a rationale can open with a verdict. Thresholds are on the
# final (confidence-adjusted, gated) score, so a speculative 85 that shrinks
# to a 62 grades as the B it deserves.
GRADE_THRESHOLDS: Tuple[Tuple[str, float], ...] = (
    ("A", 80.0),
    ("B", 65.0),
    ("C", 50.0),
    ("D", 35.0),
    ("F", 0.0),
)


def letter_grade(score) -> "str | None":
    if score is None:
        return None
    for grade, floor in GRADE_THRESHOLDS:
        if score >= floor:
            return grade
    return "F"


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

    distress_weight: float = 0.30
    price_cut_weight: float = 0.22
    dom_weight: float = 0.15
    discount_weight: float = 0.12
    # Equity position: a seller who bought long ago with a small loan can
    # take a discount; one who bought in 2022 at the top cannot, whatever
    # they say. Derived from the last sold event in price_history, which is
    # present on 86% of the corpus.
    equity_weight: float = 0.13
    keyword_weight: float = 0.08

    # --- equity position ---
    # Assumed loan at purchase, for implied equity when no better data exists.
    assumed_purchase_ltv: float = 0.80
    # A sale older than this is treated as fully paid down for scoring.
    equity_full_after_years: int = 15
    # Implied equity (as a share of our value) at which the seller can absorb
    # any realistic discount. Equity is ABILITY to discount, not motivation
    # — a 20-year owner with a paid-off house is not thereby pressed to sell
    # — so the ability ramp tops out below the component's full range.
    equity_pct_for_full_score: float = 0.50
    equity_ability_max_score: float = 60.0
    # Asking below what they paid within this window is a loss seller — the
    # strongest pressure signal short of a lender being involved, and it
    # takes the whole component regardless of how much equity is left.
    loss_seller_window_years: int = 5
    loss_seller_score: float = 100.0

    # --- listing churn ---
    # Distinct MLS listing numbers on sale-side listed/relisted events within
    # the window. Two runs is common (a relist), three or more is a seller
    # cycling agents, which brokers read as ready-to-deal.
    churn_window_months: int = 36
    churn_two_runs_bonus: float = 6.0
    churn_three_runs_bonus: float = 14.0

    # --- confidence floor by coverage ---
    # A single weak signal (a discount against a market-median value on a
    # coming-soon listing) used to reach 80/100 on 14% coverage, because the
    # generic floor keeps 55% of the deviation from 50 regardless. Below this
    # coverage the floor drops so one input cannot carry a lead to the top.
    thin_coverage_threshold: float = 0.45
    thin_coverage_floor: float = 0.10

    # `original_list_price` below this share of the current price is junk (a
    # lot listing, a typo) and is ignored rather than producing a 99% "cut".
    min_original_price_ratio: float = 0.25

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

    # --- ranking basis ---
    # The score is now driven primarily by the GAP between asking price and
    # the maximum allowable offer, which is how a flipper actually triages:
    # at or under MAO is a deal, within ~10% is a negotiation, 20%+ over is
    # a pass. Levered ROI stays as a secondary input; on its own it is the
    # most error-amplifying number in the system, which is why valuation
    # mistakes owned the top of the previous ranking.
    gap_weight: float = 0.70
    roi_weight: float = 0.30
    # Logistic on (MAO - asking) / asking, in percent. Midpoint at -8% means
    # asking 8% over MAO scores 50; steepness 7 puts asking at MAO around 75
    # and asking 20% over MAO around 15.
    gap_midpoint_pct: float = -8.0
    gap_steepness: float = 7.0

    # --- seller pressure fusion ---
    # A property 15% over MAO with a seller who has cut three times and is
    # in foreclosure is a better lead than one 5% over MAO listed yesterday.
    # The motivated-seller score scales the flip score within this band.
    pressure_weight: float = 0.20        # 0 = ignore pressure entirely
    pressure_neutral_score: float = 35.0  # ms score at which the modifier is 1.0

    # --- winner's-curse gate ---
    # When our value sits far above asking with no corroboration, the most
    # likely explanation is that the comps are wrong, not that the listing
    # agent left 40% on the table. The valuation layer flags and shrinks
    # these; this caps what is left so they cannot lead the list.
    uncorroborated_value_cap: float = 55.0
    # A comp set that looks like the wrong product (detached comps for a
    # townhouse subject, or the reverse) is the same failure in a milder
    # form, and after the first rebuild it still owned the top ten.
    type_mismatch_cap: float = 60.0
    # Fix & flip is the strategy where valuation error is most amplified, so
    # its confidence floor is lower than the generic 0.55: a deal built on a
    # 0.1-confidence valuation should sit near 50, not near 75.
    confidence_floor: float = 0.30

    # --- exit liquidity ---
    # ARV $/sqft above this multiple of the market's p75 sold $/sqft means
    # the renovated house would be the most expensive sale on the block.
    exit_ceiling_ratio: float = 1.15
    exit_ceiling_penalty: float = 0.20
    # Median days on market above this marks a slow market for a six-month
    # flip clock; the penalty ramps to the maximum at twice the threshold.
    slow_market_dom_days: float = 60.0
    slow_market_max_penalty: float = 0.15


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

    # --- all-in basis ---
    # Yield is measured on price PLUS the renovation needed before the
    # property can be let. Without this a $60,000 uninhabitable house with a
    # $1,700 modelled rent showed a 24% cap rate and led the list; no tenant
    # pays rent on a house with no working kitchen.
    use_all_in_basis: bool = True

    # --- debt service ---
    # DSCR lenders — the product most small investors actually use for a
    # rental — want NOI over debt service of 1.20 or better; below 1.0 the
    # property does not pay its own mortgage. Negative levered cash flow is
    # capped rather than merely noted: a 9% cap rate on a building nobody
    # will finance is not a 9% cap rate.
    min_dscr: float = 1.0
    negative_cash_flow_cap: float = 55.0
    closing_cost_pct: float = 0.02

    # --- Florida condo risk ---
    # Post-Surfside, condos built before 1992 face milestone structural
    # inspections and fully funded reserves (SIRS), which are arriving as
    # special assessments and falling prices. The prior scorer sent buyers
    # straight at this stock. Points accumulate and are applied as a
    # multiplicative penalty; a financeability killer caps outright.
    condo_risk_year_built_before: int = 1992
    condo_risk_age_points: float = 25.0
    # HOA per sqft this far above the market's own median HOA suggests a
    # distressed association (deferred maintenance already being billed).
    condo_hoa_ratio_high: float = 1.6
    condo_hoa_high_points: float = 20.0
    condo_special_assessment_points: float = 35.0
    condo_cash_only_points: float = 30.0
    # Penalty fraction per 100 risk points, capped.
    condo_risk_max_penalty: float = 0.40
    # A condo-hotel or resort unit cannot be let long-term and cannot be
    # financed conventionally. It is not a buy-and-hold at all.
    condo_hotel_cap: float = 25.0
    # An attached property with no HOA fee on record is missing data, not a
    # free building. Its NOI is overstated, so its score is capped.
    missing_hoa_cap: float = 45.0


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

    # SEASONAL MODEL. The previous flat 1.35x over 8 months lost to an annual
    # lease on every one of 9,423 scored properties, so the score degenerated
    # into "cheapest property wins". A Florida seasonal let is not a flat
    # premium: in-season (roughly January to April) furnished rents in the
    # snowbird markets run 1.8-2.5x the annual rate, shoulder months a little
    # above annual, and summer is soft or empty. Modelled as three blocks.
    season_months: float = 4.0
    season_multiplier: float = 1.9
    shoulder_months: float = 4.0
    shoulder_multiplier: float = 1.1
    # Remaining months are assumed vacant (12 - season - shoulder).
    # Per-market overrides for the in-season multiplier, keyed "city,ST"
    # lowercased. The Gulf and Treasure Coast snowbird markets command more
    # than the inland ones; Miami's seasonality is weaker and its annual
    # rents already high.
    season_multiplier_by_market: Dict[str, float] = field(default_factory=lambda: {
        "naples,FL": 2.4, "vero beach,FL": 2.2, "sarasota,FL": 2.2,
        "jupiter,FL": 2.2, "fort myers,FL": 2.1, "boca raton,FL": 2.0,
        "cape coral,FL": 2.0, "st. petersburg,FL": 1.9, "clearwater,FL": 1.9,
        "west palm beach,FL": 1.9, "palm coast,FL": 1.7, "st. augustine,FL": 1.8,
        "miami,FL": 1.5, "miami beach,FL": 1.7, "orlando,FL": 1.4,
        "jacksonville,FL": 1.3, "pensacola,FL": 1.6, "tampa,FL": 1.5,
        "san jose,CA": 1.2,
    })
    # Higher than long-term (turnover, furnishing, utilities) but far below
    # nightly.
    management_pct: float = 0.15
    furnishing_cost_per_sqft: float = 12.0

    # The revenue score now answers "is mid-term better than an annual lease
    # HERE" (uplift) as well as "is the yield good" — and where the annual
    # lease wins, the strategy says so and is capped rather than pretending.
    uplift_weight: float = 0.55
    net_yield_weight: float = 0.45
    uplift_pct_for_full_score: float = 40.0
    annual_lease_wins_cap: float = 45.0

    # Travelling healthcare staff on 13-week contracts are the other half of
    # mid-term demand. A hospital (not a veterinary one) inside this radius
    # is a stronger signal than any walkability dimension.
    hospital_radius_miles: float = 3.0
    hospital_bonus: float = 10.0

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
    # Was 2.6x, which on a modelled $14,000/month Brickell 3-bed produced a
    # $1,197 nightly rate and a 24% net yield — roughly double what those
    # units actually gross. 2.0x at lower occupancy is still generous but
    # no longer fantasy.
    adr_multiple_of_daily_ltr: float = 2.0
    base_occupancy: float = 0.55
    peak_location_occupancy: float = 0.65
    weak_location_occupancy: float = 0.40

    # Condo buildings overwhelmingly prohibit or restrict nightly letting in
    # their own rules regardless of what the city allows, and nothing in a
    # listing says otherwise unless it advertises it. Capped, not zeroed.
    condo_default_cap: float = 60.0

    # Leisure demand drivers the walkability dimensions cannot see, taken
    # from the points-of-interest table by name.
    beach_radius_miles: float = 1.5
    beach_bonus: float = 14.0
    theme_park_radius_miles: float = 8.0
    theme_park_bonus: float = 12.0
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
