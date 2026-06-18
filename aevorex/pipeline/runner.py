"""
Pipeline orchestration — the glue between all layers.

Flow per URL:
1. fetch(url) → raw response
2. save raw JSON to raw_scrapes
3. parse(raw) → platform dict
4. normalize() → properties schema dict
5. dedup lookup
6. insert or update
7. upsert satellites (price_history, images, etc.)
"""

import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from aevorex.db.deduplicator import Deduplicator
from aevorex.db.models import (
    Property,
    RawScrape,
    PriceHistory,
    PropertyImage,
    OpenHouse,
    ScrapeError,
)
from aevorex.normalizers.base import BaseNormalizer
from aevorex.scrapers.base import BaseScraper


class PipelineRunner:
    """
    Orchestrates the entire scraping pipeline.
    
    Usage:
        runner = PipelineRunner(scraper, normalizer)
        result = await runner.run_scrape(session, state)
    """

    def __init__(self, scraper: BaseScraper, normalizer: BaseNormalizer):
        """
        Initialize pipeline with scraper and normalizer.
        
        Args:
            scraper: Scraper instance (BaseScraper subclass)
            normalizer: Normalizer instance (BaseNormalizer subclass)
        """
        self.scraper = scraper
        self.normalizer = normalizer
        self.source = scraper.platform

    async def run_scrape(
        self, session: AsyncSession, state: str, urls: Optional[List[str]] = None, city: str = ""
    ) -> Dict[str, Any]:
        """
        Execute complete scrape pipeline for a state.
        
        Flow:
        1. Get listing URLs
        2. For each URL: fetch → parse → normalize → dedup → upsert
        3. Return stats and errors
        
        Args:
            session: Async database session
            state: State to scrape (FL, CA, etc.)
            urls: Override URLs (optional)
            
        Returns:
            {
                'state': str,
                'source': str,
                'stats': {
                    'total_urls': int,
                    'success': int,
                    'failed': int,
                    'errors': int,
                },
                'errors': [list of error dicts],
                'inserted': int,
                'updated': int,
            }
        """
        result = {
            "state": state,
            "source": self.source,
            "stats": {
                "total_urls": 0,
                "success": 0,
                "failed": 0,
                "errors": 0,
            },
            "errors": [],
            "inserted": 0,
            "updated": 0,
        }

        try:
            # Step 1: Get URLs
            if urls is None:
                urls = await self.scraper.get_listing_urls(state)
            
            result["stats"]["total_urls"] = len(urls)

            # Step 2: Process each URL
            for url in urls:
                error_record = None
                try:
                    # Fetch
                    raw = await self.scraper.fetch(url)
                    if raw is None:
                        result["stats"]["failed"] += 1
                        error_record = {
                            "url": url,
                            "error_type": "fetch_failed",
                            "error_message": "fetch() returned None",
                        }
                        continue

                    # Save raw blob
                    external_id = None
                    try:
                        if isinstance(raw, dict):
                            external_id = raw.get("id") or raw.get("external_id")
                    except:
                        pass

                    raw_scrape = RawScrape(
                        source=self.source,
                        external_id=external_id,
                        url=url,
                        raw_json=raw if isinstance(raw, dict) else {"html": raw},
                        scrape_status="success",
                        scraped_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                    session.add(raw_scrape)
                    await session.flush()

                    # Parse
                    parsed = await self.scraper.parse(raw)
                    if parsed is None:
                        result["stats"]["failed"] += 1
                        error_record = {
                            "url": url,
                            "error_type": "parse_failed",
                            "error_message": "parse() returned None",
                        }
                        continue

                    # Normalize
                    normalized = await self.normalizer.normalize(parsed)
                    if normalized is None:
                        result["stats"]["failed"] += 1
                        error_record = {
                            "url": url,
                            "error_type": "normalize_failed",
                            "error_message": "normalize() returned None",
                        }
                        continue

                    # Dedup & Upsert
                    property_obj, is_new = await Deduplicator.upsert(
                        session, self.source, normalized
                    )

                    # Upsert satellites
                    await self._upsert_satellites(session, property_obj, normalized)

                    if is_new:
                        result["inserted"] += 1
                    else:
                        result["updated"] += 1

                    result["stats"]["success"] += 1

                except Exception as e:
                    result["stats"]["failed"] += 1
                    result["stats"]["errors"] += 1
                    error_record = {
                        "url": url,
                        "error_type": type(e).__name__,
                        "error_message": str(e),
                        "traceback": traceback.format_exc(),
                    }

                finally:
                    # Save error to database
                    if error_record:
                        result["errors"].append(error_record)
                        scrape_error = ScrapeError(
                            url=url,
                            source=self.source,
                            error_type=error_record.get("error_type", "unknown"),
                            error_message=error_record.get("error_message", ""),
                            traceback=error_record.get("traceback"),
                            attempted_at=datetime.now(timezone.utc).replace(tzinfo=None),
                        )
                        session.add(scrape_error)

        except Exception as e:
            result["stats"]["errors"] += 1
            result["errors"].append({
                "error_type": "batch_error",
                "error_message": str(e),
                "traceback": traceback.format_exc(),
            })

        # Commit all changes
        await session.commit()

        return result

    async def _upsert_satellites(
        self, session: AsyncSession, property_obj: Property, normalized: dict
    ) -> None:
        """
        Upsert satellite tables (price_history, images, open_houses).
        
        Args:
            session: Async database session
            property_obj: Property object
            normalized: Normalized data dict
        """
        # Price history
        price_history = normalized.get("price_history", [])
        for entry in price_history:
            if entry.get("price") and entry.get("event_date"):
                existing = None
                # Could check for duplicates here if needed
                ph = PriceHistory(
                    property_id=property_obj.id,
                    source=self.source,
                    price=entry["price"],
                    event=entry.get("event", "listed"),
                    event_date=entry["event_date"],
                )
                session.add(ph)

        # Property images
        images = normalized.get("property_images", [])
        for idx, img_url in enumerate(images):
            if img_url:
                pi = PropertyImage(
                    property_id=property_obj.id,
                    source=self.source,
                    url=img_url,
                    sort_order=idx,
                )
                session.add(pi)

        # Open houses
        open_houses = normalized.get("open_houses", [])
        for oh in open_houses:
            if oh.get("start_time") and oh.get("end_time"):
                ohouse = OpenHouse(
                    property_id=property_obj.id,
                    source=self.source,
                    start_time=oh["start_time"],
                    end_time=oh["end_time"],
                )
                session.add(ohouse)

        await session.flush()
