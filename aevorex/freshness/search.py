"""Redfin search-result-only client used by the fast freshness check."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from aevorex.db.models import utc_now
from aevorex.freshness.market import Market
from aevorex.freshness.types import SearchListing
from aevorex.scrapers.redfin import RedfinScraper

PAGE_SIZE = 350
MAX_PAGES = 100


class SearchFetchError(RuntimeError):
    """Raised when any page in a market snapshot cannot be validated."""


class RedfinSearchClient:
    """Fetch complete city inventory from Redfin's search GIS response."""

    def __init__(self, scraper: RedfinScraper | None = None) -> None:
        self.scraper = scraper or RedfinScraper()

    async def fetch_all(self, market: Market) -> tuple[list[SearchListing], int]:
        """Return the deduplicated snapshot only after the terminal page succeeds."""
        client = self.scraper._new_client()
        listings: dict[str, SearchListing] = {}
        page = 1
        try:
            while page <= MAX_PAGES:
                query = urlencode(
                    {
                        "al": 1,
                        "excl_ar": "true",
                        "excl_ll": "true",
                        "include_nearby_homes": "true",
                        "market": market.city.lower().replace(" ", ""),
                        "min_price": 50_000,
                        "mpt": 99,
                        "num_homes": PAGE_SIZE,
                        "ord": "days-on-redfin-asc",
                        "page_number": page,
                        "start": (page - 1) * PAGE_SIZE,
                        "region_id": market.region_id,
                        "region_type": 6,
                        "sf": "1,2,3,7",
                        "status": 9,
                        "uipt": "1,2,3,4",
                        "time_on_market_range": "120-",
                        "v": 8,
                    }
                )
                raw = await client.get_text(
                    f"{self.scraper.base_url}/stingray/api/gis?{query}"
                )
                homes = _decode_homes(raw)
                observed_at = utc_now()
                known_ids = set(listings)
                page_ids: set[str] = set()
                for home in homes:
                    listing = _parse_home(home, observed_at, self.scraper.base_url)
                    if listing is not None:
                        page_ids.add(listing.redfin_id)
                        listings[listing.redfin_id] = listing
                if homes and page_ids and page_ids.issubset(known_ids):
                    raise SearchFetchError("RepeatedSearchPage")
                if len(homes) < PAGE_SIZE:
                    return list(listings.values()), page
                page += 1
        except Exception as exc:
            raise SearchFetchError(type(exc).__name__) from exc
        finally:
            await client.close()
        raise SearchFetchError("PageLimitExceeded")


def _decode_homes(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        raise SearchFetchError("EmptySearchResponse")
    body = raw[4:] if raw.startswith("{}&&") else raw[raw.find("{") :]
    payload = json.loads(body)
    if payload.get("resultCode") != 0:
        raise SearchFetchError("SearchResultRejected")
    homes = payload.get("payload", {}).get("homes")
    if not isinstance(homes, list):
        raise SearchFetchError("MissingSearchHomes")
    return [home for home in homes if isinstance(home, dict)]


def _level_value(value: object) -> object:
    if isinstance(value, dict):
        return value.get("value")
    return value


def _parse_home(
    home: dict[str, Any], observed_at: datetime, base_url: str
) -> SearchListing | None:
    redfin_id = home.get("propertyId") or home.get("listingId")
    relative_url = home.get("url")
    if redfin_id is None or not isinstance(relative_url, str):
        return None
    price_value = _level_value(home.get("price"))
    dom_value = _level_value(home.get("dom"))
    listed_at = _listed_at(home, observed_at)
    return SearchListing(
        redfin_id=str(redfin_id),
        listing_url=(relative_url if relative_url.startswith("http") else base_url + relative_url),
        price=int(price_value) if isinstance(price_value, int | float) else None,
        listing_status=_string_or_none(home.get("mlsStatus")),
        dom=int(dom_value) if isinstance(dom_value, int | float) else None,
        listed_at=listed_at,
    )


def _listed_at(home: dict[str, Any], observed_at: datetime) -> datetime | None:
    original = _level_value(home.get("originalTimeOnRedfin"))
    if isinstance(original, int | float) and original > 0:
        return datetime.utcfromtimestamp(original / 1000)
    elapsed = _level_value(home.get("timeOnRedfin"))
    if isinstance(elapsed, int | float) and elapsed >= 0:
        return observed_at - timedelta(milliseconds=elapsed)
    return None


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
