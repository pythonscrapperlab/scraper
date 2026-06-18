"""Test scraper — mock implementation to verify pipeline works."""

from typing import Any, Dict, List, Optional

from aevorex.scrapers.base import BaseScraper


class TestScraper(BaseScraper):
    """
    Test scraper with mock data — no actual network calls.
    
    Used to verify the entire pipeline works before implementing real scrapers.
    """

    def __init__(self):
        """Initialize test scraper."""
        super().__init__()

    async def get_listing_urls(self, state: str) -> List[str]:
        """Return mock URLs."""
        # Return 3 mock URLs for testing
        return [
            f"https://example.com/property-{i}" 
            for i in range(1, 4)
        ]

    async def fetch(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Return mock property data (no actual HTTP call).
        """
        # Mock property data
        mock_data = {
            "property_1": {
                "external_id": "TEST_001",
                "address": "123 Main St",
                "city": "Miami",
                "state": "FL",
                "zip_code": "33101",
                "price": 350000,
                "bedrooms": 3,
                "bathrooms": 2.0,
                "sqft": 1500,
                "lot_size": 0.25,
                "year_built": 2010,
                "property_type": "house",
                "listing_status": "for_sale",
                "days_on_market": 5,
                "listing_url": url,
                "description": "Beautiful property in Miami",
                "latitude": 25.7617,
                "longitude": -80.1918,
            },
            "property_2": {
                "external_id": "TEST_002",
                "address": "456 Oak Ave",
                "city": "Los Angeles",
                "state": "CA",
                "zip_code": "90001",
                "price": 750000,
                "bedrooms": 4,
                "bathrooms": 3.0,
                "sqft": 2500,
                "lot_size": 0.5,
                "year_built": 2015,
                "property_type": "house",
                "listing_status": "pending",
                "days_on_market": 12,
                "listing_url": url,
                "description": "Modern home in LA",
                "latitude": 34.0522,
                "longitude": -118.2437,
            },
            "property_3": {
                "external_id": "TEST_003",
                "address": "789 Pine Rd",
                "city": "Miami Beach",
                "state": "FL",
                "zip_code": "33139",
                "price": 1200000,
                "bedrooms": 2,
                "bathrooms": 2.0,
                "sqft": 1200,
                "lot_size": 0.1,
                "year_built": 2020,
                "property_type": "condo",
                "listing_status": "for_sale",
                "days_on_market": 2,
                "listing_url": url,
                "description": "Luxury condo with ocean view",
                "latitude": 25.7907,
                "longitude": -80.1300,
            },
        }

        # Return different property based on URL
        if "property-1" in url:
            return mock_data["property_1"]
        elif "property-2" in url:
            return mock_data["property_2"]
        else:
            return mock_data["property_3"]

    async def parse(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Parse mock data — just return as-is since it's already formatted.
        """
        if not raw:
            return None
        
        return {
            "test_id": raw.get("external_id"),
            "address": raw.get("address"),
            "city": raw.get("city"),
            "state": raw.get("state"),
            "zip_code": raw.get("zip_code"),
            "price": raw.get("price"),
            "bedrooms": raw.get("bedrooms"),
            "bathrooms": raw.get("bathrooms"),
            "sqft": raw.get("sqft"),
            "lot_size": raw.get("lot_size"),
            "year_built": raw.get("year_built"),
            "property_type": raw.get("property_type"),
            "listing_status": raw.get("listing_status"),
            "days_on_market": raw.get("days_on_market"),
            "listing_url": raw.get("listing_url"),
            "description": raw.get("description"),
            "latitude": raw.get("latitude"),
            "longitude": raw.get("longitude"),
            "meta": {
                "source": "test",
                "mock_data": True,
            },
        }
