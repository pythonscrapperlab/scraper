"""Typed value objects shared by freshness stages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias
from uuid import UUID

EventKind: TypeAlias = Literal[
    "new",
    "price_cut",
    "price_increase",
    "status",
    "relisted",
    "delisted",
    "tier_up",
    "tier_down",
    "score_move",
]


@dataclass(frozen=True)
class SearchListing:
    """Only the fields allowed from Redfin search results."""

    redfin_id: str
    listing_url: str
    price: int | None
    listing_status: str | None
    dom: int | None
    listed_at: datetime | None


@dataclass(frozen=True)
class PreviousListing:
    """Prior complete-search state used by the pure diff engine."""

    redfin_id: str
    property_id: UUID | None
    price: int | None
    listing_status: str | None
    dom: int | None
    listed_at: datetime | None
    absence_count: int
    is_delisted: bool


@dataclass(frozen=True)
class DiffEvent:
    """A PII-free state transition."""

    redfin_id: str
    property_id: UUID | None
    kind: EventKind
    detail: dict[str, str | int | float | bool | None]


@dataclass(frozen=True)
class ListingDecision:
    """Per-listing outcome of one complete snapshot comparison."""

    listing: SearchListing
    property_id: UUID | None
    events: tuple[DiffEvent, ...]
    enqueue_reason: str | None


@dataclass(frozen=True)
class AbsenceDecision:
    """State change for a listing absent from the latest complete snapshot."""

    redfin_id: str
    property_id: UUID | None
    absence_count: int
    confirmed_delisted: bool
    event: DiffEvent | None
