"""Queued Redfin detail refresh through the existing property-page pipeline."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aevorex.db.models import (
    ChangeEvent,
    ListingPresence,
    MarketFreshness,
    PendingRefresh,
    Property,
    utc_now,
)
from aevorex.db.session import async_session_maker
from aevorex.freshness.check import _ensure_market
from aevorex.freshness.market import resolve_market
from aevorex.normalizers.redfin import RedfinNormalizer
from aevorex.pipeline.runner import PipelineRunner
from aevorex.scrapers.redfin import RedfinScraper

logger = logging.getLogger("aevorex.freshness.refresh")


def backoff_delay_minutes(failure_count: int) -> int:
    """Return 5, 10, 20, 40, 80, then the two-hour cap."""
    if failure_count < 1:
        return 0
    return int(min(120, 5 * (2 ** (failure_count - 1))))


async def run_refresh(
    *, market_slug: str | None = None, pending_only: bool = False
) -> dict[str, int | float]:
    """Queue a full city when requested, then consume due detail refreshes."""
    if (market_slug is None) == (not pending_only):
        raise ValueError("Choose exactly one of market_slug or pending_only")
    if market_slug is not None:
        market = resolve_market(market_slug)
        await _ensure_market(market)
        await _queue_city(market.slug, market.city, market.state)

    now = utc_now()
    async with async_session_maker() as session:
        query = select(PendingRefresh).where(
            PendingRefresh.status.in_(("pending", "failed")),
            PendingRefresh.next_attempt_at <= now,
        )
        if market_slug is not None:
            query = query.where(PendingRefresh.market_slug == market_slug)
        items = (await session.execute(query)).scalars().all()
        for item in items:
            processing_item: Any = item
            processing_item.status = "processing"
        await session.commit()

    if not items:
        return {
            "queued": 0,
            "success": 0,
            "failed": 0,
            "attempts": 0,
            "http_405": 0,
            "blocked": 0,
            "block_rate": 0.0,
        }

    started_at = utc_now()
    scraper = RedfinScraper()
    runner = PipelineRunner(
        scraper,
        RedfinNormalizer(),  # type: ignore[no-untyped-call]
        max_concurrent_fetches=3,
    )
    urls = [str(item.listing_url) for item in items]
    state = resolve_market(market_slug).state if market_slug else ""
    async with async_session_maker() as session:
        pipeline_result = await runner.run_scrape(session, state, urls=urls)

    redfin_ids = {str(item.redfin_id) for item in items}
    queue_ids = {item.id for item in items}
    async with async_session_maker() as session:
        refreshed = (
            await session.execute(
                select(Property).where(
                    Property.redfin_id.in_(redfin_ids),
                    Property.refreshed_at >= started_at,
                )
            )
        ).scalars().all()
        refreshed_by_id = {
            str(prop.redfin_id): prop for prop in refreshed if prop.redfin_id is not None
        }
        queue_items = (
            await session.execute(
                select(PendingRefresh).where(
                    PendingRefresh.id.in_(queue_ids),
                    PendingRefresh.status == "processing",
                )
            )
        ).scalars().all()
        success = 0
        failed = 0
        for item in queue_items:
            mutable_item: Any = item
            prop = refreshed_by_id.get(str(item.redfin_id))
            if prop is not None:
                mutable_item.status = "succeeded"
                mutable_item.property_id = prop.id
                mutable_item.last_error_class = None
                success += 1
                await _attach_property(
                    session, str(item.market_slug), str(item.redfin_id), prop
                )
            else:
                mutable_item.status = "failed"
                mutable_item.attempts = int(item.attempts) + 1
                mutable_item.last_error_class = "DetailRefreshFailed"
                mutable_item.next_attempt_at = utc_now() + timedelta(
                    minutes=backoff_delay_minutes(int(mutable_item.attempts))
                )
                failed += 1

        if market_slug is not None and failed == 0:
            market_row = await session.get(MarketFreshness, market_slug)
            if market_row is not None:
                mutable_market: Any = market_row
                mutable_market.last_refreshed_at = utc_now()
        await session.commit()

    attempts = int(scraper.fetch_metrics["attempts"])
    blocks = int(scraper.fetch_metrics["blocked"])
    counts: dict[str, int | float] = {
        "queued": len(items),
        "success": success,
        "failed": failed,
        "attempts": attempts,
        "http_405": int(scraper.fetch_metrics["http_405"]),
        "blocked": blocks,
        "block_rate": round(blocks / attempts, 4) if attempts else 0.0,
        "pipeline_errors": int(pipeline_result["stats"]["errors"]),
    }
    logger.info(
        "Detail refresh complete: queued=%s success=%s failed=%s attempts=%s "
        "http_405=%s blocked=%s block_rate=%.4f",
        counts["queued"],
        counts["success"],
        counts["failed"],
        counts["attempts"],
        counts["http_405"],
        counts["blocked"],
        counts["block_rate"],
    )
    return counts


async def _queue_city(market_slug: str, city: str, state: str) -> None:
    now = utc_now()
    async with async_session_maker() as session:
        properties = (
            await session.execute(
                select(Property).where(
                    Property.city.ilike(city),
                    Property.state == state,
                    Property.redfin_id.is_not(None),
                    Property.listing_url.is_not(None),
                    or_(
                        Property.listing_status_normalized.is_(None),
                        ~Property.listing_status_normalized.in_(("sold", "delisted")),
                    ),
                )
            )
        ).scalars().all()
        existing = (
            await session.execute(
                select(PendingRefresh).where(PendingRefresh.market_slug == market_slug)
            )
        ).scalars().all()
        queue = {str(item.redfin_id): item for item in existing}
        presence_rows = (
            await session.execute(
                select(ListingPresence).where(
                    ListingPresence.market_slug == market_slug,
                    ListingPresence.is_delisted.is_(False),
                )
            )
        ).scalars().all()
        candidates: dict[str, tuple[object | None, str]] = {
            str(row.redfin_id): (row.property_id, str(row.listing_url))
            for row in presence_rows
        }
        for prop in properties:
            redfin_id = str(prop.redfin_id)
            candidates.setdefault(redfin_id, (prop.id, str(prop.listing_url)))
        for redfin_id, (property_id, listing_url) in candidates.items():
            item = queue.get(redfin_id)
            if item is None:
                item = PendingRefresh(
                    market_slug=market_slug,
                    source="redfin",
                    redfin_id=redfin_id,
                    listing_url=listing_url,
                )
                session.add(item)
            mutable_item: Any = item
            mutable_item.property_id = property_id
            mutable_item.listing_url = listing_url
            mutable_item.reason = "nightly"
            mutable_item.status = "pending"
            mutable_item.next_attempt_at = now
            mutable_item.last_error_class = None
        await session.commit()


async def _attach_property(
    session: AsyncSession, market_slug: str, redfin_id: str, prop: Property
) -> None:
    presence = (
        await session.execute(
            select(ListingPresence).where(
                ListingPresence.market_slug == market_slug,
                ListingPresence.redfin_id == redfin_id,
            )
        )
    ).scalars().first()
    if presence is not None:
        mutable_presence: Any = presence
        mutable_presence.property_id = prop.id
    events = (
        await session.execute(
            select(ChangeEvent).where(
                ChangeEvent.market_slug == market_slug,
                ChangeEvent.redfin_id == redfin_id,
                ChangeEvent.property_id.is_(None),
            )
        )
    ).scalars().all()
    for event in events:
        mutable_event: Any = event
        mutable_event.property_id = prop.id
