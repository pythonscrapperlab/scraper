"""
Pipeline orchestration — the glue between all layers.

Flow per URL, in two separate transactions:
  Phase 1 (fetch → parse → save raw_scrapes) commits on its own, so you
  always keep an audit trail of every fetch attempt, even ones that fail
  to normalize or upsert later.
  Phase 2 (normalize → dedup → upsert property → upsert satellites)
  commits on its own too.

Committing per URL (not once at the very end of the whole run) is
deliberate: with a single commit at the end, one bad URL — or the process
simply getting killed partway through a long run — throws away every
property that had already succeeded. Each URL is now durable the moment
it finishes.
"""

import json
import logging
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from aevorex.db.deduplicator import Deduplicator
from aevorex.db.models import (
    LocationScore,
    MarketSnapshot,
    OpenHouse,
    PointOfInterest,
    PriceHistory,
    Property,
    PropertyComp,
    PropertyFeature,
    PropertyImage,
    RawScrape,
    School,
    ScrapeError,
    TaxHistory,
    TransportStop,
)
from aevorex.normalizers.base import BaseNormalizer
from aevorex.scrapers.base import BaseScraper


def _json_safe(value: Any) -> Any:
    """
    Make a parsed payload safe to store in a JSON/JSONB column.

    Redfin's parse() output has real datetime objects mixed in (listed_at,
    event dates in price/event history, etc). SQLAlchemy's JSON type calls
    json.dumps() with no special handling, which throws on those. Round-trip
    through json.dumps(default=str) once here rather than making every
    scraper's parse() return JSON-primitive-only data.
    """
    return json.loads(json.dumps(value, default=str))


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

        # scraper.platform has been observed returning the lowercased class
        # name (e.g. "redfinscraper") instead of the intended source label —
        # worth fixing at the source in BaseScraper.platform to just return
        # self.SOURCE. Preferring SOURCE here is a defensive fallback so the
        # pipeline is correct either way, since a wrong value here breaks
        # external_id lookups and platform-ID dedup matching.
        self.source = getattr(scraper, "SOURCE", None) or scraper.platform

        self.logger = logging.getLogger(f"aevorex.pipeline.{self.source}")

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
                urls = await self.scraper.get_listing_urls(state, city=city)

            result["stats"]["total_urls"] = len(urls)

            # Step 2: Process each URL. Each URL commits its own work — see
            # module docstring for why.
            for url in urls:
                await self._process_url(session, url, result)

        except Exception as e:
            result["stats"]["errors"] += 1
            result["errors"].append({
                "error_type": "batch_error",
                "error_message": str(e),
                "traceback": traceback.format_exc(),
            })
            self.logger.error(f"Batch-level failure: {e}", exc_info=True)

        return result

    async def _process_url(self, session: AsyncSession, url: str, result: Dict[str, Any]) -> None:
        """Run one URL through fetch → parse → normalize → upsert, each phase its own transaction."""

        # ---- Phase 1: fetch + parse + save raw payload ----
        try:
            raw = await self.scraper.fetch(url)
            if raw is None:
                raise RuntimeError("fetch() returned None")

            parsed = await self.scraper.parse(raw)

            if parsed is None:
                # Parsing failed. Keep a slice of the raw HTML so we can
                # debug why the scraper broke, without defaulting to
                # storing the full page on every successful scrape.
                html_snippet = raw if isinstance(raw, str) else str(raw)
                raw_scrape = RawScrape(
                    source=self.source,
                    external_id=None,
                    url=url,
                    raw_json={"html_snippet": html_snippet[:20000]},
                    scrape_status="partial",
                    scraped_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
                session.add(raw_scrape)
                await session.commit()
                raise RuntimeError("parse() returned None")

            # Save the parsed payload — this is the real "raw platform
            # data" in the audit-trail sense: everything the platform gave
            # us, before normalization, minus the page markup.
            external_id = parsed.get(f"{self.source}_id")
            raw_scrape = RawScrape(
                source=self.source,
                external_id=external_id,
                url=url,
                raw_json=_json_safe(parsed),
                scrape_status="success",
                scraped_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            session.add(raw_scrape)
            await session.commit()

        except Exception as e:
            await session.rollback()
            self._record_failure(result, url, e)
            await self._record_error(session, url, e)
            return

        # ---- Phase 2: normalize + dedup + upsert ----
        try:
            normalized = await self.normalizer.normalize(parsed)
            if normalized is None:
                raise RuntimeError("normalize() returned None")

            property_obj, is_new = await Deduplicator.upsert(session, self.source, normalized)
            property_obj.raw_scrape_id = raw_scrape.id

            await self._upsert_satellites(session, property_obj, normalized)

            await session.commit()

            if is_new:
                result["inserted"] += 1
            else:
                result["updated"] += 1
            result["stats"]["success"] += 1

        except Exception as e:
            await session.rollback()
            self._record_failure(result, url, e)
            await self._record_error(session, url, e)

    def _record_failure(self, result: Dict[str, Any], url: str, e: Exception) -> None:
        """Bump stats, log immediately (don't just bury this in the result dict), and stash for the summary."""
        result["stats"]["failed"] += 1
        result["stats"]["errors"] += 1
        error_record = {
            "url": url,
            "error_type": type(e).__name__,
            "error_message": str(e),
            "traceback": traceback.format_exc(),
        }
        result["errors"].append(error_record)
        self.logger.error(f"Failed processing {url}: {e}", exc_info=True)

    async def _record_error(self, session: AsyncSession, url: str, e: Exception) -> None:
        """
        Persist a ScrapeError in its own small transaction.

        Runs after the main phase's rollback, so this write is independent
        of whatever just failed — a failure here shouldn't take down the
        rest of the run either.
        """
        try:
            session.add(ScrapeError(
                url=url,
                source=self.source,
                error_type=type(e).__name__,
                error_message=str(e),
                traceback=traceback.format_exc(),
                attempted_at=datetime.now(timezone.utc).replace(tzinfo=None),
            ))
            await session.commit()
        except Exception:
            await session.rollback()
            self.logger.error(f"Failed to record scrape error for {url}", exc_info=True)

    async def _upsert_satellites(
        self, session: AsyncSession, property_obj: Property, normalized: dict
    ) -> None:
        """
        Upsert every satellite table the normalizer can produce.

        Two different update strategies are in play here, deliberately:

        - "Current state" tables (schools, comps, pois, transport_stops,
          images, open_houses) get replaced wholesale on every rescrape:
          delete this property's rows from this source, insert the fresh
          set. These represent "what's true right now", not a history we
          want piling up — without this, a daily rescrape would insert the
          same 3 schools 365 times a year.
        - Genuine history tables (price_history, tax_history) get
          deduped/merged by their natural key instead, since the whole
          point is to keep every distinct event over time.
        - 1:1 tables (location_score, features) and market_snapshot get a
          plain merge-or-insert keyed on their primary key.
        """
        property_id = property_obj.id
        source = self.source

        await self._replace_rows(session, School, property_id, source, normalized.get("schools") or [])
        await self._replace_rows(session, PropertyComp, property_id, source, normalized.get("comps") or [])
        await self._replace_rows(session, PointOfInterest, property_id, source, normalized.get("pois") or [])
        await self._replace_rows(session, TransportStop, property_id, source, normalized.get("transport_stops") or [])
        await self._replace_images(session, property_id, source, normalized.get("property_images") or [])
        await self._replace_open_houses(session, property_id, source, normalized.get("open_houses") or [])

        await self._upsert_price_history(session, property_id, source, normalized.get("price_history") or [])
        await self._upsert_tax_history(session, property_id, source, normalized.get("tax_history") or [])

        await self._upsert_one_to_one(session, LocationScore, property_id, normalized.get("location_score"))
        await self._upsert_one_to_one(session, PropertyFeature, property_id, normalized.get("features"))

        await self._upsert_market_snapshot(session, normalized.get("market_snapshot"))

        await session.flush()

    # ------------------------------------------------------------------
    # "current state" tables: replace this property+source's rows wholesale
    # ------------------------------------------------------------------

    @staticmethod
    async def _replace_rows(session: AsyncSession, model, property_id, source: str, entries: list) -> None:
        await session.execute(
            delete(model).where(model.property_id == property_id, model.source == source)
        )
        for entry in entries:
            if not entry:
                continue
            session.add(model(property_id=property_id, **entry))

    @staticmethod
    async def _replace_images(session: AsyncSession, property_id, source: str, image_urls: list) -> None:
        await session.execute(
            delete(PropertyImage).where(PropertyImage.property_id == property_id, PropertyImage.source == source)
        )
        for idx, url in enumerate(image_urls):
            if url:
                session.add(PropertyImage(property_id=property_id, source=source, url=url, sort_order=idx))

    @staticmethod
    async def _replace_open_houses(session: AsyncSession, property_id, source: str, entries: list) -> None:
        await session.execute(
            delete(OpenHouse).where(OpenHouse.property_id == property_id, OpenHouse.source == source)
        )
        for oh in entries:
            if oh.get("start_time") and oh.get("end_time"):
                session.add(OpenHouse(
                    property_id=property_id,
                    source=source,
                    start_time=oh["start_time"],
                    end_time=oh["end_time"],
                ))

    # ------------------------------------------------------------------
    # history tables: dedupe/merge by natural key, never drop past rows
    # ------------------------------------------------------------------

    @staticmethod
    async def _upsert_price_history(session: AsyncSession, property_id, source: str, entries: list) -> None:
        for entry in entries:
            price = entry.get("price")
            event_date = entry.get("event_date")
            if not price or not event_date:
                continue
            result = await session.execute(
                select(PriceHistory.id).where(
                    PriceHistory.property_id == property_id,
                    PriceHistory.source == source,
                    PriceHistory.event_date == event_date,
                    PriceHistory.price == price,
                )
            )
            if result.scalar_one_or_none():
                continue
            session.add(PriceHistory(
                property_id=property_id,
                source=source,
                price=price,
                event=entry.get("event", "listed"),
                event_date=event_date,
            ))

    @staticmethod
    async def _upsert_tax_history(session: AsyncSession, property_id, source: str, entries: list) -> None:
        for entry in entries:
            tax_year = entry.get("tax_year")
            if not tax_year:
                continue
            result = await session.execute(
                select(TaxHistory).where(
                    TaxHistory.property_id == property_id,
                    TaxHistory.source == source,
                    TaxHistory.tax_year == tax_year,
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                # A county reassessment can revise a prior year's figures, so
                # update in place rather than skip.
                existing.tax_amount = entry.get("tax_amount")
                existing.assessed_value = entry.get("assessed_value")
            else:
                session.add(TaxHistory(
                    property_id=property_id,
                    source=source,
                    tax_year=tax_year,
                    tax_amount=entry.get("tax_amount"),
                    assessed_value=entry.get("assessed_value"),
                ))

    # ------------------------------------------------------------------
    # 1:1 and zip-level tables: merge-or-insert on the natural key
    # ------------------------------------------------------------------

    @staticmethod
    async def _upsert_one_to_one(session: AsyncSession, model, property_id, data: Optional[dict]) -> None:
        if not data:
            return
        valid_keys = {c.name for c in model.__table__.columns} - {"property_id"}
        payload = {k: v for k, v in data.items() if k in valid_keys}

        existing = await session.get(model, property_id)
        if existing:
            for key, value in payload.items():
                setattr(existing, key, value)
        else:
            session.add(model(property_id=property_id, **payload))

    @staticmethod
    async def _upsert_market_snapshot(session: AsyncSession, data: Optional[dict]) -> None:
        if not data or not data.get("zip_code") or not data.get("snapshot_date"):
            return

        result = await session.execute(
            select(MarketSnapshot).where(
                MarketSnapshot.zip_code == data["zip_code"],
                MarketSnapshot.source == data.get("source"),
                MarketSnapshot.snapshot_date == data.get("snapshot_date"),
            )
        )
        existing = result.scalar_one_or_none()
        valid_keys = {c.name for c in MarketSnapshot.__table__.columns} - {"id", "created_at"}
        payload = {k: v for k, v in data.items() if k in valid_keys}

        if existing:
            for key, value in payload.items():
                setattr(existing, key, value)
        else:
            session.add(MarketSnapshot(**payload))