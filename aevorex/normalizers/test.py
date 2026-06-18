"""Test normalizer — mock implementation."""

from typing import Any, Dict, Optional

from aevorex.normalizers.base import BaseNormalizer


class TestNormalizer(BaseNormalizer):
    """
    Test normalizer — maps mock test data to unified schema.
    """

    def __init__(self):
        """Initialize test normalizer."""
        super().__init__()

    async def normalize(self, platform_dict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Normalize test data to properties schema.
        """
        try:
            address = platform_dict.get("address")
            city = platform_dict.get("city")
            state = platform_dict.get("state")
            zip_code = platform_dict.get("zip_code")

            # Validate required fields
            if not all([address, city, state, zip_code]):
                return None

            normalized = {
                "test_id": platform_dict.get("test_id"),
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
                "listed_at": None,
                "primary_source": "test",
                "meta": platform_dict.get("meta", {}),
                "price_history": [],
                "property_images": [],
                "open_houses": [],
            }

            return normalized

        except Exception as e:
            print(f"Error normalizing test data: {e}")
            return None
