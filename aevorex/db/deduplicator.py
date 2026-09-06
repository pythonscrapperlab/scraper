"""Deduplication logic for matching and merging properties across sources."""

import re
from typing import Dict, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from aevorex.db.models import Property, utc_now

# Which platform a `primary_source` value refers to, highest priority first.
# Realtor's data is the most complete of the three, Zillow's the least, so a
# property confirmed by several platforms should present Realtor's view.
SOURCE_PRIORITY = {"realtor": 3, "redfin": 2, "zillow": 1}

# The three per-platform ID columns on Property, in the same priority order.
# `source_count` is derived from how many of these are populated — never
# incremented — see the note in upsert().
PLATFORM_ID_COLUMNS = {
    "realtor": "realtor_id",
    "redfin": "redfin_id",
    "zillow": "zillow_id",
}

# Where the last price each platform reported is kept, inside Property.meta.
# There is no per-source price column on Property (only the single winning
# `price`), so cross-source disagreement has nowhere else to live.
SOURCE_PRICES_META_KEY = "source_prices"

# Fields on the normalized-data dict that live in satellite tables, not as
# plain columns on Property. Never setattr these directly onto a Property
# instance. Populating the satellite tables themselves (schools, comps,
# POIs, transport stops, location score, features, price/tax history,
# images, open houses) is a separate step the caller does after upsert(),
# using the returned property_obj.id.
SATELLITE_KEYS = (
    "price_history",
    "tax_history",
    "property_images",
    "open_houses",
    "schools",
    "comps",
    "pois",
    "transport_stops",
    "location_score",
    "features",
    "market_snapshot",  # zip-level, not tied to this one property at all
)


class Deduplicator:
    """
    Handles matching, merging, and upserting properties.

    Strategy:
    1. Try to match by platform ID (zillow_id, redfin_id, realtor_id) —
       cheapest, exact, catches rescrapes of a listing you already have
       from this same source.
    2. If no match, try APN (Assessor Parcel Number), scoped to state —
       the strongest cross-source signal, since it identifies the
       physical parcel rather than a formatted address string. Not every
       source reliably exposes it, so this only fires when present.
    3. If no match, try normalized address + zip (fallback for when APN
       is missing).
    4. If no match, insert new property.
    5. If match found, update with new data and recalculate variance.
    """

    @staticmethod
    def normalize_address(address: str) -> str:
        """
        Normalize address for deduplication.

        - Lowercase
        - Remove extra whitespace
        - Remove common suffixes (#, Apt, Suite, etc.)
        - Remove punctuation
        """
        if not address:
            return ""

        address = address.lower().strip()
        address = re.sub(r'\s+', ' ', address)
        # Remove apartment numbers
        address = re.sub(r'#\d+', '', address)
        address = re.sub(r'\b(apt|suite|ste|unit|no|number)\s*\d+', '', address, flags=re.IGNORECASE)
        # Remove punctuation
        address = re.sub(r'[^\w\s]', '', address)
        return address.strip()

    @staticmethod
    def normalize_zip(zip_code: str) -> str:
        """Extract 5-digit zip code."""
        if not zip_code:
            return ""
        digits = re.sub(r'\D', '', zip_code)
        return digits[:5]

    @staticmethod
    def normalize_apn(apn: str) -> str:
        """
        Normalize APN for matching.

        Sources format the same parcel number differently (dashes, spaces,
        leading zeros) — e.g. "28-22-24-7566-02-130" vs "282224756602130".
        Strip everything but alphanumerics and uppercase, so the same
        physical parcel always normalizes to the same string regardless
        of source formatting.
        """
        if not apn:
            return ""
        return re.sub(r'[^A-Za-z0-9]', '', apn).upper()

    @staticmethod
    async def find_by_platform_id(
        session: AsyncSession, source: str, external_id: str
    ) -> Optional[Property]:
        """
        Find property by platform ID.

        Args:
            session: Async database session
            source: Platform (zillow | redfin | realtor)
            external_id: Platform's property ID

        Returns:
            Property or None
        """
        if not external_id:
            return None

        from sqlalchemy import select

        if source == "zillow":
            query = select(Property).where(Property.zillow_id == external_id)
        elif source == "redfin":
            query = select(Property).where(Property.redfin_id == external_id)
        elif source == "realtor":
            query = select(Property).where(Property.realtor_id == external_id)
        else:
            return None

        result = await session.execute(query)
        # .first() not .scalar_one_or_none(): duplicate Property rows sharing
        # the same redfin_id can exist from before the 'redfinscraper' vs
        # 'redfin' source bug was fixed (each buggy run never matched its own
        # earlier inserts). See the same fix in pipeline.py's satellite
        # upserts for the full explanation.
        return result.scalars().first()

    @staticmethod
    async def find_by_apn(
        session: AsyncSession, apn: str, state: Optional[str] = None
    ) -> Optional[Property]:
        """
        Find property by normalized APN (Assessor Parcel Number).

        APN is stored pre-normalized on Property, so this is a plain
        indexed equality lookup, not a client-side scan. APN formats
        aren't guaranteed unique nationwide (only within an assessor's
        jurisdiction), so scope by state as a cheap safety net against a
        coincidental collision between two different counties/states.

        Args:
            session: Async database session
            apn: Raw or normalized APN (will be normalized here regardless)
            state: 2-letter state code to scope the match, if known

        Returns:
            Property or None
        """
        norm_apn = Deduplicator.normalize_apn(apn)
        if not norm_apn:
            return None

        from sqlalchemy import select

        query = select(Property).where(Property.apn == norm_apn)
        if state:
            query = query.where(Property.state == state)

        result = await session.execute(query)
        return result.scalars().first()

    @staticmethod
    async def find_by_address(
        session: AsyncSession, address: str, zip_code: str
    ) -> Optional[Property]:
        """
        Find property by normalized address + zip.

        Args:
            session: Async database session
            address: Street address
            zip_code: Zip code

        Returns:
            Property or None
        """
        if not address or not zip_code:
            return None

        from sqlalchemy import select

        norm_addr = Deduplicator.normalize_address(address)
        norm_zip = Deduplicator.normalize_zip(zip_code)
        if not norm_addr or not norm_zip:
            return None

        # Match on the NORMALIZED zip, not the raw one. Stored zips are always
        # the 5-digit form, so a scrape arriving as "33401-1234" would compare
        # against nothing and silently insert a duplicate property.
        #
        # Still a Python-side scan of one zip's properties, because the stored
        # address isn't normalized in the column. That's bounded (a few hundred
        # rows per zip today) but it is the slow path in dedup — worth a
        # generated/normalized address column if this table grows an order of
        # magnitude.
        query = select(Property).where(
            Property.zip_code == norm_zip
        )

        result = await session.execute(query)
        candidates = result.scalars().all()

        # Filter by normalized address
        for prop in candidates:
            if Deduplicator.normalize_address(prop.address) == norm_addr: # type: ignore
                return prop

        return None

    @staticmethod
    async def find_existing(
        session: AsyncSession,
        source: str,
        external_id: Optional[str],
        apn: Optional[str],
        address: Optional[str],
        zip_code: Optional[str],
        state: Optional[str] = None,
    ) -> Optional[Property]:
        """
        Find existing property using multi-step strategy.

        1. Try platform ID match
        2. Try APN match
        3. Try address + zip match

        Args:
            session: Async database session
            source: Platform
            external_id: Platform property ID (optional)
            apn: Assessor Parcel Number (optional)
            address: Street address (optional)
            zip_code: Zip code (optional)
            state: 2-letter state code, used to scope the APN match (optional)

        Returns:
            Matching Property or None
        """
        # Step 1: Try platform ID
        if external_id:
            existing = await Deduplicator.find_by_platform_id(session, source, external_id)
            if existing:
                return existing

        # Step 2: Try APN
        if apn:
            existing = await Deduplicator.find_by_apn(session, apn, state)
            if existing:
                return existing

        # Step 3: Try address + zip
        if address and zip_code:
            existing = await Deduplicator.find_by_address(session, address, zip_code)
            if existing:
                return existing

        return None

    @staticmethod
    def calculate_price_variance(prices: list) -> Optional[float]:
        """
        Spread between the highest and lowest price, as a percentage of the
        lowest — i.e. how much the sources disagree about what this property
        is listed at.

        Returns None, not 0.0, when there aren't two prices to compare.
        0.0 means "every source reports the same number", which is a real and
        useful statement; returning it for a single-source property asserts a
        cross-source agreement that was never observed. This previously
        returned 0.0 in both cases, so the column read as "all sources agree"
        across a database that only ever had one source.
        """
        usable = [p for p in prices if p and p > 0]
        if len(usable) < 2:
            return None

        min_price = min(usable)
        max_price = max(usable)
        return round(((max_price - min_price) / min_price) * 100, 2)

    @staticmethod
    def _source_prices(meta: Optional[dict]) -> Dict[str, int]:
        """Read the per-platform price map out of Property.meta, defensively."""
        raw = (meta or {}).get(SOURCE_PRICES_META_KEY)
        if not isinstance(raw, dict):
            return {}
        prices: Dict[str, int] = {}
        for platform, value in raw.items():
            if platform not in SOURCE_PRIORITY:
                continue
            try:
                price = int(value)
            except (TypeError, ValueError):
                continue
            if price > 0:
                prices[platform] = price
        return prices

    @staticmethod
    def _count_sources(property_obj: Property) -> int:
        """
        How many platforms have actually contributed to this row.

        Derived from the populated platform-ID columns rather than counted up
        over time. The previous implementation did `source_count + 1` on every
        upsert, which counted *rescrapes* — a single-source database ended up
        with a third of its rows claiming three-source confirmation, and any
        confidence weighting built on the column inherited that.
        """
        return sum(
            1 for column in PLATFORM_ID_COLUMNS.values() if getattr(property_obj, column, None)
        ) or 1

    @staticmethod
    def _pick_primary_source(current: Optional[str], incoming: str) -> str:
        """
        Keep whichever of the two platforms ranks highest: Realtor > Redfin > Zillow.

        Without this, `primary_source` was simply whichever platform scraped
        most recently, so a Zillow rescrape would silently demote a property
        that Realtor had already provided better data for.
        """
        if not current:
            return incoming
        if SOURCE_PRIORITY.get(incoming, 0) > SOURCE_PRIORITY.get(current, 0):
            return incoming
        return current

    @staticmethod
    async def upsert(
        session: AsyncSession,
        source: str,
        normalized_data: dict,
    ) -> Tuple[Property, bool]:
        """
        Insert or update property, returning (property, is_new).

        Args:
            session: Async database session
            source: Platform (zillow | redfin | realtor)
            normalized_data: Normalized property dict from normalizer

        Returns:
            (Property object, is_new: bool) where is_new=True if inserted
        """
        # Normalize APN up front so both matching and storage use the same
        # canonical form, regardless of how this source formatted it.
        if normalized_data.get("apn"):
            normalized_data["apn"] = Deduplicator.normalize_apn(normalized_data["apn"])

        external_id = normalized_data.get(f"{source}_id")
        apn = normalized_data.get("apn")
        address = normalized_data.get("address")
        zip_code = normalized_data.get("zip_code")
        state = normalized_data.get("state")

        # Try to find existing
        existing = await Deduplicator.find_existing(
            session, source, external_id, apn, address, zip_code, state
        )

        if existing:
            # Update existing property
            is_new = False

            # Read the prior per-source price map before the update loop below
            # overwrites `meta` wholesale with this scrape's freshly built one.
            # Losing it here is what would make cross-source variance
            # uncomputable on the very next scrape.
            source_prices = Deduplicator._source_prices(existing.meta)

            # Set platform-specific ID
            if source == "zillow":
                existing.zillow_id = external_id or existing.zillow_id
            elif source == "redfin":
                existing.redfin_id = external_id or existing.redfin_id
            elif source == "realtor":
                existing.realtor_id = external_id or existing.realtor_id

            # Update fields (prefer non-None new values).
            # `primary_source` is deliberately excluded — it's resolved by
            # source priority below, not by whichever platform scraped last.
            for key, value in normalized_data.items():
                if value is None:
                    continue
                if key in ("zillow_id", "redfin_id", "realtor_id", "primary_source"):
                    continue
                if key in SATELLITE_KEYS:
                    continue
                setattr(existing, key, value)

            existing.primary_source = Deduplicator._pick_primary_source( # type: ignore
                existing.primary_source, source
            )

            # Record what THIS platform says the price is, then measure the
            # spread across platforms. Comparing `existing.price` against the
            # incoming price (as this used to) compares the property against
            # itself: the update loop above has already assigned the new price,
            # so both sides of the comparison were identical and the column was
            # 0.0 on every row. It also wouldn't have been cross-source
            # variance even when it worked — that's price movement over time,
            # which belongs in price_history.
            incoming_price = normalized_data.get("price")
            if incoming_price:
                source_prices[source] = int(incoming_price)
            if source_prices:
                new_meta = dict(existing.meta or {})
                new_meta[SOURCE_PRICES_META_KEY] = source_prices
                existing.meta = new_meta # type: ignore

            existing.price_variance = Deduplicator.calculate_price_variance( # type: ignore
                list(source_prices.values())
            )
            existing.source_count = Deduplicator._count_sources(existing) # type: ignore
            existing.last_seen_at = utc_now() # type: ignore

            # Flag for rescoring only if a scalar column genuinely changed
            # value — session.is_modified() does a real equality comparison
            # against the persisted baseline `existing` was loaded with
            # (via find_existing() above), not just "was setattr called," so
            # a no-op rescrape (identical data) correctly leaves the flag
            # alone. Satellite-table changes (price_history, tax_history,
            # comps, location_score, features) are handled separately in
            # PipelineRunner._upsert_satellites, since those are independent
            # rows, not attributes on this Property instance.
            if session.is_modified(existing, include_collections=False):
                existing.needs_analysis = True # type: ignore

            property_obj = existing

        else:
            # Create new property
            is_new = True

            # Remove satellite/computed fields from dict to pass to Property
            data_for_property = {
                k: v for k, v in normalized_data.items()
                if k not in SATELLITE_KEYS and k != "primary_source"
            }

            # Seed the per-source price map from the very first scrape, so the
            # second platform to arrive has something to disagree with.
            # price_variance itself stays NULL until there are two sources —
            # a single source can't disagree with anything.
            incoming_price = normalized_data.get("price")
            if incoming_price:
                meta = dict(data_for_property.get("meta") or {})
                meta[SOURCE_PRICES_META_KEY] = {source: int(incoming_price)}
                data_for_property["meta"] = meta

            property_obj = Property(
                primary_source=normalized_data.get("primary_source") or source,
                source_count=1,
                needs_analysis=True,
                **data_for_property
            )

            # Set platform ID
            if source == "zillow":
                property_obj.zillow_id = external_id # type: ignore
            elif source == "redfin":
                property_obj.redfin_id = external_id # type: ignore
            elif source == "realtor":
                property_obj.realtor_id = external_id # type: ignore

        session.add(property_obj)
        await session.flush()  # Ensure ID is generated
        return property_obj, is_new