"""Redfin scraper — uses internal Redfin API endpoints with httpx."""

from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

from aevorex.config import settings
from aevorex.scrapers.base import BaseScraper
from aevorex.transport.http_client import HttpClient
from aevorex.transport.proxy_manager import ProxyManager
from aevorex.scrapers.constants import REDFIN
import math
import asyncio

class RedfinScraper(BaseScraper):
    """
    Redfin scraper using internal API endpoints.

    Redfin exposes a GIS API that returns property listings as JSON.
    """

    SOURCE = "redfin"

    # Tune based on Redfin tolerance — webshare's rotating gateway handles
    # IP diversity, this just caps how many requests are in flight at once.
    MAX_CONCURRENT_PAGE_FETCHES = 8

    def __init__(self, proxy_manager: ProxyManager | None = None):
        """Initialize Redfin scraper."""
        super().__init__()
        self.base_url = settings.redfin_base_url
        self.api_url = settings.redfin_api_url

        self.proxy_manager = proxy_manager or ProxyManager()
        if not proxy_manager:
            self.proxy_manager.provider = "webshare"
            if self.proxy_manager.user and self.proxy_manager.password:
                self.proxy_manager.enabled = True

        self.base_filters = "/filter/sort=lo-days,property-type=house+condo+townhouse+multifamily,max-year-built=2024,max-days-on-market=2mo,include=forsale+mlsfsbo+fsbo,exclude-short-sale,exclude-age-restricted,exclude-land-lease/page-[PAGE]"

    def _new_client(self) -> HttpClient:
        """Create an HttpClient bound to webshare's rotating gateway (or no proxy)."""
        proxy = self.proxy_manager.get_rotating_proxy_url()
        if proxy:
            return HttpClient(proxy=proxy)
        return HttpClient()

    def get_state_urls(self, state: str = "", city: str = "") -> List[str]:
        """
        Fetch listing URLs for a state.

        Redfin search: /homes/for_sale/{state}/
        """
        self.logger.debug(f"Redfin URL mapping: {REDFIN}")
        urls = []
        if state and city:
            redfin_id = REDFIN.get(state, {}).get(city, 0)
            if redfin_id:
                urls.append(f"https://www.redfin.com/city/{redfin_id}/{state}/{city}")
        if state and not city:
            redfin_state = REDFIN.get(state)
            if not redfin_state:
                return []
            for city, redfin_id in redfin_state.items():
                urls.append(f"https://www.redfin.com/city/{redfin_id}/{state}/{city}")
        if not state and not city:
            for state, cities in REDFIN.items():
                for city, redfin_id in cities.items():
                    urls.append(f"https://www.redfin.com/city/{redfin_id}/{state}/{city}")
        return urls

    def _parse_homecards(self, html: str) -> List[str]:
        soup = BeautifulSoup(html, "html.parser")  # type: ignore
        return [
            self.base_url + a["href"]
            for a in soup.findAll("a", {"class": "bp-Homecard__Address"})
            if a.get("href")
        ]

    async def _fetch_page(self, client: HttpClient, url: str, page: int) -> List[str]:
        """Fetch and parse a single results page using a shared client."""
        page_url = f"{url}{self.base_filters}".replace("[PAGE]", str(page))
        self.logger.info(f"Fetching Redfin Property Listing page {page} ...")
        try:
            res = await client.get_text(page_url)
            return self._parse_homecards(res)  # type: ignore
        except Exception as e:
            self.logger.warning(f"Failed to fetch page {page} for {url}: {e}")
            return []

    async def get_property_urls(self, url: str) -> List[str]:
        """
        Fetch all property URLs for a given Redfin search/listing URL,
        paginating concurrently across remaining pages using one shared client.
        """
        client = self._new_client()
        try:
            first_page_url = f"{url}{self.base_filters}".replace("[PAGE]", "1")
            res = await client.get_text(first_page_url)
            soup = BeautifulSoup(res, "html.parser")  # type: ignore

            property_urls = [
                self.base_url + a["href"]
                for a in soup.findAll("a", {"class": "bp-Homecard__Address"})
                if a.get("href")
            ]

            try:
                total_properties = int(
                    soup.find("div", {"data-rf-test-id": "homes-description"})
                    .text.split(" ")[0] # type: ignore
                    .replace(",", "")
                )  # type: ignore
                total_pages = math.ceil(total_properties / 40)
            except Exception:
                total_pages = 1

            if total_pages < 2:
                return property_urls

            semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_PAGE_FETCHES)

            async def bounded_fetch(page: int) -> List[str]:
                async with semaphore:
                    return await self._fetch_page(client, url, page)

            tasks = [bounded_fetch(page) for page in range(2, total_pages + 1)]
            results = await asyncio.gather(*tasks)

            for page_urls in results:
                property_urls.extend(page_urls)

            return property_urls
        finally:
            await client.close()

    async def get_listing_urls(self, state: str, city:str="") -> List[str]:
        """
        Fetch listing URLs for a state and city.
        
        Redfin search: /homes/for_sale/{state}/
        """
        urls = self.get_state_urls(state, city)
        property_urls = []
        for url in urls:
            self.logger.info(f"Fetching property URLs from: {url}")
            new_urls = await self.get_property_urls(url)
            property_urls.extend(new_urls)
        self.logger.info(f"Found {len(property_urls)} property URLs for {state}, {city}")
        # Use httpx to fetch and parse
        client = HttpClient()
        try:
            # Fetch search page HTML
            # Extract URLs from property listings
            pass
        except Exception as e:
            print(f"Error fetching listing URLs: {e}")
        finally:
            await client.close()
        print(property_urls)
        raise NotImplementedError("get_listing_urls not implemented yet")
        return urls

    async def fetch(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Fetch property page from Redfin.
        
        Returns raw HTML or JSON from API.
        
        Args:
            url: Property URL
            
        Returns:
            Raw response (HTML string or JSON dict) or None
        """
        client = HttpClient()
        try:
            # Check if it's an API endpoint or regular property page
            if "/gis" in url:
                # API endpoint — return JSON
                response = await client.get_json(url)
                return response
            else:
                # Property page — return HTML
                response = await client.get_text(url)
                return response # type: ignore
        except Exception as e:
            print(f"Error fetching {url}: {e}")
            return None
        finally:
            await client.close()

    async def parse(self, raw: str | Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Parse Redfin response into platform-specific dict.
        
        Args:
            raw: HTML string or JSON dict
            
        Returns:
            Dict with property fields
        """
        try:
            # TODO: Implement Redfin parsing
            
            parsed = {
                "redfin_id": None,
                "external_id": None,
                "address": None,
                "city": None,
                "state": None,
                "zip_code": None,
                "price": None,
                "bedrooms": None,
                "bathrooms": None,
                "sqft": None,
                "lot_size": None,
                "year_built": None,
                "property_type": None,
                "listing_status": None,
                "days_on_market": None,
                "listing_url": None,
                "description": None,
                "latitude": None,
                "longitude": None,
                "meta": {},
                "price_history": [],
                "property_images": [],
                "open_houses": [],
            }
            
            return parsed
        except Exception as e:
            print(f"Error parsing Redfin data: {e}")
            return None
