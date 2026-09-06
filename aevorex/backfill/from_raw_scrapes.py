"""
Re-derive `properties`, `price_history` and `tax_history` from stored raw payloads.

WHY
---
A normalizer fix landed at 2026-08-16 23:00 and was never backfilled, so
95.9% of properties are missing MLS status, ai_summary, price-drop counts and
area market context — and 94.3% of price events have no `event_type`. Worse,
the pre-fix normalizer dropped every unpriced event on insert, so "Listing
Removed", "Pending", "Contingent" and "Relisted" — the motivated-seller
signals — are absent from the historical set entirely, not merely untyped.

None of that needs re-scraping. `raw_scrapes` retained the full payload for
all 11,218 fetches (status on 100%, ai_summary on 98%), so this is a purely
local recomputation.

WHAT IT TOUCHES
---------------
Only the three tables whose contents the ingestion fixes actually changed:

  properties     every scalar column the normalizer produces, including the
                 distress block and the economics promoted out of `meta`.
  price_history  inserts the previously-dropped unpriced events, and
                 refreshes the derived `event_type` / `is_rental_event` on
                 rows that already exist.
  tax_history    land/improvement components and the corrected
                 `assessed_value` semantics.

Deliberately NOT touched: schools, points_of_interest, transport_stops,
property_images, property_comps. Nothing in the fixes changed how those are
extracted, and the pipeline replaces them wholesale (delete + re-insert), so
running them here would churn ~800,000 rows to write back identical data.

SAFETY
------
- Dry-run by default. `--apply` is required to write anything.
- Matches strictly on `redfin_id`. It never runs the address/APN dedup
  fallback, so it cannot merge two existing properties into one — a backfill
  must not silently change the shape of the table it's repairing.
- Commits per property, so an interruption keeps everything already done.
- Idempotent: re-running produces the same result.

USAGE
-----
    python -m aevorex.backfill.from_raw_scrapes                 # dry run
    python -m aevorex.backfill.from_raw_scrapes --limit 50      # sample
    python -m aevorex.backfill.from_raw_scrapes --apply         # commit
"""

import argparse
import asyncio
import json
import logging
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aevorex.db.deduplicator import (
    SOURCE_PRICES_META_KEY,
    Deduplicator,
    SATELLITE_KEYS,
)
from aevorex.db.models import Property, RawScrape, utc_now
from aevorex.db.session import async_session_maker, close_engine
from aevorex.normalizers.redfin import RedfinNormalizer
from aevorex.pipeline.runner import PipelineRunner

logger = logging.getLogger("aevorex.backfill")

SOURCE = "redfin"

# Junk prices already sitting in price_history from before the sanity guard
# existed. Set to NULL rather than deleted: the event is a real state
# transition on a real date, and `price` is nullable precisely so those
# survive. Mirrors RedfinNormalizer._sane_price, and must run before the
# per-property upsert or the normalizer's NULL-priced version of the same
# event won't match the stored row and would be inserted alongside it.
NEUTRALISE_BAD_PRICES_SQL = text("""
    UPDATE price_history
       SET price = NULL
     WHERE price IS NOT NULL
       AND (price <= 0 OR price >= :max_price OR price = :int32_max)
""")

# Repairs events duplicated by the source_event_id NULL mismatch.
#
# `source_event_id` was added to the schema after ~64,000 events had already
# been stored with NULL. When a re-scrape supplies the id, the dedup key
# compares NULL against 'O6424898' with IS NOT DISTINCT FROM, which is false,
# so the same event reads as new and gets inserted a second time. The first
# run of this backfill did exactly that, 48,245 times.
#
# PipelineRunner._upsert_price_history now adopts the NULL-id row instead of
# duplicating it, so this cannot recur — this clears what already happened.
#
# Only deletes a NULL-id row when a row carrying an id exists for the very
# same (property, source, event wording, timestamp, price). A NULL-id event
# with no such twin is a real event the source never gave an id for, and is
# left alone.
REPAIR_DUPLICATE_EVENTS_SQL = text("""
    DELETE FROM price_history a
     WHERE a.source_event_id IS NULL
       AND EXISTS (
           SELECT 1 FROM price_history b
            WHERE b.property_id = a.property_id
              AND b.source = a.source
              AND b.event = a.event
              AND b.event_date = a.event_date
              AND COALESCE(b.price, -1) = COALESCE(a.price, -1)
              AND b.source_event_id IS NOT NULL
       )
""")


class BackfillStats(Counter):
    """Counter with a readable summary."""

    def report(self) -> str:
        width = max((len(k) for k in self), default=0)
        return "\n".join(f"  {k:<{width}}  {v:>7,}" for k, v in sorted(self.items()))


async def latest_payload_by_redfin_id(session: AsyncSession) -> Dict[str, dict]:
    """
    The most recent successful payload for every scraped listing ID.

    Loaded up front in one pass: 11,218 rows keyed by ID beats 6,854
    correlated subqueries, and the payloads are needed in full anyway.
    """
    newest = (
        select(
            RawScrape.external_id,
            func.max(RawScrape.scraped_at).label("scraped_at"),
        )
        .where(
            RawScrape.external_id.is_not(None),
            RawScrape.scrape_status == "success",
        )
        .group_by(RawScrape.external_id)
        .subquery()
    )

    rows = await session.execute(
        select(RawScrape.external_id, RawScrape.raw_json).join(
            newest,
            (RawScrape.external_id == newest.c.external_id)
            & (RawScrape.scraped_at == newest.c.scraped_at),
        )
    )

    payloads: Dict[str, dict] = {}
    for external_id, raw_json in rows:
        payload = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
        if isinstance(payload, dict):
            # Several scrapes can share the newest timestamp; any of them is
            # equally current, so first-wins is fine and keeps this stable.
            payloads.setdefault(external_id, payload)
    return payloads


def apply_scalar_columns(prop: Property, normalized: dict) -> List[str]:
    """
    Copy the normalizer's scalar output onto an existing Property.

    Returns the names of columns whose value actually changed, which is what
    makes the dry-run report meaningful rather than just a row count.

    None is skipped, matching the live pipeline: a field the source didn't
    send this time shouldn't wipe a value an earlier scrape did send. The new
    distress booleans are unaffected by that rule — they are False (not None)
    when the text was read and nothing matched, and None only when there was
    no text at all, which is exactly the distinction worth preserving.
    """
    changed: List[str] = []
    for key, value in normalized.items():
        if value is None or key in SATELLITE_KEYS:
            continue
        if key in ("zillow_id", "redfin_id", "realtor_id", "primary_source"):
            continue
        if not hasattr(prop, key):
            logger.warning("Normalizer produced unknown column %r; skipping.", key)
            continue
        if getattr(prop, key) != value:
            changed.append(key)
            setattr(prop, key, value)
    return changed


def apply_derived_columns(prop: Property, normalized: dict) -> List[str]:
    """Recompute primary_source, the per-source price map, and source_count."""
    changed: List[str] = []

    primary = Deduplicator._pick_primary_source(prop.primary_source, SOURCE)
    if primary != prop.primary_source:
        prop.primary_source = primary
        changed.append("primary_source")

    price = normalized.get("price")
    source_prices = Deduplicator._source_prices(prop.meta)
    if price:
        source_prices[SOURCE] = int(price)
    if source_prices:
        meta = dict(prop.meta or {})
        if meta.get(SOURCE_PRICES_META_KEY) != source_prices:
            meta[SOURCE_PRICES_META_KEY] = source_prices
            prop.meta = meta
            changed.append("meta.source_prices")

    variance = Deduplicator.calculate_price_variance(list(source_prices.values()))
    if variance != prop.price_variance:
        prop.price_variance = variance
        changed.append("price_variance")

    count = Deduplicator._count_sources(prop)
    if count != prop.source_count:
        prop.source_count = count
        changed.append("source_count")

    return changed


async def backfill_one(
    session: AsyncSession,
    prop: Property,
    payload: dict,
    normalizer: RedfinNormalizer,
) -> Optional[Tuple[List[str], bool, bool]]:
    """
    Recompute one property. Returns (changed columns, price_history changed,
    tax_history changed), or None if the payload couldn't be normalized.
    """
    normalized = await normalizer.normalize(payload)
    if normalized is None:
        return None

    changed = apply_scalar_columns(prop, normalized)
    changed += apply_derived_columns(prop, normalized)

    price_changed = await PipelineRunner._upsert_price_history(
        session, prop.id, SOURCE, normalized.get("price_history") or []
    )
    tax_changed = await PipelineRunner._upsert_tax_history(
        session, prop.id, SOURCE, normalized.get("tax_history") or []
    )

    if changed or price_changed or tax_changed:
        # Every scorer reads at least one of these, so anything that moved
        # here invalidates the stored analysis.
        prop.needs_analysis = True
        prop.updated_at = utc_now()

    return changed, price_changed, tax_changed


async def run(apply: bool, limit: Optional[int]) -> BackfillStats:
    stats = BackfillStats()
    column_hits: Counter = Counter()
    normalizer = RedfinNormalizer()

    async with async_session_maker() as session:
        if apply:
            result = await session.execute(
                NEUTRALISE_BAD_PRICES_SQL,
                {
                    "max_price": RedfinNormalizer.MAX_PLAUSIBLE_PRICE,
                    "int32_max": RedfinNormalizer.INT32_MAX,
                },
            )
            await session.commit()
            stats["implausible_prices_nulled"] = result.rowcount or 0
            logger.info("Neutralised %s implausible stored price(s).", result.rowcount)

            repaired = await session.execute(REPAIR_DUPLICATE_EVENTS_SQL)
            await session.commit()
            stats["duplicate_events_removed"] = repaired.rowcount or 0
            logger.info("Removed %s duplicated price event(s).", repaired.rowcount)

        logger.info("Loading latest raw payload per listing ID...")
        payloads = await latest_payload_by_redfin_id(session)
        logger.info("Loaded %s payloads.", len(payloads))

        # IDs first, then load each Property fresh inside the loop. Both a
        # commit and a rollback can expire the identity map, and touching an
        # expired ORM object outside the async context raises MissingGreenlet
        # — so holding a long-lived list of instances across transaction
        # boundaries is exactly the thing to avoid here. One extra primary-key
        # lookup per property is a cheap price for that.
        query = select(Property.id).where(Property.redfin_id.is_not(None)).order_by(Property.created_at)
        if limit:
            query = query.limit(limit)
        property_ids = list((await session.execute(query)).scalars().all())
        stats["properties_examined"] = len(property_ids)

        for index, property_id in enumerate(property_ids, start=1):
            prop = await session.get(Property, property_id)
            if prop is None:
                stats["skipped_property_vanished"] += 1
                continue

            payload = payloads.get(prop.redfin_id)
            if payload is None:
                stats["skipped_no_raw_payload"] += 1
                continue

            try:
                outcome = await backfill_one(session, prop, payload, normalizer)
            except Exception:
                await session.rollback()
                stats["errors"] += 1
                logger.error("Failed backfilling property %s", property_id, exc_info=True)
                continue

            if outcome is None:
                await session.rollback()
                stats["skipped_normalize_returned_none"] += 1
                continue

            changed, price_changed, tax_changed = outcome
            column_hits.update(changed)
            if changed:
                stats["properties_with_column_changes"] += 1
            if price_changed:
                stats["properties_with_price_history_changes"] += 1
            if tax_changed:
                stats["properties_with_tax_history_changes"] += 1
            if not (changed or price_changed or tax_changed):
                stats["already_current"] += 1

            if apply:
                # Per-property commit, mirroring the pipeline: an interrupted
                # backfill keeps everything it already finished.
                await session.commit()
            else:
                await session.rollback()

            if index % 500 == 0:
                logger.info("  ... %s/%s", index, len(property_ids))

    logger.info("\nMost-changed columns:")
    for column, hits in column_hits.most_common(30):
        logger.info("  %-30s %6d", column, hits)

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write. Without this the run is a dry run and rolls back.",
    )
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N properties.")
    parser.add_argument("--quiet", action="store_true", help="Warnings and above only.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )
    # The normalizer logs a warning per implausible price and per placeholder;
    # across 6,854 properties that buries the progress output.
    logging.getLogger("aevorex.normalizers.redfin").setLevel(logging.ERROR)

    async def _main():
        try:
            stats = await run(apply=args.apply, limit=args.limit)
        finally:
            await close_engine()
        mode = "APPLIED" if args.apply else "DRY RUN (nothing written)"
        print(f"\n=== Backfill {mode} ===")
        print(stats.report())

    asyncio.run(_main())


if __name__ == "__main__":
    main()
