"""Pure freshness diff, tier, and backoff contract tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from aevorex.freshness.diff import (
    diff_absence,
    diff_listing,
    tier_direction,
    tier_for_percentile,
)
from aevorex.freshness.refresh import backoff_delay_minutes
from aevorex.freshness.search import _decode_homes
from aevorex.freshness.types import PreviousListing, SearchListing

NOW = datetime(2026, 9, 30, 12, 0, 0)


def _current(**changes: object) -> SearchListing:
    values: dict[str, object] = {
        "redfin_id": "123",
        "listing_url": "https://example.invalid/home/123",
        "price": 300_000,
        "listing_status": "Active",
        "dom": 10,
        "listed_at": NOW,
    }
    values.update(changes)
    return SearchListing(**values)  # type: ignore[arg-type]


def _previous(**changes: object) -> PreviousListing:
    values: dict[str, object] = {
        "redfin_id": "123",
        "property_id": uuid4(),
        "price": 300_000,
        "listing_status": "Active",
        "dom": 10,
        "listed_at": NOW,
        "absence_count": 0,
        "is_delisted": False,
    }
    values.update(changes)
    return PreviousListing(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("current", "previous", "kinds", "reason"),
    [
        (_current(price=290_000), _previous(), ["price_cut"], "price_cut"),
        (_current(price=310_000), _previous(), ["price_increase"], "price_increase"),
        (_current(listing_status="Pending"), _previous(), ["status"], "status"),
        (
            _current(listing_status="Active"),
            _previous(listing_status="Delisted", is_delisted=True),
            ["relisted"],
            "relisted",
        ),
        (_current(dom=11), _previous(), [], "search_metadata"),
        (
            _current(listed_at=NOW + timedelta(seconds=20)),
            _previous(),
            [],
            None,
        ),
        (_current(), _previous(), [], None),
    ],
)
def test_diff_matrix(
    current: SearchListing,
    previous: PreviousListing,
    kinds: list[str],
    reason: str | None,
) -> None:
    result = diff_listing(current, previous, property_id=previous.property_id)
    assert [event.kind for event in result.events] == kinds
    assert result.enqueue_reason == reason


def test_new_listing_emits_once_when_no_prior_state() -> None:
    result = diff_listing(_current(), None, property_id=None)
    assert [event.kind for event in result.events] == ["new"]
    assert result.enqueue_reason == "new"


def test_delist_requires_two_consecutive_absences() -> None:
    first = diff_absence(_previous())
    assert first.absence_count == 1
    assert first.event is None
    second = diff_absence(_previous(absence_count=first.absence_count))
    assert second.absence_count == 2
    assert second.event is not None
    assert second.event.kind == "delisted"


@pytest.mark.parametrize(
    ("percentile", "tier"),
    [(None, "rest"), (79.99, "rest"), (80, "strong"), (94.99, "strong"), (95, "top")],
)
def test_tier_thresholds(percentile: float | None, tier: str) -> None:
    assert tier_for_percentile(percentile) == tier


def test_tier_directions() -> None:
    assert tier_direction("rest", "strong") == "tier_up"
    assert tier_direction("top", "strong") == "tier_down"
    assert tier_direction("strong", "strong") is None


def test_backoff_schedule_caps_at_two_hours() -> None:
    assert [backoff_delay_minutes(attempt) for attempt in range(1, 9)] == [
        5,
        10,
        20,
        40,
        80,
        120,
        120,
        120,
    ]


def test_redfin_xssi_prefix_is_removed() -> None:
    assert _decode_homes('{}&&{"resultCode":0,"payload":{"homes":[]}}') == []
