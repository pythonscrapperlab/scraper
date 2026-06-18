"""Deduplication logic for matching and merging properties across sources."""

import re
from typing import Optional, Tuple

from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from aevorex.db.models import Property


class Deduplicator:
    """
    Handles matching, merging, and upserting properties.
    
    Strategy:
    1. Try to match by platform ID (zillow_id, redfin_id, realtor_id)
    2. If no match, try normalized address + zip
    3. If no match, insert new property
    4. If match found, update with new data and recalculate variance
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
        return result.scalar_one_or_none()

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

        from sqlalchemy import select, func

        norm_addr = Deduplicator.normalize_address(address)
        norm_zip = Deduplicator.normalize_zip(zip_code)

        # Query: Find matching properties by comparing normalized values
        # Fetch all properties with matching zip, then normalize in Python
        query = select(Property).where(
            Property.zip_code == zip_code
        )

        result = await session.execute(query)
        candidates = result.scalars().all()

        # Filter by normalized address
        for prop in candidates:
            if Deduplicator.normalize_address(prop.address) == norm_addr:
                return prop

        return None

    @staticmethod
    async def find_existing(
        session: AsyncSession,
        source: str,
        external_id: Optional[str],
        address: Optional[str],
        zip_code: Optional[str],
    ) -> Optional[Property]:
        """
        Find existing property using multi-step strategy.
        
        1. Try platform ID match
        2. Try address + zip match
        
        Args:
            session: Async database session
            source: Platform
            external_id: Platform property ID (optional)
            address: Street address (optional)
            zip_code: Zip code (optional)
            
        Returns:
            Matching Property or None
        """
        # Step 1: Try platform ID
        if external_id:
            existing = await Deduplicator.find_by_platform_id(session, source, external_id)
            if existing:
                return existing

        # Step 2: Try address + zip
        if address and zip_code:
            existing = await Deduplicator.find_by_address(session, address, zip_code)
            if existing:
                return existing

        return None

    @staticmethod
    def calculate_price_variance(prices: list) -> float:
        """
        Calculate price variance percentage.
        
        Returns: max(prices) / min(prices) * 100 - 100 (percent difference)
        """
        if len(prices) < 2:
            return 0.0

        prices = [p for p in prices if p and p > 0]
        if len(prices) < 2:
            return 0.0

        min_price = min(prices)
        max_price = max(prices)
        variance = ((max_price - min_price) / min_price) * 100
        return round(variance, 2)

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
        external_id = normalized_data.get(f"{source}_id")
        address = normalized_data.get("address")
        zip_code = normalized_data.get("zip_code")

        # Try to find existing
        existing = await Deduplicator.find_existing(
            session, source, external_id, address, zip_code
        )

        if existing:
            # Update existing property
            is_new = False

            # Set platform-specific ID
            if source == "zillow":
                existing.zillow_id = external_id or existing.zillow_id
            elif source == "redfin":
                existing.redfin_id = external_id or existing.redfin_id
            elif source == "realtor":
                existing.realtor_id = external_id or existing.realtor_id

            # Update fields (prefer non-None new values)
            for key, value in normalized_data.items():
                if value is not None and key not in ("zillow_id", "redfin_id", "realtor_id"):
                    if key not in ("price_history", "tax_history", "property_images", "open_houses"):
                        setattr(existing, key, value)

            # Recalculate price variance
            prices = [
                existing.price,
                normalized_data.get("price"),
            ]
            prices = [p for p in prices if p]
            if prices:
                existing.price_variance = Deduplicator.calculate_price_variance(prices)

            # Increment source count
            existing.source_count = min(existing.source_count + 1, 3)

            property_obj = existing

        else:
            # Create new property
            is_new = True

            # Remove satellite/computed fields from dict to pass to Property
            data_for_property = {
                k: v for k, v in normalized_data.items()
                if k not in ("price_history", "tax_history", "property_images", "open_houses", "primary_source")
            }

            property_obj = Property(
                primary_source=normalized_data.get("primary_source") or source,
                source_count=1,
                **data_for_property
            )

            # Set platform ID
            if source == "zillow":
                property_obj.zillow_id = external_id
            elif source == "redfin":
                property_obj.redfin_id = external_id
            elif source == "realtor":
                property_obj.realtor_id = external_id

        session.add(property_obj)
        await session.flush()  # Ensure ID is generated
        return property_obj, is_new

    @staticmethod
    def _pick_primary_source(source: str) -> str:
        """
        Pick primary source based on priority.
        
        Priority: Realtor > Redfin > Zillow
        """
        priority = {"realtor": 3, "redfin": 2, "zillow": 1}
        return source  # For now, use the current source (can enhance later to compare all)
