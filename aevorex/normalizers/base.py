"""Base normalizer class for mapping platform-specific data to unified schema."""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
import re


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
