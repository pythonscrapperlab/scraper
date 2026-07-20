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
    UUID,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def utc_now() -> datetime:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
    listing_status = Column(String(50), nullable=True, index=True)  # for_sale | pending | sold
    days_on_market = Column(Integer, nullable=True)

    # Cross-platform variance
    price_variance = Column(Float, nullable=True)  # % difference across sources
    source_count = Column(Integer, nullable=False, default=1)  # 1 to 3

    listing_url = Column(String(2048), nullable=True)
    description = Column(Text, nullable=True)

    # Platform-specific fields and conflicts stored here
    meta = Column(JSON, nullable=True)  # zestimate | redfin_estimate | mls_number | walk_score | hoa_fee | discrepancies

    listed_at = Column(DateTime(timezone=False), nullable=True)
    last_seen_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)
    created_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=False), nullable=False, default=utc_now, onupdate=utc_now)

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

    __table_args__ = (
        Index("idx_properties_address_zip", "address", "zip_code"),
        Index("idx_properties_state_city", "state", "city"),
        Index("idx_properties_created_at", "created_at"),
        Index("idx_properties_source_count", "source_count"),
        Index("idx_properties_apn_state", "apn", "state"),
    )


class PriceHistory(Base):
    """Tracks price changes across sources."""

    __tablename__ = "price_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    property_id = Column(UUID(as_uuid=True), ForeignKey("properties.id"), nullable=False, index=True)
    source = Column(String(50), nullable=False, index=True)  # zillow | redfin | realtor
    price = Column(Integer, nullable=False)
    event = Column(String(50), nullable=False)  # listed | reduced | relisted | sold
    event_date = Column(DateTime(timezone=False), nullable=False)
    created_at = Column(DateTime(timezone=False), nullable=False, default=utc_now)

    # Relationships
    property = relationship("Property", back_populates="price_history")

    __table_args__ = (
        Index("idx_price_history_property_source", "property_id", "source"),
        Index("idx_price_history_event_date", "event_date"),
    )


class TaxHistory(Base):
    """Tracks tax assessments across sources."""

    __tablename__ = "tax_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    property_id = Column(UUID(as_uuid=True), ForeignKey("properties.id"), nullable=False, index=True)
    source = Column(String(50), nullable=False, index=True)  # zillow | redfin | realtor
    tax_year = Column(Integer, nullable=False)
    tax_amount = Column(Integer, nullable=True)
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
    category = Column(String(50), nullable=True)  # nullable until you build a categorizer
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