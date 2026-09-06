"""Base normalizer class for mapping platform-specific data to unified schema."""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
import logging
import re

logger = logging.getLogger("aevorex.normalizers.base")


class BaseNormalizer(ABC):
    """
    Abstract base class for all normalizers.
    
    Maps platform-specific scraped dicts into the unified properties schema.
    """

    def __init__(self):
        """Initialize normalizer."""
        self.platform: str = self.__class__.__name__.split("Normalizer")[0].lower()

    @staticmethod
    def normalize_address(address: str) -> str:
        """
        Normalize address string for deduplication.
        
        - Lowercase
        - Remove extra whitespace
        - Standardize directionals (N → North, etc.)
        - Remove punctuation
        """
        address = address.lower().strip()
        address = re.sub(r'\s+', ' ', address)
        address = re.sub(r'[^\w\s]', '', address)
        return address

    @staticmethod
    def normalize_zip(zip_code: str) -> str:
        """Normalize zip code (5 digits only)."""
        if not zip_code:
            return ""
        digits = re.sub(r'\D', '', zip_code)
        return digits[:5]

    @staticmethod
    def normalize_state(state: Optional[str]) -> Optional[str]:
        """
        Uppercase 2-letter state code.

        Sources are inconsistent about casing ("FL" / "Fl" / "fl" all appear
        in production data from the same scraper), and every state-scoped
        query and the APN dedup lookup are plain equality matches, so a
        single lowercased row silently falls out of all of them.
        """
        if not state:
            return None
        cleaned = re.sub(r'[^A-Za-z]', '', str(state)).upper()
        if len(cleaned) > 2:
            # Sources send the 2-letter code; a full state name would be
            # truncated to something wrong ("GEORGIA" -> "GE") rather than
            # translated, so make the guess visible instead of silent.
            logger.warning("Unexpected state value %r; storing %r.", state, cleaned[:2])
        return cleaned[:2] or None

    # Canonical listing-status slugs. The raw source string is always kept
    # alongside these (Property.listing_status) — this is the queryable form,
    # not a replacement for the source's own wording.
    LISTING_STATUS_RULES = (
        ("coming soon", "coming_soon"),
        ("contingent", "contingent"),
        ("backup", "contingent"),
        ("pending", "pending"),
        ("sold", "sold"),
        ("closed", "sold"),
        ("active", "active"),
        ("for sale", "active"),
        ("new", "new"),
    )

    @classmethod
    def normalize_listing_status(cls, status: Optional[str]) -> Optional[str]:
        """
        Map a source's MLS status to a canonical slug, or None.

        Handles the source's own formatting damage without editing it away:
        Redfin ships "Contingent- Accepting Backups" (note the missing space),
        which normalizes to "contingent" here while the malformed original
        stays in Property.listing_status.
        """
        if not status or not isinstance(status, str):
            return None
        normalized = " ".join(status.strip().lower().split())
        if not normalized:
            return None
        for needle, slug in cls.LISTING_STATUS_RULES:
            if needle in normalized:
                return slug
        return None

    @abstractmethod
    async def normalize(self, platform_dict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Map platform-specific dict to unified properties schema.
        
        Args:
            platform_dict: Raw output from scraper.parse()
            
        Returns:
            Dict matching properties table schema, or None if normalization failed
            
        Required fields to populate:
            - address, city, state, zip_code (for deduplication)
            - price, bedrooms, bathrooms, sqft, property_type, listing_status
            - listing_url, description (if available)
            - External ID: zillow_id, redfin_id, or realtor_id
            
        Optional fields:
            - latitude, longitude, lot_size, year_built, days_on_market
            - listed_at, meta (platform-specific extras)
            - price_history, tax_history, images, open_houses
        """
        pass

    async def normalize_batch(self, platform_dicts: list) -> Dict[str, Any]:
        """
        Normalize a batch of platform dicts.
        
        Returns:
            {
                'results': [normalized dicts],
                'errors': [error dicts],
                'stats': {'success': int, 'failed': int, 'total': int}
            }
        """
        results = {
            "results": [],
            "errors": [],
            "stats": {"success": 0, "failed": 0, "total": len(platform_dicts)},
        }

        for item in platform_dicts:
            try:
                normalized = await self.normalize(item)
                if normalized:
                    results["results"].append(normalized)
                    results["stats"]["success"] += 1
                else:
                    results["stats"]["failed"] += 1
                    results["errors"].append({
                        "item": item,
                        "error": "normalize() returned None",
                    })
            except Exception as e:
                results["stats"]["failed"] += 1
                results["errors"].append({
                    "item": item,
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                })

        return results
