"""Base scraper class that all platform scrapers inherit from."""
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
import html
import re
from dateutil import parser as dateutil_parser
import json


class BaseScraper(ABC):
    """
    Abstract base class for all scrapers.
    
    Each scraper must implement:
    - get_listing_urls(state) → list of URLs to scrape
    - fetch(url) → raw response (HTML or JSON)
    - parse(raw) → platform-specific dict
    """
    SOURCE: str = "unknown"   # e.g. "zillow" | "redfin" | "realtor"
    def __init__(self):
        """Initialize scraper."""
        self.logger = logging.getLogger(f"aevorex.scrapers.{self.SOURCE}")
        self.platform: str = self.__class__.__name__.lower()

    @abstractmethod
    async def get_listing_urls(self, state: str, city: str = "") -> List[str]:
        """
        Fetch all property listing URLs for a given state.
        
        Args:
            state: US state abbreviation (e.g., 'FL', 'CA')
            
        Returns:
            List of property URLs to scrape
        """
        pass

    @abstractmethod
    async def fetch(self, url: str) -> Optional[str | Dict[str, Any]]:
        """
        Fetch raw response (HTML or JSON) for a URL.
        
        Args:
            url: Property URL to fetch
            
        Returns:
            Raw HTML string, JSON dict, or None if failed
            
        Raises:
            Should catch exceptions internally and return None
        """
        pass

    @abstractmethod
    async def parse(self, raw: str | Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Parse raw response into platform-specific dict.
        
        Args:
            raw: Raw response from fetch()
            
        Returns:
            Dict with platform-specific fields, or None if parse failed
            
        Keys typically include:
            - external_id: platform's property ID
            - address, city, state, zip_code
            - price, bedrooms, bathrooms, sqft
            - property_type, listing_status, days_on_market
            - listing_url, description
            - photos, open_houses, price_history, tax_history
            - Any other platform-specific fields in 'meta'
        """
        pass

    async def scrape_batch(self, state: str, urls: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Scrape a batch of URLs for a state.
        
        Orchestrates: get_urls → fetch → parse → return results
        
        Args:
            state: State to scrape
            urls: Override list of URLs (optional). If None, fetch fresh.
            
        Returns:
            Dict with:
                - 'results': list of parsed property dicts
                - 'errors': list of error dicts with url, error_type, error_message, traceback
                - 'stats': dict with counts (success, failed, total)
        """
        results = {
            "results": [],
            "errors": [],
            "stats": {"success": 0, "failed": 0, "total": 0},
        }

        try:
            if urls is None:
                urls = await self.get_listing_urls(state)
            
            results["stats"]["total"] = len(urls)

            for url in urls[:2]:
                try:
                    raw = await self.fetch(url)
                    if raw is None:
                        results["stats"]["failed"] += 1
                        continue

                    parsed = await self.parse(raw)
                    if parsed is None:
                        results["stats"]["failed"] += 1
                        continue

                    results["results"].append(parsed)
                    results["stats"]["success"] += 1

                except Exception as e:
                    results["stats"]["failed"] += 1
                    results["errors"].append({
                        "url": url,
                        "error_type": type(e).__name__,
                        "error_message": str(e),
                    })

        except Exception as e:
            results["errors"].append({
                "error_type": "batch_error",
                "error_message": str(e),
            })

        return results

    def parse_date(self, value, ms=True) -> datetime | None:
        """
        Parses a date from either:
        - int/float timestamp (ms=True assumes milliseconds, like Redfin's eventDate)
        - string like 'Sep 19, 2019' or '2026-06-27' or 'Jun 27, 2026 1:30 AM'
        Returns a timezone-aware UTC datetime, or None if unparseable.
        """
        if value is None:
            return None

        # Numeric timestamp
        if isinstance(value, (int, float)):
            if value < 0:
                return None
            ts = value / 1000 if ms else value
            return datetime.fromtimestamp(ts, tz=timezone.utc)

        # String
        if isinstance(value, str):
            value = value.strip()
            if not value or value == "—":
                return None
            try:
                return dateutil_parser.parse(value, default=datetime(1900, 1, 1, tzinfo=timezone.utc))
            except (ValueError, OverflowError):
                return None

        return None

    def parse_price(self, price: str) -> float | None:
        """
        Convert any US-based price string to a float.
        
        Examples:
            "$1,027"        -> 1027.0
            "$1.2M"         -> 1200000.0
            "$500K"         -> 500000.0
            "$1,234.56"     -> 1234.56
            "$2.5B"         -> 2500000000.0
            "1,027"         -> 1027.0
            "$119,900"      -> 119900.0
            "Free"          -> None
        """
        if not price or not isinstance(price, str):
            return None

        cleaned = price.strip().upper().replace("$", "").replace(",", "").replace(" ", "")

        if not cleaned or not re.search(r'\d', cleaned):
            return None

        multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}

        match = re.fullmatch(r'([\d.]+)([KMB])?', cleaned)
        if not match:
            return None

        value = float(match.group(1))
        suffix = match.group(2)

        if suffix:
            value *= multipliers[suffix]

        return value

    def serialize_description(self, description: str) -> str:
        """Unescape HTML entities and clean up whitespace from a Redfin listing description."""
        if not description:
            return ""
        
        cleaned = html.unescape(description)
        cleaned = re.sub(r'\s+', ' ', cleaned)
        cleaned = cleaned.strip()
        
        return cleaned
