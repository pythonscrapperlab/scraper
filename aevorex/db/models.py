"""
SQLAlchemy ORM models for Aevorex scraper.

All tables use UUIDs as primary keys, UTC timestamps, and JSONB for platform-specific data.
Single deduplicated "properties" table across all sources, with per-source detail
(price history, tax history, schools, comps, POIs, location scores, features)
split into satellite tables so the base table stays lean and easy to score against.
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    JSON,
    UUID,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    false,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utc_now() -> datetime:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Run(Base):
    """One durable, PII-free execution record per CLI invocation."""

    __tablename__ = "runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    kind = Column(String(50), nullable=False, index=True)
    scope = Column(JSONB, nullable=False, default=dict)
    started_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)
    finished_at = Column(DateTime(timezone=False), nullable=True)
    status = Column(String(20), nullable=False, default="running", index=True)
    counts = Column(JSONB, nullable=False, default=dict)
    error_class = Column(String(255), nullable=True)
    duration_s = Column(Float, nullable=True)

    __table_args__ = (Index("idx_runs_kind_started", "kind", "started_at"),)


class RawScrape(Base):
    """
    Stores the raw JSON blob from every scrape run.
    Never lose source data — always preserve what came from the platform.
    """

    __tablename__ = "raw_scrapes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    source = Column(String(50), nullable=False, index=True)  # zillow | redfin | realtor
    external_id = Column(String(255), nullable=True, index=True)  # platform's property ID
    url = Column(String(2048), nullable=False, index=True)
    raw_json = Column(JSON, nullable=False)  # full response blob
    scrape_status = Column(
        String(50), nullable=False, default="success"
    )  # success | failed | partial
    scraped_at = Column(DateTime(timezone=False), nullable=False)
    created_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)

    # Relationships
    properties = relationship("Property", back_populates="raw_scrape", uselist=True)

    __table_args__ = (
        Index("idx_raw_scrapes_source_created", "source", "created_at"),
        Index("idx_raw_scrapes_status", "scrape_status"),
    )


class Property(Base):
    """
    The single deduplicated table for all platforms.
    One row per real-world property.
    Dedup key: (normalized_address + zip_code) or platform IDs.
    """

    __tablename__ = "properties"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    raw_scrape_id = Column(
        UUID(as_uuid=True), ForeignKey("raw_scrapes.id"), nullable=True
    )

    # Platform IDs (nullable — populated as scrapers find them)
    zillow_id = Column(String(255), nullable=True, index=True)
    redfin_id = Column(String(255), nullable=True, index=True)
    realtor_id = Column(String(255), nullable=True, index=True)

    # Primary source priority: Realtor > Redfin > Zillow
    primary_source = Column(String(50), nullable=False)  # realtor | redfin | zillow

    # Core property data
    address = Column(String(500), nullable=False, index=True)
    city = Column(String(100), nullable=False, index=True)
    state = Column(String(2), nullable=False, index=True)  # FL, CA, etc.
    zip_code = Column(String(10), nullable=False, index=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)

    # Assessor Parcel Number: strongest cross-source dedup key, since it
    # identifies the physical parcel rather than a formatted address string.
    # Stored normalized (alphanumeric only, uppercase) so lookups are a plain
    # indexed equality match. Not unique nationwide, so pair with `state`
    # when matching. `county` is informational (also useful for tax context).
    apn = Column(String(64), nullable=True, index=True)
    county = Column(String(100), nullable=True)

    price = Column(Integer, nullable=True, index=True)
    bedrooms = Column(Integer, nullable=True)
    bathrooms = Column(Float, nullable=True)
    sqft = Column(Integer, nullable=True)
    lot_size = Column(Float, nullable=True)  # acres
    year_built = Column(Integer, nullable=True)

    property_type = Column(String(50), nullable=True)  # house | condo | townhouse | multi_family

    # The source's own MLS status, verbatim — "Active", "Coming Soon", "New",
    # "Pending", "Sold", "Contingent- Accepting Backups" (the mangled spacing
    # is Redfin's, kept as-is so this column always reconciles against
    # raw_scrapes). Rows written before 2026-08 hold the schema.org
    # availability value ("InStock") instead; see availability_status.
    listing_status = Column(String(50), nullable=True, index=True)
    # Canonical slug for the above: active | coming_soon | new | pending |
    # contingent | sold. Query this, not the raw string.
    listing_status_normalized = Column(String(30), nullable=True, index=True)
    # schema.org offers.availability ("InStock"), which is all this table used
    # to carry. Kept separate in case anything downstream still reads it.
    availability_status = Column(String(50), nullable=True)

    days_on_market = Column(Integer, nullable=True)
    days_on_market_mls = Column(Integer, nullable=True)

    # Cross-platform variance
    price_variance = Column(Float, nullable=True)  # % difference across sources
    source_count = Column(Integer, nullable=False, default=1)  # 1 to 3

    listing_url = Column(String(2048), nullable=True)
    description = Column(Text, nullable=True)

    # Source-generated bullet summary of the listing (Redfin's `aiSummary`),
    # stored verbatim, one "- " prefixed bullet per line. Materially richer
    # than `description` for condition signals — roof material/age, impact
    # windows, HVAC age — i.e. the Florida insurability inputs. Deliberately
    # left unparsed here: extraction belongs to the scoring layer.
    ai_summary = Column(Text, nullable=True)

    # Per-listing metrics the source computes for us (Redfin's
    # `offer_insights`). `price_drop_count` and `sale_to_list_pct` describe
    # THIS listing; the `area_*` fields describe its surrounding market as the
    # source defines "area" (not necessarily the zip — see market_snapshots
    # for the zip-level series).
    price_drop_count = Column(Integer, nullable=True)
    sale_to_list_pct = Column(Float, nullable=True)
    area_price_drop_pct = Column(Float, nullable=True)
    area_avg_days_to_pending = Column(Integer, nullable=True)
    area_median_list_price = Column(Integer, nullable=True)

    # When the source last touched this listing record (Redfin's
    # basic_info.propertyLastUpdatedDate, epoch ms). A freshness signal that's
    # independent of when we happened to scrape it.
    source_updated_at = Column(DateTime(timezone=False), nullable=True)

    # Platform-specific fields and conflicts stored here
    meta = Column(JSON, nullable=True)  # zestimate | redfin_estimate | mls_number | walk_score | hoa_fee | discrepancies

    # --- Economics, promoted out of `meta` into typed columns -------------
    # These are all scoring inputs (ARV, cap rate, gross yield, carrying
    # cost) and the API needs to filter and sort on them — "cap rate > 6%"
    # can't be an indexed predicate while the rent estimate lives in a JSON
    # blob. They remain mirrored in `meta` as the provenance copy, the same
    # way the offer_insights numbers are.
    avm_value = Column(Float, nullable=True)  # source's automated valuation
    rental_est_low = Column(Integer, nullable=True)  # monthly, long-term
    rental_est_mid = Column(Integer, nullable=True)
    rental_est_high = Column(Integer, nullable=True)
    hoa_monthly = Column(Float, nullable=True)
    price_per_sqft = Column(Float, nullable=True)
    tax_annual = Column(Float, nullable=True)
    has_open = Column(Boolean, nullable=True)

    # --- Climate risk (0-10 as the source scores it) ----------------------
    # In Florida these are not "nice to have" metadata: flood and wind are
    # the two inputs that decide whether a property is insurable and at what
    # premium, which is the difference between a positive and negative
    # cash-flow model on a buy-and-hold or STR deal.
    flood_factor = Column(Integer, nullable=True)
    fire_factor = Column(Integer, nullable=True)
    heat_factor = Column(Integer, nullable=True)
    wind_factor = Column(Integer, nullable=True)

    # --- Walkability (Walk Score) -----------------------------------------
    # Distinct from the location_scores table, which is the source's own
    # separate lifestyle-score block. `transit_score` regressed to 0% fill in
    # the most recent crawl while walk/bike kept working — prefer
    # location_scores.transit_score until that's resolved.
    walk_score = Column(Float, nullable=True)
    transit_score = Column(Float, nullable=True)
    bike_score = Column(Float, nullable=True)

    # --- Condition ---------------------------------------------------------
    year_renovated = Column(Integer, nullable=True)
    stories = Column(Float, nullable=True)

    # --- Seller distress and use restrictions ------------------------------
    # Extracted from listing remarks at ingestion by
    # aevorex/normalizers/distress.py, which documents what each signal means
    # to an investor and why they are not interchangeable. NULL (not False)
    # means there was no listing text to read — the project convention is to
    # flag a gap rather than assert a negative.
    is_foreclosure = Column(Boolean, nullable=True)
    is_auction = Column(Boolean, nullable=True)
    auction_date = Column(DateTime(timezone=False), nullable=True)
    is_short_sale = Column(Boolean, nullable=True)
    is_reo = Column(Boolean, nullable=True)  # bank/lender already owns it
    is_probate_or_estate = Column(Boolean, nullable=True)
    is_as_is = Column(Boolean, nullable=True)
    is_tenant_occupied = Column(Boolean, nullable=True)  # + for buy_hold, - for fix_flip
    is_vacant = Column(Boolean, nullable=True)
    is_cash_only = Column(Boolean, nullable=True)  # often "won't finance", not a preference
    is_age_restricted = Column(Boolean, nullable=True)  # 55+; ends the STR case
    is_rental_restricted = Column(Boolean, nullable=True)  # HOA min-lease / no-lease
    allows_short_term_rental = Column(Boolean, nullable=True)
    # {signal_name: [matched phrases]} — the audit trail behind the booleans
    # above, so any flag can be checked without re-reading the listing.
    distress_signals = Column(JSON, nullable=True)

    # --- Ingestion-level data quality --------------------------------------
    # True when `price` is provably not a market asking price — the clearest
    # case being a foreclosure auction, where the source publishes the deposit
    # or opening bid (38 live rows sit at exactly $5,000 against assessed
    # values of $167k-$556k). Without this, those rank as the best deal in
    # every strategy at once. See RedfinNormalizer._detect_placeholder_price.
    price_is_placeholder = Column(Boolean, nullable=True)
    # Ingestion-time quality notes, e.g. ["price_is_auction_deposit"].
    # Deliberately separate from PropertyAnalysis.*_flags, which are
    # per-strategy scoring flags computed much later.
    data_quality_flags = Column(JSON, nullable=True)

    listed_at = Column(DateTime(timezone=False), nullable=True)
    last_seen_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)
    created_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=False), nullable=False, default=utc_now, onupdate=utc_now)

    # True on insert and whenever a rescrape changes scoring-relevant data
    # (see Deduplicator.upsert / PipelineRunner._upsert_satellites). The
    # scoring runner only queries WHERE needs_analysis, and clears it once
    # property_analysis has been (re)computed — see aevorex/scoring/runner.py.
    needs_analysis = Column(Boolean, nullable=False, default=True, server_default=true())

    # Relationships
    raw_scrape = relationship("RawScrape", back_populates="properties")
    price_history = relationship("PriceHistory", back_populates="property", cascade="all, delete-orphan")
    tax_history = relationship("TaxHistory", back_populates="property", cascade="all, delete-orphan")
    property_images = relationship("PropertyImage", back_populates="property", cascade="all, delete-orphan")
    open_houses = relationship("OpenHouse", back_populates="property", cascade="all, delete-orphan")
    leads = relationship("Lead", back_populates="property", cascade="all, delete-orphan")
    schools = relationship("School", back_populates="property", cascade="all, delete-orphan")
    comps = relationship("PropertyComp", back_populates="property", cascade="all, delete-orphan")
    pois = relationship("PointOfInterest", back_populates="property", cascade="all, delete-orphan")
    transport_stops = relationship("TransportStop", back_populates="property", cascade="all, delete-orphan")
    location_score = relationship("LocationScore", back_populates="property", uselist=False, cascade="all, delete-orphan")
    features = relationship("PropertyFeature", back_populates="property", uselist=False, cascade="all, delete-orphan")
    analysis = relationship("PropertyAnalysis", back_populates="property", uselist=False, cascade="all, delete-orphan")
    valuation = relationship("PropertyValuation", back_populates="property", uselist=False, cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_properties_address_zip", "address", "zip_code"),
        Index("idx_properties_state_city", "state", "city"),
        Index("idx_properties_created_at", "created_at"),
        Index("idx_properties_source_count", "source_count"),
        Index("idx_properties_apn_state", "apn", "state"),
        Index("idx_properties_needs_analysis", "needs_analysis", postgresql_where=(needs_analysis == true())),
        # Partial indexes: the distress cohorts are a tiny fraction of the
        # table (44 auctions in 6,854 rows) and are always queried as
        # "show me the ones that are True", so indexing only those rows keeps
        # the index a few kilobytes instead of covering 6,800 falses.
        Index("idx_properties_foreclosure", "state", "is_foreclosure",
              postgresql_where=(is_foreclosure == true())),
        Index("idx_properties_auction", "state", "auction_date",
              postgresql_where=(is_auction == true())),
        Index("idx_properties_placeholder_price", "price_is_placeholder",
              postgresql_where=(price_is_placeholder == true())),
    )


class PriceHistory(Base):
    """
    Every listing event a source reports, priced or not.

    `price` is nullable on purpose. Five of Redfin's twelve event types
    (Listing Removed, Pending, Relisted, Contingent, Delisted) never carry a
    price — they record a state transition, not a number — and while this
    column was NOT NULL those events were dropped on insert, taking the
    expired-listing and relisted signals with them. Anything filtering this
    table for pricing math must filter on `price IS NOT NULL`, not assume it.
    """

    __tablename__ = "price_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    property_id = Column(UUID(as_uuid=True), ForeignKey("properties.id"), nullable=False, index=True)
    source = Column(String(50), nullable=False, index=True)  # zillow | redfin | realtor
    price = Column(Integer, nullable=True)
    # The source's own wording, verbatim ("Listed", "Sold (MLS)", "Listing
    # Removed", ...) — the audit trail, reconcilable against raw_scrapes.
    event = Column(String(50), nullable=False)
    # Canonical slug for the above (see aevorex/db/event_types.py). Consumers
    # should read this; `event` is for display and provenance.
    event_type = Column(String(30), nullable=True, index=True)
    event_date = Column(DateTime(timezone=False), nullable=False)
    # The event's own originating feed ("Beaches MLS", "MARMLS", "Public
    # Records", ...) — distinct from `source`, which is the platform we
    # scraped it from and is always one of zillow/redfin/realtor.
    event_source = Column(String(150), nullable=True)
    # The source's ID for the listing this event belongs to (Redfin's
    # `sourceId`, i.e. the MLS number). NOT unique per event — one listing ID
    # spans every event in that listing cycle — so it identifies a listing
    # *run*, which is what makes it useful for telling a relist apart from
    # the original listing. See the uniqueness note in the pipeline's
    # _upsert_price_history.
    source_event_id = Column(String(64), nullable=True)
    # Whether this event belongs to the property's RENTAL history rather than
    # its sale history. Redfin files both in one timeline, and rents average
    # ~$4,855 against ~$674,901 for a sale listing — so any sale-price
    # arithmetic that doesn't exclude these is mixing units. Set by the
    # normalizer, which attributes a directionless "Price Changed" to
    # whichever listing cycle was open at that point in the timeline.
    is_rental_event = Column(Boolean, nullable=False, default=False, server_default=false())
    created_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)

    # Relationships
    property = relationship("Property", back_populates="price_history")

    __table_args__ = (
        Index("idx_price_history_property_source", "property_id", "source"),
        Index("idx_price_history_event_date", "event_date"),
        Index("idx_price_history_event_type", "property_id", "event_type"),
        # Sale-side lookups are the common case; keep them off the rental rows.
        Index("idx_price_history_sale_events", "property_id", "event_type",
              postgresql_where=(is_rental_event == false())),
    )


class TaxHistory(Base):
    """Tracks tax assessments across sources."""

    __tablename__ = "tax_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    property_id = Column(UUID(as_uuid=True), ForeignKey("properties.id"), nullable=False, index=True)
    source = Column(String(50), nullable=False, index=True)  # zillow | redfin | realtor
    tax_year = Column(Integer, nullable=False)
    tax_amount = Column(Integer, nullable=True)
    # The two components of the assessment, stored separately because the
    # source frequently reports only one of them (533 of 3,827 populated rows
    # in the latest run carry an improvement value with no land value).
    land_value = Column(Integer, nullable=True)
    improvement_value = Column(Integer, nullable=True)
    # land_value + improvement_value, and ONLY when both are present. It used
    # to be their sum with a missing component treated as zero, which recorded
    # an improvement-only parcel's assessment as its improvement alone —
    # understated by the entire land component (routinely 20-40% of value in
    # Florida) with nothing to indicate it. A NULL here is a visible gap; a
    # silently understated number is not.
    assessed_value = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)

    # Relationships
    property = relationship("Property", back_populates="tax_history")

    __table_args__ = (
        Index("idx_tax_history_property_year", "property_id", "tax_year"),
        Index("idx_tax_history_source", "source"),
    )


class PropertyImage(Base):
    """Stores property images from each source."""

    __tablename__ = "property_images"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    property_id = Column(UUID(as_uuid=True), ForeignKey("properties.id"), nullable=False, index=True)
    source = Column(String(50), nullable=False, index=True)  # zillow | redfin | realtor
    url = Column(String(2048), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)

    # Relationships
    property = relationship("Property", back_populates="property_images")

    __table_args__ = (
        Index("idx_property_images_property_source", "property_id", "source"),
    )


class OpenHouse(Base):
    """Tracks open houses from each source."""

    __tablename__ = "open_houses"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    property_id = Column(UUID(as_uuid=True), ForeignKey("properties.id"), nullable=False, index=True)
    source = Column(String(50), nullable=False, index=True)  # zillow | redfin | realtor
    start_time = Column(DateTime(timezone=False), nullable=False)
    end_time = Column(DateTime(timezone=False), nullable=False)
    created_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)

    # Relationships
    property = relationship("Property", back_populates="open_houses")

    __table_args__ = (
        Index("idx_open_houses_property_source", "property_id", "source"),
        Index("idx_open_houses_time_range", "start_time", "end_time"),
    )


class School(Base):
    """Nearby schools for a property, per source (Redfin's `schools` block, etc.)."""

    __tablename__ = "schools"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    property_id = Column(UUID(as_uuid=True), ForeignKey("properties.id"), nullable=False, index=True)
    source = Column(String(50), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    school_type = Column(String(20), nullable=True)  # Public | Private | Charter
    grades = Column(String(20), nullable=True)
    rating = Column(Integer, nullable=True)
    distance_miles = Column(Float, nullable=True)
    level = Column(String(20), nullable=True)  # elementary | middle | high
    created_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)

    # Relationships
    property = relationship("Property", back_populates="schools")

    __table_args__ = (
        Index("idx_schools_property_source", "property_id", "source"),
    )


class PropertyComp(Base):
    """AVM comparables a source used to justify its estimated value (Redfin's `avm_comps`)."""

    __tablename__ = "property_comps"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    property_id = Column(UUID(as_uuid=True), ForeignKey("properties.id"), nullable=False, index=True)
    url = Column(String(2048), nullable=True)
    source = Column(String(50), nullable=False, index=True)
    comp_address = Column(String(500), nullable=True)
    price = Column(Integer, nullable=True)
    bedrooms = Column(Integer, nullable=True)
    bathrooms = Column(Float, nullable=True)
    sqft = Column(Integer, nullable=True)
    sold_date = Column(DateTime(timezone=False), nullable=True)
    created_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)

    # Relationships
    property = relationship("Property", back_populates="comps")

    __table_args__ = (
        Index("idx_property_comps_property_source", "property_id", "source"),
    )


class PointOfInterest(Base):
    """Nearby POIs — groceries, restaurants, gas stations, etc (Redfin's `places`)."""

    __tablename__ = "points_of_interest"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    property_id = Column(UUID(as_uuid=True), ForeignKey("properties.id"), nullable=False, index=True)
    source = Column(String(50), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    # ALWAYS NULL, and not a normalizer bug: Redfin's `places` entries carry
    # exactly three keys — name, distance, popularity. There is no category
    # in the source payload, so there is no mapping to hunt for. Populating
    # this needs a classifier over `name` (or a third-party POI lookup), not
    # a scraper/normalizer fix. Left in place rather than dropped because
    # dropping it is a destructive migration for zero gain.
    category = Column(String(50), nullable=True)
    popularity = Column(Float, nullable=True)
    distance_miles = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)

    # Relationships
    property = relationship("Property", back_populates="pois")

    __table_args__ = (
        Index("idx_pois_property_source", "property_id", "source"),
    )


class TransportStop(Base):
    """Nearby transit stops / intersections (Redfin's `Transport` set)."""

    __tablename__ = "transport_stops"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    property_id = Column(UUID(as_uuid=True), ForeignKey("properties.id"), nullable=False, index=True)
    source = Column(String(50), nullable=False, index=True)
    stop_name = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)

    # Relationships
    property = relationship("Property", back_populates="transport_stops")

    __table_args__ = (
        Index("idx_transport_stops_property", "property_id"),
    )


class LocationScore(Base):
    """
    Walkability / lifestyle scores, one row per property (latest snapshot).
    Kept separate so it can refresh on its own cadence, independent of rescrapes.
    """

    __tablename__ = "location_scores"

    property_id = Column(UUID(as_uuid=True), ForeignKey("properties.id"), primary_key=True)
    source = Column(String(50), nullable=False)
    pedestrian_score = Column(Float, nullable=True)
    cycling_score = Column(Float, nullable=True)
    transit_score = Column(Float, nullable=True)
    car_score = Column(Float, nullable=True)
    parks_score = Column(Float, nullable=True)
    groceries_score = Column(Float, nullable=True)
    shopping_score = Column(Float, nullable=True)
    nightlife_score = Column(Float, nullable=True)
    restaurants_score = Column(Float, nullable=True)
    cafes_score = Column(Float, nullable=True)
    daycares_score = Column(Float, nullable=True)
    primary_schools_score = Column(Float, nullable=True)
    high_schools_score = Column(Float, nullable=True)
    quiet_score = Column(Float, nullable=True)
    vibrant_score = Column(Float, nullable=True)
    wellness_score = Column(Float, nullable=True)
    source_updated_at = Column(DateTime(timezone=False), nullable=True)
    updated_at = Column(DateTime(timezone=False), nullable=False, default=utc_now, onupdate=utc_now)

    # Relationships
    property = relationship("Property", back_populates="location_score", uselist=False)


class PropertyFeature(Base):
    """
    Structured amenity fields used for scoring/filtering (heating, roof, construction, etc),
    plus a JSON catch-all for whatever varies per source/MLS.
    """

    __tablename__ = "property_features"

    property_id = Column(UUID(as_uuid=True), ForeignKey("properties.id"), primary_key=True)
    heating = Column(String(100), nullable=True)
    cooling = Column(String(100), nullable=True)
    flooring = Column(String(150), nullable=True)
    construction_material = Column(String(100), nullable=True)
    roof = Column(String(100), nullable=True)
    foundation = Column(String(100), nullable=True)
    interior_features = Column(Text, nullable=True)
    appliances = Column(Text, nullable=True)
    laundry_features = Column(String(150), nullable=True)
    water_source = Column(String(50), nullable=True)
    sewer = Column(String(50), nullable=True)
    utilities = Column(Text, nullable=True)
    furnished = Column(String(30), nullable=True)
    direction_faces = Column(String(20), nullable=True)
    raw_amenities = Column(JSON, nullable=True)  # full amenities dict, source-specific
    updated_at = Column(DateTime(timezone=False), nullable=False, default=utc_now, onupdate=utc_now)

    # Relationships
    property = relationship("Property", back_populates="features", uselist=False)


class MarketSnapshot(Base):
    """
    Zip-level market stats (median price, DOM, sale-to-list %), not tied to one property.
    Redfin's `offer_insights` block is really this — worth keeping separate since the
    same numbers repeat across every listing scraped in a given zip on a given day.
    """

    __tablename__ = "market_snapshots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    zip_code = Column(String(10), nullable=False, index=True)
    source = Column(String(50), nullable=False, index=True)
    snapshot_date = Column(Date, nullable=False)
    median_list_price = Column(Integer, nullable=True)
    median_sale_price = Column(Integer, nullable=True)
    avg_days_on_market = Column(Float, nullable=True)
    sale_to_list_pct = Column(Float, nullable=True)
    yoy_price_change_pct = Column(Float, nullable=True)
    price_drop_pct = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)

    __table_args__ = (
        Index("idx_market_snapshots_zip_date", "zip_code", "snapshot_date"),
    )


class Lead(Base):
    """Generated investment leads based on scoring."""

    __tablename__ = "leads"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    property_id = Column(UUID(as_uuid=True), ForeignKey("properties.id"), nullable=False, index=True)

    lead_type = Column(
        String(50), nullable=False, index=True
    )  # fix_flip | str_airbnb | buy_hold | motivated_seller
    score = Column(Float, nullable=False)  # 0.0 to 1.0
    score_breakdown = Column(JSON, nullable=True)  # individual scoring factors

    status = Column(String(50), nullable=False, default="new")  # new | reviewed | contacted | dismissed
    created_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=False), nullable=False, default=utc_now, onupdate=utc_now)

    # Relationships
    property = relationship("Property", back_populates="leads")

    __table_args__ = (
        Index("idx_leads_property_type", "property_id", "lead_type"),
        Index("idx_leads_status", "status"),
        Index("idx_leads_score", "score"),
    )


class PropertyAnalysis(Base):
    """
    One row per property, holding all 5 investment-strategy scores together
    (motivated_seller, fix_flip, buy_hold, str, airbnb) so a client can be
    walked through every angle on a property from a single record.

    Each strategy gets its own columns rather than a single JSON blob,
    specifically so `{strategy}_score` stays a plain indexed Float the API can
    filter and sort on directly. Written and cleared by
    aevorex/scoring/runner.py.

    THREE NUMBERS PER STRATEGY, and they answer different questions:

    - `{strategy}_score` — absolute. Anchored to real economics, so a 12% ROI
      scores the same in Brickell as in Pensacola. Comparable across the whole
      country and stable over time.
    - `{strategy}_percentile` — relative. Where this property ranks against
      others in its own market (see MarketStats). Necessary because median
      sold $/sqft across our corpus spans $171 to $651, a 3.8x range: an
      absolute-only score buries every listing in a cheap market and flatters
      every listing in an expensive one.
    - `{strategy}_confidence` — how much of the strategy's input was actually
      available, 0-1. A score built on two of five signals must not look
      identical to one built on all five, which is exactly what the previous
      version did.

    `str` here means mid-term / snowbird letting (30+ days), which Florida
    Statute 509 does NOT regulate as a vacation rental. `airbnb` is nightly
    letting (under 30 days), which it does. They are separate strategies with
    opposite answers on several inputs — a 55+ community is a positive for
    snowbird demand and disqualifying for nightly letting.
    """

    __tablename__ = "property_analysis"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    property_id = Column(
        UUID(as_uuid=True), ForeignKey("properties.id"), nullable=False, unique=True, index=True
    )

    motivated_seller_score = Column(Float, nullable=True, index=True)
    motivated_seller_percentile = Column(Float, nullable=True, index=True)
    motivated_seller_confidence = Column(Float, nullable=True)
    motivated_seller_rationale = Column(Text, nullable=True)
    motivated_seller_factors = Column(JSON, nullable=True)
    motivated_seller_flags = Column(JSON, nullable=True)  # list of data-quality flag strings
    motivated_seller_grade = Column(String(1), nullable=True, index=True)
    motivated_seller_breakdown = Column(JSONB, nullable=True)

    fix_flip_score = Column(Float, nullable=True, index=True)
    fix_flip_percentile = Column(Float, nullable=True, index=True)
    fix_flip_confidence = Column(Float, nullable=True)
    fix_flip_rationale = Column(Text, nullable=True)
    fix_flip_factors = Column(JSON, nullable=True)
    fix_flip_flags = Column(JSON, nullable=True)
    fix_flip_grade = Column(String(1), nullable=True, index=True)
    fix_flip_breakdown = Column(JSONB, nullable=True)

    buy_hold_score = Column(Float, nullable=True, index=True)
    buy_hold_percentile = Column(Float, nullable=True, index=True)
    buy_hold_confidence = Column(Float, nullable=True)
    buy_hold_rationale = Column(Text, nullable=True)
    buy_hold_factors = Column(JSON, nullable=True)
    buy_hold_flags = Column(JSON, nullable=True)
    buy_hold_grade = Column(String(1), nullable=True, index=True)
    buy_hold_breakdown = Column(JSONB, nullable=True)

    # Mid-term / snowbird letting, 30+ days.
    str_score = Column(Float, nullable=True, index=True)
    str_percentile = Column(Float, nullable=True, index=True)
    str_confidence = Column(Float, nullable=True)
    str_rationale = Column(Text, nullable=True)
    str_factors = Column(JSON, nullable=True)
    str_flags = Column(JSON, nullable=True)
    str_grade = Column(String(1), nullable=True, index=True)
    str_breakdown = Column(JSONB, nullable=True)

    # Nightly vacation letting, under 30 days.
    airbnb_score = Column(Float, nullable=True, index=True)
    airbnb_percentile = Column(Float, nullable=True, index=True)
    airbnb_confidence = Column(Float, nullable=True)
    airbnb_rationale = Column(Text, nullable=True)
    airbnb_factors = Column(JSON, nullable=True)
    airbnb_flags = Column(JSON, nullable=True)
    airbnb_grade = Column(String(1), nullable=True, index=True)
    airbnb_breakdown = Column(JSONB, nullable=True)

    # Tags which ScoringConfig.CONFIG_VERSION produced these scores, so a
    # weight/threshold change can be traced against which rows are stale.
    scoring_config_version = Column(String(20), nullable=True)

    computed_at = Column(DateTime(timezone=False), nullable=False, default=utc_now, onupdate=utc_now)
    created_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)

    # Relationships
    property = relationship("Property", back_populates="analysis")


class MarketStats(Base):
    """
    Market baselines, computed from our own data — the reference every
    market-relative score is measured against.

    WHY THIS EXISTS: the previous scorers compared every property to fixed
    national thresholds. Median sold $/sqft in this corpus runs from $171
    (Pensacola 32526) to $651 (Brickell 33131). A "$200/sqft" rule is
    meaningless against that spread, and a 90-day time on market is ordinary
    in Naples luxury and alarming for an Orlando starter home.

    SOURCE: `property_comps`, which are sold transactions and carry a zip in
    100% of their addresses — 274 zips have 20+ comps sold in the last twelve
    months, 39,735 observations. That is a far better market series than the
    ~1,242 sold events in our own price_history, and it needs no extra
    scraping.

    GRAIN: one row per (geo_level, geo_key, property_class, as_of_date).
    Resolution falls back zip -> city -> county -> state so every property
    resolves to something, with `confidence` degrading down the chain.
    """

    __tablename__ = "market_stats"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    geo_level = Column(String(10), nullable=False)   # zip | city | county | state
    geo_key = Column(String(120), nullable=False)    # "33139" | "miami,FL" | "FL"
    # all | single_family | condo | townhouse | multi_family. Kept coarse on
    # purpose: splitting finer starves most cells of sample.
    property_class = Column(String(30), nullable=False, default="all")
    as_of_date = Column(Date, nullable=False)

    # --- sold-price baseline (from comps) ---
    median_sold_ppsf = Column(Float, nullable=True)
    p25_sold_ppsf = Column(Float, nullable=True)
    # The renovated-exit benchmark: a refurbished home sells at the top of its
    # comp range, not the middle. This is what fix & flip ARV is built on.
    p75_sold_ppsf = Column(Float, nullable=True)
    median_sold_price = Column(Integer, nullable=True)
    sold_count = Column(Integer, nullable=False, default=0)

    # --- listing-side baseline (from our own properties) ---
    median_list_ppsf = Column(Float, nullable=True)
    median_list_price = Column(Integer, nullable=True)
    median_dom = Column(Float, nullable=True)
    p75_dom = Column(Float, nullable=True)
    # Share of live listings carrying at least one price reduction. The
    # denominator for "is a price cut here unusual, or just what everyone does".
    pct_listings_with_cut = Column(Float, nullable=True)
    median_price_cut_pct = Column(Float, nullable=True)
    listing_count = Column(Integer, nullable=False, default=0)

    # --- income baseline ---
    median_gross_yield = Column(Float, nullable=True)   # annual rent / price
    median_rent_per_bed = Column(Float, nullable=True)
    rent_sample = Column(Integer, nullable=False, default=0)

    # --- carrying-cost baseline ---
    median_tax_rate = Column(Float, nullable=True)      # tax_annual / price
    median_hoa_monthly = Column(Float, nullable=True)

    # 0-1, driven by sample size. Consumers should weight a thin cell down
    # rather than treating a 3-sale zip like a 900-sale one.
    confidence = Column(Float, nullable=False, default=0.0)

    created_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)

    __table_args__ = (
        Index("uq_market_stats_grain", "geo_level", "geo_key", "property_class",
              "as_of_date", unique=True),
        Index("idx_market_stats_lookup", "geo_level", "geo_key", "property_class"),
    )


class PropertyValuation(Base):
    """
    Our own derived economics for one property — the layer every scorer reads.

    WHY THIS EXISTS: the scorers used to depend on the source's own estimates,
    which are present on only 57.1% (AVM) and 56.5% (rent) of properties. That
    was verified to be a hard ceiling — coalescing across every historical
    scrape of the same property returns exactly 57.0%, so there is no unread
    data to recover. Meanwhile sold comps cover 94.6% with a median of 5.9
    comps each, 99.3% of them sold within twelve months.

    Deriving value from comps rather than passing through an AVM therefore
    lifts usable coverage from 57% to ~95%, and it is also strictly more
    appropriate for fix & flip: an AVM estimates as-is value, which is the
    wrong number for a property whose thesis is that it will be renovated.

    Every estimate carries its method and a confidence, so a scorer can
    down-weight a thin or dispersed comp set instead of trusting it blindly.
    """

    __tablename__ = "property_valuation"

    property_id = Column(UUID(as_uuid=True), ForeignKey("properties.id"), primary_key=True)

    # --- value ---
    # As-is market value: comp median $/sqft x sqft, adjusted for how the
    # subject differs from its comp set, blended with the source AVM if present.
    market_value = Column(Float, nullable=True)
    market_value_method = Column(String(40), nullable=True)  # comps | comps_avm_blend | avm | market_ppsf
    # After-repair value: comp p75 $/sqft, because a renovated home exits at
    # the top of its comp range.
    arv = Column(Float, nullable=True)
    arv_method = Column(String(40), nullable=True)
    # Listed price over our market value. Below 1.0 means listed under market.
    price_to_value_ratio = Column(Float, nullable=True)

    # --- comp evidence behind the above ---
    comp_count = Column(Integer, nullable=True)
    comp_median_ppsf = Column(Float, nullable=True)
    comp_p75_ppsf = Column(Float, nullable=True)
    # Coefficient of variation of comp $/sqft. Live median is 0.158; 46% of
    # properties are under 0.15 (tight), 12% over 0.30 (wide). A wide set means
    # an uncertain exit, which must reduce a flip score, not merely annotate it.
    comp_dispersion_cv = Column(Float, nullable=True)
    comp_median_age_days = Column(Integer, nullable=True)
    valuation_confidence = Column(Float, nullable=True)  # 0-1

    # --- renovation ---
    rehab_cost_low = Column(Float, nullable=True)
    rehab_cost_mid = Column(Float, nullable=True)
    rehab_cost_high = Column(Float, nullable=True)
    rehab_basis = Column(JSON, nullable=True)   # {"base_psf": .., "adders": {...}}
    condition_class = Column(String(20), nullable=True)  # fixer|standard|updated|new

    # --- income ---
    rent_estimate_monthly = Column(Float, nullable=True)
    # source_avm | zip_bed_model | market_yield_prior — always surfaced,
    # because a modelled rent and a published one deserve different trust.
    rent_method = Column(String(30), nullable=True)
    rent_confidence = Column(Float, nullable=True)
    gross_yield = Column(Float, nullable=True)

    # --- carrying costs (annual) ---
    annual_taxes = Column(Float, nullable=True)
    # Modelled, not sourced. There is no insurance field anywhere in the
    # schema, and in Florida it is frequently the difference between a
    # positive and negative cash flow, so it cannot be left out.
    # See aevorex/valuation/insurance.py for the model and its assumptions.
    annual_insurance = Column(Float, nullable=True)
    insurance_basis = Column(JSON, nullable=True)
    annual_hoa = Column(Float, nullable=True)
    # Florida Community Development District bond — an annual assessment on
    # top of taxes and HOA that standard underwriting routinely misses.
    annual_cdd = Column(Float, nullable=True)
    annual_maintenance = Column(Float, nullable=True)
    annual_operating_expenses = Column(Float, nullable=True)

    # --- returns ---
    noi_annual = Column(Float, nullable=True)
    cap_rate = Column(Float, nullable=True)
    # The number an investor actually asks for: what should I offer?
    max_allowable_offer = Column(Float, nullable=True)

    # --- market context resolved for this property ---
    market_geo_level = Column(String(10), nullable=True)
    market_geo_key = Column(String(120), nullable=True)

    data_quality_flags = Column(JSON, nullable=True)
    valuation_version = Column(String(20), nullable=True)
    computed_at = Column(DateTime(timezone=False), nullable=False, default=utc_now, onupdate=utc_now)

    property = relationship("Property", back_populates="valuation", uselist=False)

    __table_args__ = (
        Index("idx_valuation_cap_rate", "cap_rate"),
        Index("idx_valuation_price_to_value", "price_to_value_ratio"),
    )


class ScrapeError(Base):
    """Stores every failed URL with full traceback for later review and retry."""

    __tablename__ = "scrape_errors"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    url = Column(String(2048), nullable=False, index=True)
    source = Column(String(50), nullable=False, index=True)  # zillow | redfin | realtor
    error_type = Column(String(100), nullable=False)  # timeout | parse_error | blocked | unknown
    error_message = Column(String(1000), nullable=True)
    traceback = Column(Text, nullable=True)  # full Python traceback
    attempted_at = Column(DateTime(timezone=False), nullable=False)
    created_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)

    __table_args__ = (
        Index("idx_scrape_errors_source_created", "source", "created_at"),
        Index("idx_scrape_errors_error_type", "error_type"),
    )
