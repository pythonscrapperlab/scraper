"""Redfin normalizer — maps Redfin API data to properties schema."""

from typing import Any, Dict, Optional

from aevorex.normalizers.base import BaseNormalizer


class RedfinNormalizer(BaseNormalizer):
    """
    Normalize Redfin API response into properties schema.
    
    Maps Redfin-specific fields (redfinID, redfin_estimate) to unified schema.
    """

    def __init__(self):
        """Initialize Redfin normalizer."""
        super().__init__()

    async def normalize(self, platform_dict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Normalize Redfin data to properties schema.
        
        Args:
            platform_dict: Parsed Redfin API response
            
        Returns:
            Normalized dict matching properties schema, or None
        """
        try:
            # TODO: Implement Redfin data normalization
            # Extract from GIS API response and map fields
            
            redfin_id = platform_dict.get("redfin_id")
            address = platform_dict.get("address")
            city = platform_dict.get("city")
            state = platform_dict.get("state")
            zip_code = platform_dict.get("zip_code")
            
            # Validate required fields
            if not all([address, city, state, zip_code]):
                return None
            
            normalized = {
                "redfin_id": redfin_id,
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
                "primary_source": "redfin",
                "meta": {
                    "redfin_estimate": platform_dict.get("redfin_estimate"),
                    "redfin_estimate_range": platform_dict.get("redfin_estimate_range"),
                    "source": "redfin",
                },
                "price_history": platform_dict.get("price_history", []),
                "property_images": platform_dict.get("property_images", []),
                "open_houses": platform_dict.get("open_houses", []),
            }
            
            return normalized
            
        except Exception as e:
            print(f"Error normalizing Redfin data: {e}")
            return None
