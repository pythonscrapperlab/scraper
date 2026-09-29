"""Realtor normalizer — maps Realtor.com JSON-LD to properties schema."""

import logging
from typing import Any, Dict, Optional

from aevorex.normalizers.base import BaseNormalizer

logger = logging.getLogger(__name__)


class RealtorNormalizer(BaseNormalizer):
    """
    Normalize Realtor.com JSON-LD and API data into properties schema.
    
    Maps Realtor-specific fields (mlsNumber, mls_id) to unified schema.
    Realtor has the most accurate MLS data.
    """

    def __init__(self):
        """Initialize Realtor normalizer."""
        super().__init__()

    async def normalize(self, platform_dict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Normalize Realtor data to properties schema.
        
        Args:
            platform_dict: Parsed Realtor JSON-LD response
            
        Returns:
            Normalized dict matching properties schema, or None
        """
        try:
            # TODO: Implement Realtor data normalization
            # Extract from JSON-LD schema and API response
            
            realtor_id = platform_dict.get("realtor_id")
            address = platform_dict.get("address")
            city = platform_dict.get("city")
            state = platform_dict.get("state")
            zip_code = platform_dict.get("zip_code")
            
            # Validate required fields
            if not all([address, city, state, zip_code]):
                return None
            
            normalized = {
                "realtor_id": realtor_id,
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
                "days_on_market_mls": platform_dict.get("days_on_market_mls"),
                "has_open": platform_dict.get("has_open"),
                "listing_url": platform_dict.get("listing_url"),
                "description": platform_dict.get("description"),
                "latitude": platform_dict.get("latitude"),
                "longitude": platform_dict.get("longitude"),
                "listed_at": platform_dict.get("listed_at"),
                "primary_source": "realtor",
                "meta": {
                    "mls_number": platform_dict.get("mls_number"),
                    "mls_id": platform_dict.get("mls_id"),
                    "source": "realtor",
                },
                "price_history": platform_dict.get("price_history", []),
                "property_images": platform_dict.get("property_images", []),
                "open_houses": platform_dict.get("open_houses", []),
            }
            
            return normalized
            
        except Exception as exc:
            logger.error("Realtor normalization failed: error_class=%s", type(exc).__name__)
            return None
