"""
Tests for the price_history upsert half of the ingestion fix.

The normalizer can emit an unpriced event perfectly and still lose it here,
either by skipping falsy prices or by re-inserting it on every rescrape
because NULL never equals NULL. Both paths are covered without a live DB: a
stub session records what would be written and hands back the compiled
statement, so the NULL-safe comparison is asserted against the SQL actually
generated rather than trusted.
"""

import asyncio
from datetime import datetime
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from aevorex.db.event_types import LISTED, PENDING, PRICE_CHANGED, REDUCED, REMOVED
from aevorex.db.models import PriceHistory
from aevorex.pipeline.runner import PipelineRunner


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    """Minimal stand-in for AsyncSession: records adds and every statement executed."""

    def __init__(self, existing_match=None):
        self.added = []
        self.statements = []
        self._existing_match = existing_match
        # When set, the exact-match lookup (which includes source_event_id)
        # misses and only the NULL-source_event_id fallback hits — the shape
        # of a row stored before that column was captured.
        self.match_only_when_sid_is_null = False

    async def execute(self, statement):
        self.statements.append(statement)
        if self._existing_match is None:
            return _FakeResult([])
        if self.match_only_when_sid_is_null and len(self.statements) == 1:
            return _FakeResult([])
        return _FakeResult([self._existing_match])

    def add(self, obj):
        self.added.append(obj)


def entry(event, event_date, price=None, event_type=None, source_event_id="O6424408"):
    return {
        "event": event,
        "event_type": event_type,
        "event_date": event_date,
        "price": price,
        "source": "redfin",
        "event_source": "Stellar MLS as Distributed by MLS Grid",
        "source_event_id": source_event_id,
    }


def upsert(session, entries):
    return asyncio.run(
        PipelineRunner._upsert_price_history(session, uuid4(), "redfin", entries)
    )


def test_event_with_a_null_price_is_inserted():
    session = FakeSession()

    changed = upsert(session, [entry("Listing Removed", datetime(2026, 3, 1), event_type=REMOVED)])

    assert changed is True
    assert len(session.added) == 1
    row = session.added[0]
    assert row.price is None
    assert row.event == "Listing Removed"
    assert row.event_type == REMOVED


def test_all_new_event_fields_reach_the_row():
    session = FakeSession()

    upsert(session, [entry("Pending", datetime(2026, 2, 20), event_type=PENDING, source_event_id="A10884991")])

    row = session.added[0]
    assert row.source == "redfin", "the platform, not the MLS feed"
    assert row.event_source == "Stellar MLS as Distributed by MLS Grid"
    assert row.source_event_id == "A10884991"
    assert row.event_date == datetime(2026, 2, 20)


def test_priced_and_unpriced_events_are_all_written():
    session = FakeSession()

    upsert(session, [
        entry("Listed", datetime(2026, 1, 5), price=450_000, event_type=LISTED),
        entry("Pending", datetime(2026, 2, 20), event_type=PENDING),
        entry("Listing Removed", datetime(2026, 3, 1), event_type=REMOVED),
    ])

    assert [row.event for row in session.added] == ["Listed", "Pending", "Listing Removed"]
    assert [row.price for row in session.added] == [450_000, None, None]


def test_an_already_stored_event_is_not_inserted_twice():
    stored = PriceHistory(
        event="Listing Removed", event_date=datetime(2026, 3, 1),
        event_type=REMOVED, is_rental_event=False,
    )
    session = FakeSession(existing_match=stored)

    changed = upsert(session, [entry("Listing Removed", datetime(2026, 3, 1), event_type=REMOVED)])

    assert changed is False
    assert session.added == []


def test_a_stored_events_derived_fields_are_refreshed_in_place():
    # event_type and is_rental_event are recomputed from the whole timeline on
    # every scrape, so a later scrape that sees more history can legitimately
    # correct an earlier verdict. Skipping matched rows outright meant a
    # "Price Changed" that was unresolvable on first sight stayed that way
    # forever, even once an earlier priced event arrived to compare against.
    stored = PriceHistory(
        event="Price Changed", event_date=datetime(2026, 3, 1), price=440_000,
        event_type=PRICE_CHANGED, is_rental_event=False,
    )
    session = FakeSession(existing_match=stored)

    changed = upsert(
        session,
        [entry("Price Changed", datetime(2026, 3, 1), price=440_000, event_type=REDUCED)],
    )

    assert changed is True, "a corrected event_type counts as a change worth rescoring"
    assert session.added == [], "corrected in place, not duplicated"
    assert stored.event_type == REDUCED


def test_an_event_repeated_within_one_payload_is_only_written_once():
    # Redfin repeats an event inside a single event_history from time to time
    # (a "Pending" on 2014-05-15 was found duplicated in one live response).
    # The database existence check can't see the first copy — it isn't flushed
    # yet — so without an in-batch guard both are added and the flush dies on
    # the uq_price_history_event violation, losing the whole property.
    session = FakeSession()

    upsert(session, [
        entry("Pending", datetime(2014, 5, 15), event_type=PENDING, source_event_id="S4726512"),
        entry("Pending", datetime(2014, 5, 15), event_type=PENDING, source_event_id="S4726512"),
    ])

    assert len(session.added) == 1


def test_an_event_stored_before_source_ids_were_captured_is_adopted_not_duplicated():
    # source_event_id was added to the schema after ~64k events had already
    # been written with NULL. Re-scraping those events now supplies an id, and
    # NULL IS NOT DISTINCT FROM 'O6424408' is false — so the row reads as a
    # brand new event. This is what doubled price_history during the backfill
    # (48,707 surplus rows). Adopt the existing row and fill in the id.
    stored = PriceHistory(
        event="Listed", event_date=datetime(2026, 1, 5), price=450_000,
        event_type=None, source_event_id=None, is_rental_event=False,
    )
    session = FakeSession(existing_match=stored)
    session.match_only_when_sid_is_null = True

    changed = upsert(session, [
        entry("Listed", datetime(2026, 1, 5), price=450_000, event_type=LISTED),
    ])

    assert session.added == [], "must not insert a second copy"
    assert stored.source_event_id == "O6424408", "the id should be backfilled onto the row"
    assert stored.event_type == LISTED
    assert changed is True


def test_rental_flag_is_persisted_on_insert():
    session = FakeSession()

    upsert(session, [
        dict(entry("Listed for Rent", datetime(2026, 4, 1), price=3_200), is_rental_event=True),
    ])

    assert session.added[0].is_rental_event is True


def test_undated_entries_are_skipped():
    session = FakeSession()

    changed = upsert(session, [entry("Pending", None, event_type=PENDING)])

    assert changed is False
    assert session.added == []


def test_dedup_lookup_compares_null_price_null_safely():
    # Without IS NOT DISTINCT FROM, `price = NULL` is NULL (never true), so
    # every unpriced event would look new on every rescrape and the table
    # would grow by ~32k rows a cycle.
    session = FakeSession()
    upsert(session, [entry("Listing Removed", datetime(2026, 3, 1), event_type=REMOVED)])

    sql = str(session.statements[0].compile(dialect=postgresql.dialect())).upper()

    assert sql.count("IS NOT DISTINCT FROM") == 2, "price and source_event_id must both be NULL-safe"


def test_dedup_lookup_keys_on_the_event_wording_too():
    # A Sold and the Listing Removed that follows it can share a timestamp;
    # keying on (property, source, date, price) alone collapsed them into one.
    session = FakeSession()
    upsert(session, [entry("Listing Removed", datetime(2026, 3, 1), event_type=REMOVED)])

    sql = str(session.statements[0].compile(dialect=postgresql.dialect()))

    assert "price_history.event = " in sql
    assert "price_history.event_date = " in sql
    assert "price_history.property_id = " in sql
