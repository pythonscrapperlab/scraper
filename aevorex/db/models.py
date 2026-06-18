"""
SQLAlchemy ORM models for Aevorex scraper.

All tables use UUIDs as primary keys, UTC timestamps, and JSONB for platform-specific data.
"""

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
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

    __table_args__ = (
        Index("idx_properties_address_zip", "address", "zip_code"),
        Index("idx_properties_state_city", "state", "city"),
        Index("idx_properties_created_at", "created_at"),
        Index("idx_properties_source_count", "source_count"),
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
