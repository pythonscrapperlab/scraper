"""Zillow normalizer — maps Zillow __NEXT_DATA__ to properties schema."""

import logging
from typing import Any, Dict, Optional

from aevorex.normalizers.base import BaseNormalizer

logger = logging.getLogger(__name__)


class ZillowNormalizer(BaseNormalizer):
    """
    Normalize Zillow __NEXT_DATA__ JSON into properties schema.
    
    Maps Zillow-specific fields (zestimate, zillowID) to unified schema.
    """

    def __init__(self):
        """Initialize Zillow normalizer."""
        super().__init__()

    async def normalize(self, platform_dict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Normalize Zillow data to properties schema.
        
        Args:
            platform_dict: Parsed Zillow __NEXT_DATA__
            
        Returns:
            Normalized dict matching properties schema, or None
        """
        try:
            # TODO: Implement Zillow data normalization
            # Extract from __NEXT_DATA__ structure and map fields
            
            # Example mapping:
            zillow_id = platform_dict.get("zillow_id")
            address = platform_dict.get("address")
            city = platform_dict.get("city")
            state = platform_dict.get("state")
            zip_code = platform_dict.get("zip_code")
            
            # Validate required fields
            if not all([address, city, state, zip_code]):
                return None
            
            normalized = {
                "zillow_id": zillow_id,
                "address": self.normalize_address(address),
                "city": city,
                "state": state,
                "zip_code": self.normalize_zip(zip_code),
                "price": platform_dict.get("price"),
                "bedrooms": platform_dict.get("bedrooms"),
                "bathrooms": platform_dict.get("bathrooms"),
                "sqft": platform_dict.get("sqft"),
                "lot_size": platform_dict.get("lot_size"),
                "year_built": platform_dict.get("year_built"),
                "property_type": platform_dict.get("property_type"),
                "listing_status": platform_dict.get("listing_status"),
                "days_on_market": platform_dict.get("days_on_market"),
                "listing_url": platform_dict.get("listing_url"),
                "description": platform_dict.get("description"),
                "latitude": platform_dict.get("latitude"),
                "longitude": platform_dict.get("longitude"),
                "listed_at": platform_dict.get("listed_at"),
                "primary_source": "zillow",
                "meta": {
                    "zestimate": platform_dict.get("zestimate"),
                    "zestimate_range": platform_dict.get("zestimate_range"),
                    "tax_value": platform_dict.get("tax_value"),
                    "source": "zillow",
                },
                "price_history": platform_dict.get("price_history", []),
                "property_images": platform_dict.get("property_images", []),
                "open_houses": platform_dict.get("open_houses", []),
            }
            
            return normalized
            
        except Exception as exc:
            logger.error("Zillow normalization failed: error_class=%s", type(exc).__name__)
            return None
