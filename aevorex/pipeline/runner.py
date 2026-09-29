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

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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

    def __init__(self, scraper: BaseScraper, normalizer: BaseNormalizer, max_concurrent_fetches: int = 6):
        """
        Initialize pipeline with scraper and normalizer.

        Args:
            scraper: Scraper instance (BaseScraper subclass)
            normalizer: Normalizer instance (BaseNormalizer subclass)
            max_concurrent_fetches: how many property pages to fetch+parse at
                once. This is the network-bound part (each fetch is a full
                page load through the proxy), so it's the one worth
                parallelizing — DB writes stay sequential regardless (see
                run_scrape). Keep this conservative: too high risks tripping
                Redfin's WAF (note the aws-waf-token cookie in fetch()'s
                headers) or exhausting the proxy pool. Tune against however
                many concurrent connections webshare's plan actually supports.
        """
        self.scraper = scraper
        self.normalizer = normalizer
        self.max_concurrent_fetches = max_concurrent_fetches

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

            # Step 2: fetch + parse every URL concurrently (bounded). This is
            # the slow, network-bound part, and it's embarrassingly
            # parallel — nothing here touches the DB yet, so there's no
            # session-safety concern in doing many at once.
            fetched = await self._fetch_and_parse_all(urls)

            # Step 3: write to the DB one at a time. AsyncSession isn't safe
            # for concurrent use from multiple coroutines, and this part was
            # never the bottleneck anyway — a handful of INSERTs is fast
            # compared to a full page fetch through a proxy.
            for url, raw, parsed, fetch_error in fetched:
                await self._process_url(session, url, raw, parsed, fetch_error, result)

        except Exception as e:
            result["stats"]["errors"] += 1
            result["errors"].append({"error_type": "batch_error"})
            self.logger.error("Batch-level failure: error_class=%s", type(e).__name__)

        return result

    async def _fetch_and_parse_all(
        self, urls: List[str]
    ) -> List[Tuple[str, Optional[Any], Optional[dict], Optional[Exception]]]:
        """
        Fetch + parse every URL concurrently, bounded by max_concurrent_fetches.

        Returns one (url, raw, parsed, error) tuple per URL, in the same
        order as `urls` — never raises itself, so one bad fetch doesn't
        cancel the others via asyncio.gather's default fail-fast behavior.
        """
        semaphore = asyncio.Semaphore(self.max_concurrent_fetches)

        async def fetch_one(url: str):
            async with semaphore:
                try:
                    raw = await self.scraper.fetch(url)
                    if raw is None:
                        return url, None, None, RuntimeError("fetch() returned None")
                    parsed = await self.scraper.parse(raw)
                    return url, raw, parsed, None
                except Exception as e:
                    return url, None, None, e

        return await asyncio.gather(*(fetch_one(url) for url in urls))

    async def _process_url(
        self,
        session: AsyncSession,
        url: str,
        raw: Optional[Any],
        parsed: Optional[dict],
        fetch_error: Optional[Exception],
        result: Dict[str, Any],
    ) -> None:
        """Take an already fetched+parsed URL through normalize → upsert, each phase its own transaction."""

        # ---- Phase 1: save raw payload (fetch/parse already happened in _fetch_and_parse_all) ----
        try:
            if fetch_error is not None:
                raise fetch_error

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

            satellites_changed = await self._upsert_satellites(session, property_obj, normalized)
            if satellites_changed:
                # Deduplicator.upsert() already flips this for changed Property
                # scalar columns; this covers changes it can't see (comps,
                # price/tax history, location_score, features — separate rows,
                # not attributes on property_obj).
                property_obj.needs_analysis = True

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
        error_record = {"error_type": type(e).__name__}
        result["errors"].append(error_record)
        self.logger.error("Failed processing listing: error_class=%s", type(e).__name__)

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
                # Messages and tracebacks may contain listing/contact data.
                # easily over the column's 1000-char cap — the full detail is still in
                error_message=type(e).__name__,
                traceback=None,
                attempted_at=datetime.now(timezone.utc).replace(tzinfo=None),
            ))
            await session.commit()
        except Exception as exc:
            await session.rollback()
            self.logger.error(
                "Failed to persist scrape error: error_class=%s", type(exc).__name__
            )

    async def _upsert_satellites(
        self, session: AsyncSession, property_obj: Property, normalized: dict
    ) -> bool:
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

        Returns whether anything *scoring-relevant* actually changed, so
        the caller can decide whether to re-flag needs_analysis. Only
        comps/price_history/tax_history/location_score/features feed the
        scorers — schools/pois/transport_stops/images/open_houses don't, so
        those replacements are intentionally not tracked here. market_snapshot
        is zip-level (shared across every property in that zip), so it's
        deliberately excluded too: flipping every property in a zip on every
        market refresh is a much bigger blast radius than "this listing
        changed" (see the module docstring in aevorex/scoring/runner.py's
        design notes) — a property only picks up fresher market context the
        next time it itself is rescraped.
        """
        property_id = property_obj.id
        source = self.source

        await self._replace_rows(session, School, property_id, source, normalized.get("schools") or [])
        comps_changed = await self._replace_comps(session, property_id, source, normalized.get("comps") or [])
        await self._replace_rows(session, PointOfInterest, property_id, source, normalized.get("pois") or [])
        await self._replace_rows(session, TransportStop, property_id, source, normalized.get("transport_stops") or [])
        await self._replace_images(session, property_id, source, normalized.get("property_images") or [])
        await self._replace_open_houses(session, property_id, source, normalized.get("open_houses") or [])

        price_history_changed = await self._upsert_price_history(
            session, property_id, source, normalized.get("price_history") or []
        )
        tax_history_changed = await self._upsert_tax_history(
            session, property_id, source, normalized.get("tax_history") or []
        )

        location_score_changed = await self._upsert_one_to_one(
            session, LocationScore, property_id, normalized.get("location_score")
        )
        features_changed = await self._upsert_one_to_one(
            session, PropertyFeature, property_id, normalized.get("features")
        )

        await self._upsert_market_snapshot(session, normalized.get("market_snapshot"))

        await session.flush()

        return any([
            comps_changed,
            price_history_changed,
            tax_history_changed,
            location_score_changed,
            features_changed,
        ])

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

    # comps feed the fix-and-flip scorer's ARV estimate, so — unlike
    # schools/pois/transport_stops — this replace is fingerprinted against
    # what's being deleted so the caller can tell "the comp set actually
    # changed" from "identical comps rescraped again."
    _COMP_FINGERPRINT_FIELDS = ("comp_address", "price", "bedrooms", "bathrooms", "sqft")

    @classmethod
    async def _replace_comps(cls, session: AsyncSession, property_id, source: str, entries: list) -> bool:
        existing_result = await session.execute(
            select(PropertyComp).where(PropertyComp.property_id == property_id, PropertyComp.source == source)
        )
        existing_fingerprint = {
            tuple(getattr(row, f) for f in cls._COMP_FINGERPRINT_FIELDS)
            for row in existing_result.scalars().all()
        }
        new_fingerprint = {
            tuple(entry.get(f) for f in cls._COMP_FINGERPRINT_FIELDS)
            for entry in entries
            if entry
        }

        await cls._replace_rows(session, PropertyComp, property_id, source, entries)
        return existing_fingerprint != new_fingerprint

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
    async def _upsert_price_history(session: AsyncSession, property_id, source: str, entries: list) -> bool:
        """
        Insert any event this property doesn't already have from this source.

        Two things to know about the matching key here:

        - `price` may legitimately be NULL (Listing Removed / Pending /
          Relisted / Contingent / Delisted never carry one), so it's compared
          with IS NOT DISTINCT FROM. A plain `== price` comparison is NULL
          against NULL, which is NULL, which is not true — every unpriced
          event would look new on every single rescrape and the table would
          grow without bound.
        - `event` is part of the key. It wasn't before, which meant two
          different events sharing a timestamp and price (a Sold and the
          Listing Removed that follows it) collapsed into one row.

        `source_event_id` is included as a discriminator only. It is NOT
        unique per event — Redfin's `sourceId` is the MLS listing number, and
        every event in a listing cycle repeats it (verified: 29,553 raw
        payloads have a repeated id) — so uniqueness must never be keyed on it
        alone. Its value is telling one listing *run* apart from the next.

        Duplicates WITHIN a single payload are dropped too, not just against
        what's already stored. Redfin sometimes reports the same event twice
        in one `event_history` (verified: a "Pending" on 2014-05-15 repeated
        in the same response). The database check below can't catch those —
        neither copy is flushed yet when the second is examined — so without
        an in-batch guard both get added and the flush dies on the
        `uq_price_history_event` violation, taking the whole property with it.
        """
        added_any = False
        seen_in_batch = set()

        for entry in entries:
            event_date = entry.get("event_date")
            if not event_date:
                continue
            price = entry.get("price")
            event = entry.get("event") or "Unknown"
            source_event_id = entry.get("source_event_id")

            # Same shape as the unique index's key.
            batch_key = (event, event_date, price, source_event_id)
            if batch_key in seen_in_batch:
                continue
            seen_in_batch.add(batch_key)

            # Identity of an event, without the listing-run discriminator.
            same_event = (
                PriceHistory.property_id == property_id,
                PriceHistory.source == source,
                PriceHistory.event == event,
                PriceHistory.event_date == event_date,
                PriceHistory.price.is_not_distinct_from(price),
            )

            result = await session.execute(
                select(PriceHistory).where(
                    *same_event,
                    PriceHistory.source_event_id.is_not_distinct_from(source_event_id),
                )
            )
            # .first() not .scalar_one_or_none(): this only needs to know
            # whether a match exists. Pre-existing duplicate rows from before
            # this dedup check existed (e.g. runs recorded under the old
            # 'redfinscraper' source bug) would make scalar_one_or_none()
            # raise MultipleResultsFound instead of just confirming a match.
            existing = result.scalars().first()

            if existing is None and source_event_id is not None:
                # Fall back to a stored copy of this same event that predates
                # `source_event_id` being captured at all. Without this, an
                # event stored as (..., NULL) and re-scraped as (..., 'O642')
                # is NULL-vs-value under IS NOT DISTINCT FROM, reads as a
                # different event, and gets inserted a second time — which is
                # exactly how a backfill doubled this table. Adopt the row and
                # fill in the id rather than duplicating it.
                result = await session.execute(
                    select(PriceHistory).where(
                        *same_event, PriceHistory.source_event_id.is_(None)
                    )
                )
                existing = result.scalars().first()
                if existing is not None:
                    existing.source_event_id = source_event_id
                    added_any = True

            new_event_type = entry.get("event_type")
            is_rental_event = bool(entry.get("is_rental_event"))

            if existing:
                # `event_type` and `is_rental_event` are DERIVED — the
                # normalizer recomputes both by walking the property's whole
                # timeline, so a later scrape that sees more history can
                # legitimately correct an earlier verdict (a "Price Changed"
                # with nothing before it resolves to `price_changed`, but once
                # an earlier priced event shows up it becomes `reduced`).
                # Skipping the row entirely, as this used to, meant that
                # correction could never land.
                if existing.event_type != new_event_type or existing.is_rental_event != is_rental_event:
                    existing.event_type = new_event_type
                    existing.is_rental_event = is_rental_event
                    added_any = True
                continue

            session.add(PriceHistory(
                property_id=property_id,
                source=source,
                price=price,
                event=event,
                event_type=new_event_type,
                event_date=event_date,
                event_source=entry.get("event_source"),
                source_event_id=source_event_id,
                is_rental_event=is_rental_event,
            ))
            added_any = True
        return added_any

    @staticmethod
    async def _upsert_tax_history(session: AsyncSession, property_id, source: str, entries: list) -> bool:
        changed = False
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
            # .first() not .scalar_one_or_none() — see the note in
            # _upsert_price_history on why scalar_one_or_none() is risky here.
            existing = result.scalars().first()
            # `assessed_value` is only set when both components are present —
            # see RedfinNormalizer._extract_tax_history. The components are
            # carried through so a partial assessment stays inspectable rather
            # than collapsing to an unexplained NULL.
            fields = {
                "tax_amount": entry.get("tax_amount"),
                "land_value": entry.get("land_value"),
                "improvement_value": entry.get("improvement_value"),
                "assessed_value": entry.get("assessed_value"),
            }
            if existing:
                if any(getattr(existing, key) != value for key, value in fields.items()):
                    changed = True
                # A county reassessment can revise a prior year's figures, so
                # update in place rather than skip.
                for key, value in fields.items():
                    setattr(existing, key, value)
            else:
                session.add(TaxHistory(
                    property_id=property_id,
                    source=source,
                    tax_year=tax_year,
                    **fields,
                ))
                changed = True
        return changed

    # ------------------------------------------------------------------
    # 1:1 and zip-level tables: merge-or-insert on the natural key
    # ------------------------------------------------------------------

    @staticmethod
    async def _upsert_one_to_one(session: AsyncSession, model, property_id, data: Optional[dict]) -> bool:
        if not data:
            return False
        valid_keys = {c.name for c in model.__table__.columns} - {"property_id"}
        payload = {k: v for k, v in data.items() if k in valid_keys}

        existing = await session.get(model, property_id)
        if existing:
            changed = any(getattr(existing, key) != value for key, value in payload.items())
            for key, value in payload.items():
                setattr(existing, key, value)
            return changed
        else:
            session.add(model(property_id=property_id, **payload))
            return True

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
        # .first() not .scalar_one_or_none() — see the note in
        # _upsert_price_history on why scalar_one_or_none() is risky here.
        existing = result.scalars().first()
        valid_keys = {c.name for c in MarketSnapshot.__table__.columns} - {"id", "created_at"}
        payload = {k: v for k, v in data.items() if k in valid_keys}

        if existing:
            for key, value in payload.items():
                setattr(existing, key, value)
        else:
            session.add(MarketSnapshot(**payload))
