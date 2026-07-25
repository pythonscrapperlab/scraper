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
from datetime import datetime, timezone
import html
import re
from dateutil import parser as dateutil_parser
import json

class RedfinScraper(BaseScraper):
    """
    Redfin scraper using internal API endpoints.

    Redfin exposes a GIS API that returns property listings as JSON.
    """

    SOURCE = "redfin"
    platform = "redfin"

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
        proxy = self.proxy_manager.get_rotating_proxy_url_dataimpulse()
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
        soup = BeautifulSoup(html, "lxml")  # type: ignore
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
            soup = BeautifulSoup(res, "lxml")  # type: ignore

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
            
            if total_pages >30:
                self.logger.warning(f"Redfin search {url} has {total_pages} pages, which exceeds the limit of 20. Only fetching the first 20 pages.")
                total_pages = 30
            
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
        
        return property_urls

    async def fetch(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Fetch property page from Redfin.
        
        Returns raw HTML or JSON from API.
        
        Args:
            url: Property URL
            
        Returns:
            Raw response (HTML string or JSON dict) or None
        """
        headers = {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
            'accept-language': 'en-US,en;q=0.9',
            'cache-control': 'max-age=0',
            'dnt': '1',
            'priority': 'u=0, i',
            'sec-ch-ua': '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'same-origin',
            'sec-fetch-user': '?1',
            'upgrade-insecure-requests': '1',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36',
        }
        client = self._new_client()
        try:
            # Property page — return HTML
            self.logger.info(f"Fetching Redfin property page: {url}")
            response = await client.get_text(url, headers=headers)
            return response # type: ignore
        except Exception as e:
            print(f"Error fetching {url}: {e}")
            return None
        finally:
            await client.close()

    async def parse(self, raw: str) -> Optional[Dict[str, Any]]:
        """
        Parse Redfin response into platform-specific dict.
        
        Args:
            raw: HTML string or JSON dict
            
        Returns:
            Dict with property fields
        """
        # lxml is a C-based parser and meaningfully faster than html.parser on
        # pages this large — Redfin's property pages run several hundred KB
        # to a few MB with all the embedded scripts/JSON.
        soup = BeautifulSoup(raw, 'lxml')
        try:
            script = [i for i in soup.findAll("script", {"type":"application/ld+json"}) if "RealEstateListing" in i.text][0]
            data = json.loads(script.text)
        except:
            data = {}

        redfin_id = data.get("url", "").split("/")[-1]
        primary_source = "redfin"

        potentialAction = data.get("potentialAction", {}).get("@type", "")

        mainEntity = data.get("mainEntity", {})
        mainEntityAddress = mainEntity.get("address", {})
        street_address = mainEntityAddress.get("streetAddress", "")
        city = mainEntityAddress.get("addressLocality", "")
        state = mainEntityAddress.get("addressRegion", "")
        zip_code = mainEntityAddress.get("postalCode", "")
        address = f"{street_address}, {city}, {state}"

        mainEntityGeo = mainEntity.get("geo", {})
        latitude = mainEntityGeo.get("latitude", "")
        longitude = mainEntityGeo.get("longitude", "")

        price = data.get("offers", {}).get("price", "")
        bedrooms = mainEntity.get("numberOfBedrooms", "")
        bathrooms = mainEntity.get("numberOfBathroomsTotal", "")
        sqft = mainEntity.get("floorSize", {}).get("value", "")
        year_built = mainEntity.get("yearBuilt", "")

        property_type = mainEntity.get("accommodationCategory", "")
        listing_status = data.get("offers", {}).get("availability", "").split("/")[-1] if data.get("offers", {}).get("availability", "") else ""
        days_on_market = 0

        listing_url = data.get("url", "")
        description = self.serialize_description(data.get("description", ""))

        listed_at = datetime.fromisoformat(data.get("datePosted", "")) if data.get("datePosted", "") else "" #2026-06-13T04:02:06.452Z
        last_seen_at = datetime.fromisoformat(data.get("lastReviewed", "")) if data.get("lastReviewed", "") else "" #2026-06-13T04:02:06.452Z

        property_images = [i.get("url") for i in mainEntity.get("image", []) if i.get("url")]

        monthly_payment = soup.find("span", {"class":"monthly-payment-amount"})

        property_data = {
            "redfin_id": redfin_id,
            "primary_source": primary_source,
            "potentialAction": potentialAction,
            "address": address,
            "street_address": street_address,
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "latitude": latitude,
            "longitude": longitude,
            "price": price,
            "bedrooms": bedrooms,
            "bathrooms": bathrooms,
            "sqft": sqft,
            "year_built": year_built,
            "property_type": property_type,
            "listing_status": listing_status,
            "days_on_market": days_on_market,
            "listing_url": listing_url,
            "description": description,
            "listed_at": listed_at,
            "last_seen_at": last_seen_at,
            "monthly_payment": self.parse_price(monthly_payment.text) if monthly_payment else "",
            "property_images": property_images,
        }
        # Pass the ORIGINAL raw HTML, not a re-serialized str(soup). soup was
        # only needed above for the JSON-LD script tag and the monthly-payment
        # span; extract_redfin_data() below does nothing but a regex search
        # over the page text, so rebuilding the whole DOM back into a string
        # (str(soup), which walks and re-renders every node) was pure waste —
        # on a page this size, that rebuild was almost certainly the single
        # biggest cost in this function.
        property_data = property_data | await self.extract_redfin_data(raw)
        
        return property_data
    
    async def extract_redfin_data(self, html: str) -> dict:
        """
        Extract all property data from Redfin's __reactServerState.InitialContext.
        Returns a flat dict of the most useful fields.
        """
        # Extract the JSON blob from the JS assignment
        match = re.search(
            r'__reactServerState\.InitialContext\s*=\s*(\{.*?\});?\s*\n',
            html,
            re.DOTALL
        )
        if not match:
            raise ValueError("Could not find InitialContext in HTML")

        raw = json.loads(match.group(1))
        # return raw
        
        cache = raw.get("ReactServerAgent.cache", {}).get("dataCache", {})
        # return cache
        def get_payload(key):
            entry = cache.get(key, {})
            res_text = entry.get("res", {}).get("text", "")
            if not res_text:
                return {}
            # Redfin prepends "{}&&" to JSON responses
            clean = res_text.replace("{}&&", "").strip()
            try:
                return json.loads(clean).get("payload", {})
            except json.JSONDecodeError:
                return {}
        
        # Pull from the relevant endpoints
        above_fold   = get_payload("/stingray/api/home/details/aboveTheFold")
        below_fold   = get_payload("/stingray/api/v1/home/details/belowTheFold")
        main_info    = get_payload("/stingray/api/home/details/mainHouseInfoPanelInfo")
        avm          = get_payload("/stingray/api/home/details/avm")
        rental_est   = get_payload("/stingray/api/home/details/rental-estimate")
        risk_factors = get_payload("/stingray/api/v1/home/details/belowTheFold/riskFactorData")
        ai_summary   = get_payload("/stingray/api/home/details/aiSummary")
        schools      = get_payload("/stingray/api/v1/home/details/belowTheFold/schoolsAndDistrictsInfo")
        region_info  = get_payload("/stingray/api/region/shared-region-info")
        walk_score   = get_payload("/stingray/api/home/details/neighborhoodStats/statsInfo")
        offer_ins    = get_payload("/stingray/api/home/details/offerInsights")
        history      = get_payload("/stingray/api/v1/home/details/belowTheFold")
        location     = get_payload("/stingray/api/v1/home/details/location-score")
        around_home  = get_payload("/stingray/api/home/details/aroundThisHomeSectionInfo")
        
        addr = above_fold.get("addressSectionInfo", {})
        pub  = below_fold.get("publicRecordsInfo", {})
        main = main_info.get("mainHouseInfo", {})
        rent = rental_est.get("rentalEstimateInfo", {})
        trends = region_info.get("trendsData", {})
        amenities     = history.get("amenitiesInfo", {})
        pub_history           = history.get("publicRecordsInfo", {})
        prop_history  = history.get("propertyHistoryInfo", {})
        
        # --- Property core ---
        result = {
            "address":          addr.get("assembledAddress") or main.get("streetAddress"),
            "full_address":     main.get("fullStreetAddress"),
            "city":             addr.get("city"),
            "state":            addr.get("state"),
            "zip":              addr.get("zip"),
            "latitude":         addr.get("latLong", {}).get("latitude"),
            "longitude":        addr.get("latLong", {}).get("longitude"),

            # Pricing
            "list_price":       addr.get("priceInfo", {}).get("amount"),
            "price_per_sqft":   addr.get("pricePerSqFt"),
            "avm_value":        avm.get("predictedValue"),
            "rental_est_low":   rent.get("predictedValueLow"),
            "rental_est_high":  rent.get("predictedValueHigh"),
            "rental_est_mid":   rent.get("predictedValue"),

            # Property details
            "beds":             addr.get("beds"),
            "baths":            addr.get("baths"),
            "sqft":             addr.get("sqFt", {}).get("value"),
            "year_built":       addr.get("yearBuilt"),
            "property_type":    addr.get("propertyType"),
            "status":           addr.get("status", {}).get("displayValue"),
            "mls_id":           main.get("mlsId"),
            "days_on_market":   addr.get("cumulativeDaysOnMarket"),
            "listing_agent":    main.get("listingAgents", [{}])[0].get("agentInfo", {}).get("agentName"),

            # HOA / Fees
            "hoa_monthly":      next(
                (a.get("content", "").replace("$","").replace("/mo","").strip()
                for a in main.get("selectedAmenities", [])
                if a.get("header") == "HOA Dues"), None
            ),

            # Tax
            "tax_annual":       pub.get("taxInfo", {}).get("taxesDue"),
            "tax_year":         pub.get("taxInfo", {}).get("rollYear"),

            # Market trends (zip level)
            "median_list_price":    trends.get("medianListPrice"),
            "median_sale_price":    trends.get("medianSalePrice"),
            "avg_days_on_market":   trends.get("avgDaysOnMarket"),
            "sale_to_list_pct":     trends.get("medianSalePerList"),
            "yoy_sale_price":       trends.get("yoySalePrice"),

            # Walk/Transit scores
            "walk_score":   walk_score.get("walkScoreInfo", {}).get("walkScoreData", {}).get("walkScore", {}).get("value"),
            "transit_score":walk_score.get("walkScoreInfo", {}).get("walkScoreData", {}).get("transitScore", {}).get("value"),
            "bike_score":   walk_score.get("walkScoreInfo", {}).get("walkScoreData", {}).get("bikeScore", {}).get("value"),

            # Risk factors
            "flood_factor": risk_factors.get("floodData", {}).get("floodFactor"),
            "fire_factor":  risk_factors.get("fireData", {}).get("fireFactor"),
            "heat_factor":  risk_factors.get("heatData", {}).get("heatFactor"),
            "wind_factor":  risk_factors.get("windData", {}).get("riskFactorScore"),

            # AI summary
            "ai_summary":   ai_summary.get("aiSummary"),

            # Offer insights
            "offer_insights": [
                {"header": c.get("header"), "title": c.get("title"), "copy": c.get("copy")}
                for c in offer_ins.get("insightsCards", [])
            ],

            # Schools
            "schools": [
                {
                    "name":    s.get("name"),
                    "type":    s.get("institutionType"),
                    "grades":  s.get("gradeRanges"),
                    "rating":  s.get("greatSchoolsRating"),
                    "distance":s.get("distanceInMiles"),
                    "level":   "elementary" if s.get("elementary") else "middle" if s.get("middle") else "high",
                }
                for s in schools.get("servingThisHomeSchools", [])
            ],

            # Price history
            "price_history": [
                {
                    "date":        (e.get("eventDateString") or e.get("eventDate")),
                    "parsed_date": self.parse_date(e.get("eventDate")) if e.get("eventDate") else None,
                    "event":       e.get("eventDescription"),
                    "price":       e.get("price"),
                    "source":      e.get("source"),
                }
                for e in below_fold.get("propertyHistoryInfo", {}).get("events", [])
            ],

            # AVM comparables
            "avm_comps": [
                {
                    "address": c.get("entireAddressString"),
                    "price":   c.get("priceInfo", {}).get("amount"),
                    "beds":    c.get("beds"),
                    "baths":   c.get("baths"),
                    "sqft":    c.get("sqFt", {}).get("value"),
                    "sold_date": c.get("soldDate"),
                }
                for c in avm.get("comparables", [])
            ],
            
            # Property Event History 
            "event_history": [
                {
                    "date": self.parse_date(event.get("eventDate")) if event.get("eventDate") else None,
                    "price": event.get("price") if event.get("price") else None,
                    "event": event.get("eventDescription") if event.get("eventDescription") else None,
                    "source": event.get("source") if event.get("source") else None,
                    "source_id": event.get("sourceId") if event.get("sourceId") else None
                }
                for event in prop_history.get("events", [])
            ],
            
            # Basic Info
            "basic_info": pub_history.get("basicInfo", {}),
            
            # Tax History
            "tax_history": [
                {
                    "tax_able_land_value": tax.get("taxableLandValue"), 
                    "tax_able_improvement_value": tax.get("taxableImprovementValue"),
                    "tax_year": tax.get("rollYear"),
                    "tax_annual": tax.get("taxesDue"),
                } 
                for tax in pub_history.get("allTaxInfo", [])
            ],
            
            "county": pub.get("countyName", ""),
            "mls": amenities.get("mlsDisclaimerInfo"),
            "amenities": {entity.get("amenityName") or entity.get("referenceName"):", ".join(entity.get("amenityValues", [])) for am in amenities.get("superGroups") for group in am.get("amenityGroups") for entity in group.get("amenityEntries")},
            
            # Around the home place
            "places": [{"name":place.get("name"), "popularity": place.get("popularity"), "distance": place.get("distance")} for place in around_home.get("pointOfInterestList", [])],
            "Transport": {trans.get("stopName") for trans in around_home.get("transitData", {}).get("stops", [])},
            
            "location_score": location
        }

        return result