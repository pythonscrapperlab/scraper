"""Pure search-snapshot and tier transition rules."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from aevorex.freshness.types import (
    AbsenceDecision,
    DiffEvent,
    ListingDecision,
    PreviousListing,
    SearchListing,
)

ACTIVE_STATUSES = {"active", "coming soon", "new"}
TIER_RANK = {"rest": 0, "strong": 1, "top": 2}


def _status(value: str | None) -> str:
    return " ".join((value or "").strip().lower().replace("_", " ").split())


def diff_listing(
    current: SearchListing,
    previous: PreviousListing | None,
    *,
    property_id: UUID | None,
) -> ListingDecision:
    """Compare one current card with the prior snapshot and canonical row."""
    resolved_property_id = previous.property_id if previous else property_id
    events: list[DiffEvent] = []
    enqueue_reason: str | None = None

    if previous is None:
        if resolved_property_id is None:
            events.append(
                DiffEvent(current.redfin_id, None, "new", {"source": "redfin"})
            )
            enqueue_reason = "new"
        return ListingDecision(
            current,
            resolved_property_id,
            tuple(events),
            enqueue_reason,
        )

    old_status = _status(previous.listing_status)
    new_status = _status(current.listing_status)
    if previous.is_delisted and new_status in ACTIVE_STATUSES:
        events.append(
            DiffEvent(
                current.redfin_id,
                previous.property_id,
                "relisted",
                {"from": previous.listing_status, "to": current.listing_status},
            )
        )
        enqueue_reason = "relisted"
    elif old_status != new_status:
        events.append(
            DiffEvent(
                current.redfin_id,
                previous.property_id,
                "status",
                {"from": previous.listing_status, "to": current.listing_status},
            )
        )
        enqueue_reason = "status"

    if current.price is not None and previous.price is not None:
        if current.price < previous.price:
            events.append(
                DiffEvent(
                    current.redfin_id,
                    previous.property_id,
                    "price_cut",
                    {"from": previous.price, "to": current.price},
                )
            )
            enqueue_reason = enqueue_reason or "price_cut"
        elif current.price > previous.price:
            events.append(
                DiffEvent(
                    current.redfin_id,
                    previous.property_id,
                    "price_increase",
                    {"from": previous.price, "to": current.price},
                )
            )
            enqueue_reason = enqueue_reason or "price_increase"

    listed_at_changed = (
        (current.listed_at is None) != (previous.listed_at is None)
        or (
            current.listed_at is not None
            and previous.listed_at is not None
            and abs(current.listed_at - previous.listed_at) > timedelta(minutes=5)
        )
    )
    # Days-on-market ticks up by itself every day, so a larger value with an unchanged listing
    # date is aging, not a change. Treating it as one re-queued ~every listing on the first
    # check of each day (the E4 soak measured 2,243 of Orlando's 2,565) and doubled the
    # nightly refresh. Only a DOM that appeared/vanished or went *down* (a relist/reset)
    # signals something new, alongside a moved listing date.
    dom_presence_changed = (current.dom is None) != (previous.dom is None)
    dom_went_down = (
        current.dom is not None and previous.dom is not None and current.dom < previous.dom
    )
    if listed_at_changed or dom_presence_changed or dom_went_down:
        enqueue_reason = enqueue_reason or "search_metadata"

    return ListingDecision(current, previous.property_id, tuple(events), enqueue_reason)


def diff_absence(previous: PreviousListing) -> AbsenceDecision:
    """Confirm delisting only after two consecutive complete-snapshot absences."""
    absence_count = previous.absence_count + 1
    confirmed = absence_count >= 2
    event = None
    if confirmed and not previous.is_delisted:
        event = DiffEvent(
            previous.redfin_id,
            previous.property_id,
            "delisted",
            {"consecutive_absences": absence_count},
        )
    return AbsenceDecision(
        redfin_id=previous.redfin_id,
        property_id=previous.property_id,
        absence_count=absence_count,
        confirmed_delisted=confirmed,
        event=event,
    )


def tier_for_percentile(percentile: float | None) -> str:
    """Map a market percentile to the owner-approved display tier."""
    if percentile is not None and percentile >= 95:
        return "top"
    if percentile is not None and percentile >= 80:
        return "strong"
    return "rest"


def tier_direction(previous: str, current: str) -> str | None:
    """Return the transition event name, if the percentile tier changed."""
    before = TIER_RANK[previous]
    after = TIER_RANK[current]
    if after > before:
        return "tier_up"
    if after < before:
        return "tier_down"
    return None
