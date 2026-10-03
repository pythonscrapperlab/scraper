"""Transactional search-level freshness checks."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aevorex.config import settings
from aevorex.db.models import (
    ChangeEvent,
    ListingPresence,
    MarketFreshness,
    PendingRefresh,
    Property,
    utc_now,
)
from aevorex.db.session import async_session_maker
from aevorex.freshness.diff import diff_absence, diff_listing
from aevorex.freshness.market import Market, resolve_market
from aevorex.freshness.search import RedfinSearchClient
from aevorex.freshness.types import PreviousListing, SearchListing

logger = logging.getLogger("aevorex.freshness.check")
CHECK_CADENCE_MINUTES = 120


class SnapshotRejected(RuntimeError):
    """The snapshot is implausibly small next to the previous complete one."""


def snapshot_is_plausible(previous: int, current: int, min_ratio: float) -> tuple[bool, float | None]:
    """Return (accepted, ratio). With no prior complete snapshot everything is accepted."""
    if previous <= 0:
        return True, None
    ratio = current / previous
    return (current > 0 and ratio >= min_ratio), ratio


async def run_check(
    market_slug: str, *, search_client: RedfinSearchClient | None = None
) -> dict[str, int]:
    """Fetch and atomically apply one complete Redfin city search snapshot."""
    market = resolve_market(market_slug)
    client = search_client or RedfinSearchClient()
    await _ensure_market(market)
    try:
        listings, pages = await client.fetch_all(market)
        async with async_session_maker() as session:
            counts = await apply_snapshot(session, market, listings)
            counts["pages"] = pages
            await session.commit()
    except Exception:
        await _mark_failed(market.slug)
        raise
    logger.info(
        "Search check complete: market=%s pages=%s listings=%s events=%s queued=%s",
        market.slug,
        counts["pages"],
        counts["listings"],
        counts["events"],
        counts["queued"],
    )
    return counts


async def apply_snapshot(
    session: AsyncSession,
    market: Market,
    listings: list[SearchListing],
) -> dict[str, int]:
    """Apply an already complete snapshot; exposed for deterministic tests."""
    now = utc_now()
    market_row = await session.get(MarketFreshness, market.slug)
    if market_row is None:
        market_row = _market_row(market)
        session.add(market_row)
        await session.flush()

    previous_count = (
        int(market_row.listings_active or 0) if market_row.last_checked_at is not None else 0
    )
    accepted, ratio = snapshot_is_plausible(
        previous_count, len(listings), settings.check_min_snapshot_ratio
    )
    if ratio is not None:
        logger.info(
            "Snapshot ratio: market=%s previous=%s current=%s ratio=%.3f min=%.2f accepted=%s",
            market.slug, previous_count, len(listings), ratio,
            settings.check_min_snapshot_ratio, accepted,
        )
    if not accepted:
        # Raised before any write: no absence counting, no events, and
        # last_checked_at is not advanced (run_check marks the market failed).
        raise SnapshotRejected(market.slug)

    redfin_ids = {listing.redfin_id for listing in listings}
    properties = {}
    if redfin_ids:
        rows = (
            await session.execute(select(Property).where(Property.redfin_id.in_(redfin_ids)))
        ).scalars()
        properties = {str(prop.redfin_id): prop for prop in rows if prop.redfin_id}

    presence_rows = (
        await session.execute(
            select(ListingPresence).where(ListingPresence.market_slug == market.slug)
        )
    ).scalars().all()
    presence = {str(row.redfin_id): row for row in presence_rows}
    queue_rows = (
        await session.execute(
            select(PendingRefresh).where(PendingRefresh.market_slug == market.slug)
        )
    ).scalars().all()
    queue = {str(row.redfin_id): row for row in queue_rows}

    event_count = 0
    queued_count = 0
    changed_ids: set[str] = set()

    for listing in listings:
        prop = properties.get(listing.redfin_id)
        row = presence.get(listing.redfin_id)
        previous = _previous(row) if row is not None else _previous_property(prop)
        decision = diff_listing(
            listing,
            previous,
            property_id=cast(UUID, prop.id) if prop is not None else None,
        )
        for event in decision.events:
            session.add(
                ChangeEvent(
                    property_id=event.property_id,
                    market_slug=market.slug,
                    redfin_id=event.redfin_id,
                    kind=event.kind,
                    detail=event.detail,
                    observed_at=now,
                )
            )
            event_count += 1
            changed_ids.add(listing.redfin_id)
        if decision.enqueue_reason is not None:
            _enqueue(
                session,
                queue,
                market.slug,
                listing,
                cast(UUID, prop.id) if prop is not None else None,
                decision.enqueue_reason,
                now,
            )
            queued_count += 1
            changed_ids.add(listing.redfin_id)

        if row is None:
            row = ListingPresence(
                market_slug=market.slug,
                redfin_id=listing.redfin_id,
                listing_url=listing.listing_url,
            )
            session.add(row)
            presence[listing.redfin_id] = row
        mutable_row: Any = row
        mutable_row.property_id = prop.id if prop is not None else row.property_id
        mutable_row.listing_url = listing.listing_url
        mutable_row.price = listing.price
        mutable_row.listing_status = listing.listing_status
        mutable_row.dom = listing.dom
        mutable_row.listed_at = listing.listed_at
        mutable_row.absence_count = 0
        mutable_row.is_delisted = False
        mutable_row.last_seen_at = now

    for redfin_id, row in presence.items():
        if redfin_id in redfin_ids:
            continue
        absence = diff_absence(_previous(row))
        mutable_row = cast(Any, row)
        mutable_row.absence_count = absence.absence_count
        if absence.confirmed_delisted:
            mutable_row.is_delisted = True
        if absence.event is None:
            continue
        session.add(
            ChangeEvent(
                property_id=absence.property_id,
                market_slug=market.slug,
                redfin_id=redfin_id,
                kind="delisted",
                detail=absence.event.detail,
                observed_at=now,
            )
        )
        event_count += 1
        changed_ids.add(redfin_id)
        if row.property_id is not None:
            prop = await session.get(Property, row.property_id)
            if prop is not None:
                mutable_prop: Any = prop
                mutable_prop.listing_status = "Delisted"
                mutable_prop.listing_status_normalized = "delisted"
                mutable_prop.delisted_at = now
                mutable_prop.needs_analysis = True

    mutable_market: Any = market_row
    mutable_market.last_checked_at = now
    mutable_market.next_check_at = now + timedelta(minutes=CHECK_CADENCE_MINUTES)
    mutable_market.check_status = "ok"
    mutable_market.listings_active = len(listings)
    mutable_market.changed_last_check = len(changed_ids)
    return {
        "listings": len(listings),
        "events": event_count,
        "changed": len(changed_ids),
        "queued": queued_count,
        "absent": sum(1 for row in presence.values() if row.absence_count > 0),
        "delisted": sum(1 for row in presence.values() if row.is_delisted),
    }


def _previous(row: ListingPresence) -> PreviousListing:
    item: Any = row
    return PreviousListing(
        redfin_id=str(item.redfin_id),
        property_id=cast(UUID | None, item.property_id),
        price=cast(int | None, item.price),
        listing_status=cast(str | None, item.listing_status),
        dom=cast(int | None, item.dom),
        listed_at=item.listed_at,
        absence_count=int(item.absence_count),
        is_delisted=bool(item.is_delisted),
    )


def _previous_property(prop: Property | None) -> PreviousListing | None:
    if prop is None or prop.redfin_id is None:
        return None
    item: Any = prop
    return PreviousListing(
        redfin_id=str(item.redfin_id),
        property_id=cast(UUID, item.id),
        price=cast(int | None, item.price),
        listing_status=cast(str | None, item.listing_status),
        dom=cast(int | None, item.days_on_market),
        listed_at=item.listed_at,
        absence_count=0,
        is_delisted=item.listing_status_normalized == "delisted",
    )


def _enqueue(
    session: AsyncSession,
    queue: dict[str, PendingRefresh],
    market_slug: str,
    listing: SearchListing,
    property_id: UUID | None,
    reason: str,
    now: object,
) -> None:
    from datetime import datetime

    if not isinstance(now, datetime):
        raise TypeError("now must be datetime")
    row = queue.get(listing.redfin_id)
    if row is None:
        row = PendingRefresh(
            market_slug=market_slug,
            source="redfin",
            redfin_id=listing.redfin_id,
            listing_url=listing.listing_url,
        )
        session.add(row)
        queue[listing.redfin_id] = row
    mutable_row: Any = row
    mutable_row.property_id = property_id
    mutable_row.listing_url = listing.listing_url
    mutable_row.reason = reason
    mutable_row.status = "pending"
    mutable_row.next_attempt_at = now
    mutable_row.last_error_class = None


async def _ensure_market(market: Market) -> None:
    async with async_session_maker() as session:
        if await session.get(MarketFreshness, market.slug) is None:
            session.add(_market_row(market))
            await session.commit()


def _market_row(market: Market) -> MarketFreshness:
    return MarketFreshness(
        slug=market.slug,
        city=market.city,
        state=market.state,
        region_id=market.region_id,
        check_status="warming",
        listings_active=0,
        changed_last_check=0,
    )


async def _mark_failed(market_slug: str) -> None:
    async with async_session_maker() as session:
        row = await session.get(MarketFreshness, market_slug)
        if row is not None:
            mutable_row: Any = row
            mutable_row.check_status = "failed"
            await session.commit()
