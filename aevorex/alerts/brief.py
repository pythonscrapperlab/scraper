"""Morning brief: one ``morning_brief`` e-mail-queue row per member per org-market per local day.

A sweep runs every few minutes. For each org it asks the pure window rule whether the org's
local clock is in 07:00-11:00, and an idempotency key (``brief:<market>:<local date>:<user>``)
makes the sweep safe to repeat, to run late after the laptop slept, and to run across DST
changes. The engine never sends mail; it queues, and the web's sender honours ``send_after``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from aevorex.alerts.alerts import Recipient, insert_rows, recipient_from_row
from aevorex.alerts.rules import (
    LENSES,
    brief_count,
    brief_window,
    send_after,
)

LOGGER = logging.getLogger(__name__)
ACTIVE = ("active", "coming_soon", "new", "pending", "contingent")
EXAMPLE_CHANGES = 10


@dataclass
class BriefSweep:
    """Aggregate-only outcome of one sweep."""

    enqueued: int = 0
    already_queued: int = 0
    not_due: int = 0
    too_late: int = 0
    no_data: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_counts(self) -> dict[str, int]:
        return {
            "brief_enqueued": self.enqueued,
            "brief_already_queued": self.already_queued,
            "brief_not_due": self.not_due,
            "brief_too_late": self.too_late,
            "brief_no_data": self.no_data,
            "brief_failed": self.failed,
        }


_ORGS_SQL = text(
    "select o.id as org_id, o.plan, o.tz, o.default_lens, om.market_slug "
    "from app.orgs o join app.org_markets om on om.org_id = o.id and om.enabled "
    "order by o.id, om.market_slug"
)
_MEMBERS_SQL = text(
    "select m.org_id, m.user_id, ns.tier_alerts, ns.in_app, ns.email, ns.morning_brief, "
    "ns.quiet_hours_enabled, ns.quiet_start, ns.quiet_end "
    "from app.org_members m "
    "left join app.notification_settings ns on ns.org_id = m.org_id and ns.user_id = m.user_id"
)
_QUEUED_SQL = text(
    "select dedupe_key from app.email_queue "
    "where org_id = :org and dedupe_key like :prefix"
)
_MARKET_SQL = text(
    "select id, slug, last_checked_at, last_refreshed_at, check_status, listings_active, "
    "changed_last_check from serving.markets where slug = :slug"
)
_TOP_SQL = text(
    "select p.id as property_id, p.address, p.city, p.zip, p.price, p.beds, p.baths, p.sqft, "
    "p.dom, s.score, s.grade, s.percentile, s.tier "
    "from serving.scores s join serving.properties p on p.id = s.property_id "
    "where p.market_id = :mid and s.lens = :lens and p.delisted_at is null "
    "and p.listing_status_normalized = any(:active) "
    "order by s.percentile desc, s.score desc, p.id limit :n"
)
_CHANGES_SQL = text(
    "select kind, count(*) as n from serving.change_events "
    "where market_id = :mid and observed_at >= :since group by kind order by kind"
)
_EXAMPLES_SQL = text(
    "select ce.kind, ce.observed_at, p.address, p.price from serving.change_events ce "
    "join serving.properties p on p.id = ce.property_id "
    "where ce.market_id = :mid and ce.observed_at >= :since and p.delisted_at is null "
    "order by ce.observed_at desc limit :n"
)


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


async def build_brief_payload(
    connection: AsyncConnection,
    slug: str,
    lens: str,
    count: int,
    now: datetime,
) -> dict[str, Any] | None:
    """Top-N for the org's lens, 24 h of changes and a freshness block; None if no data."""
    market = (await connection.execute(_MARKET_SQL, {"slug": slug})).mappings().first()
    if market is None:
        return None
    top = (
        await connection.execute(
            _TOP_SQL, {"mid": market["id"], "lens": lens, "active": list(ACTIVE), "n": count}
        )
    ).mappings().all()
    if not top:
        return None
    since = now - timedelta(hours=24)
    kinds = (await connection.execute(_CHANGES_SQL, {"mid": market["id"], "since": since})).all()
    examples = (
        await connection.execute(
            _EXAMPLES_SQL, {"mid": market["id"], "since": since, "n": EXAMPLE_CHANGES}
        )
    ).mappings().all()
    status = str(market["check_status"])
    return {
        "market_slug": slug,
        "lens": lens,
        "top": [
            {**{k: row[k] for k in ("address", "city", "zip", "price", "beds", "baths",
                                    "sqft", "dom", "grade", "tier")},
             "property_id": str(row["property_id"]),
             "score": float(row["score"]), "percentile": float(row["percentile"])}
            for row in top
        ],
        "changes_24h": {
            "counts": {str(k): int(n) for k, n in kinds},
            "examples": [
                {"kind": r["kind"], "observed_at": _iso(r["observed_at"]),
                 "address": r["address"], "price": r["price"]} for r in examples
            ],
        },
        "freshness": {
            "last_checked_at": _iso(market["last_checked_at"]),
            "last_refreshed_at": _iso(market["last_refreshed_at"]),
            "check_status": status,
            "listings_active": market["listings_active"],
            "note": None if status == "ok"
            else f"Market check status is '{status}'; figures may be older than usual.",
        },
    }


async def run_brief_sweep(
    engine: AsyncEngine,
    now: datetime,
    *,
    counts_by_plan: Mapping[str, int] | None = None,
) -> BriefSweep:
    """Enqueue every brief that is due and not yet queued. Safe to call repeatedly."""
    result = BriefSweep()
    async with engine.connect() as connection:
        org_rows = (await connection.execute(_ORGS_SQL)).mappings().all()
        member_rows = (await connection.execute(_MEMBERS_SQL)).mappings().all()
    members: dict[UUID, list[Recipient]] = defaultdict(list)
    for row in member_rows:
        members[row["org_id"]].append(recipient_from_row(row))

    for row in org_rows:
        window = brief_window(now, str(row["tz"]))
        if not window.due:
            if window.reason == "too_late":
                result.too_late += 1
            else:
                result.not_due += 1
            continue
        recipients = [r for r in members.get(row["org_id"], []) if r.morning_brief and r.email]
        if not recipients:
            continue
        try:
            await _enqueue_one(engine, row, recipients, window.local_date, now, counts_by_plan,
                               result)
        except Exception as exc:  # one org's failure must not block the others
            result.failed += 1
            result.errors.append(type(exc).__name__)
            LOGGER.warning("Morning brief failed: error_class=%s", type(exc).__name__)
    return result


async def _enqueue_one(
    engine: AsyncEngine,
    row: Mapping[Any, Any],
    recipients: list[Recipient],
    local_date: date,
    now: datetime,
    counts_by_plan: Mapping[str, int] | None,
    result: BriefSweep,
) -> None:
    slug = str(row["market_slug"])
    prefix = f"brief:{slug}:{local_date.isoformat()}:"
    async with engine.begin() as connection:
        queued = {
            str(k) for k in (
                await connection.execute(
                    _QUEUED_SQL, {"org": row["org_id"], "prefix": prefix + "%"}
                )
            ).scalars().all()
        }
        pending = [r for r in recipients if f"{prefix}{r.user_id}" not in queued]
        result.already_queued += len(recipients) - len(pending)
        if not pending:
            return
        lens = str(row["default_lens"])
        if lens not in LENSES:
            lens = LENSES[0]
        payload = await build_brief_payload(
            connection, slug, lens, brief_count(str(row["plan"]), dict(counts_by_plan or {})), now
        )
        if payload is None:
            result.no_data += 1
            return
        rows = [{
            "org_id": row["org_id"],
            "user_id": r.user_id,
            "template": "morning_brief",
            "payload": {**payload, "local_date": local_date.isoformat()},
            "send_after": send_after(now, str(row["tz"]), r.quiet),
            "dedupe_key": f"{prefix}{r.user_id}",
        } for r in pending]
        result.enqueued += len(await insert_rows(
            connection, "email_queue", rows,
            ("org_id", "user_id", "template", "payload", "send_after", "dedupe_key"),
        ))
