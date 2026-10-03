"""Threshold alerts: publisher step 6 of AGENTS.md section 8.

The engine only *enqueues* (``app.alert_events`` and ``app.email_queue``); it never sends mail.
Crossings are detected against the serving cache's state *before* the push, so a repeated
push of unchanged scores produces nothing, and a first-ever push of a market is a silent
baseline rather than an alert storm.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from aevorex.alerts.rules import (
    LENSES,
    QuietHours,
    ScoreState,
    Threshold,
    crossed,
    send_after,
)

LOGGER = logging.getLogger(__name__)
MAX_EMAIL_ITEMS = 20

ScoreKey = tuple[str, str]  # (property_id, lens)


@dataclass(frozen=True)
class Recipient:
    """A member's delivery preferences; missing settings rows mean the column defaults."""

    user_id: UUID
    tier_alerts: bool = True
    in_app: bool = True
    email: bool = True
    morning_brief: bool = True
    quiet: QuietHours = field(default_factory=QuietHours)


@dataclass(frozen=True)
class OrgRules:
    """Everything the engine needs to know about one org for one market."""

    org_id: UUID
    plan: str
    tz: str
    default_lens: str
    thresholds: Mapping[str, Threshold]
    recipients: Sequence[Recipient]


@dataclass(frozen=True)
class Candidate:
    """A newly scored, still-listed property with its pre-push and post-push state."""

    property_id: str
    lens: str
    new: ScoreState
    old: ScoreState | None
    score: float
    grade: str
    computed_at: datetime
    address: str
    price: int | None


@dataclass
class AlertPlan:
    """Rows to insert; pure data so the crossing and quiet-hours logic can be unit-tested."""

    events: list[dict[str, Any]] = field(default_factory=list)
    emails: list[dict[str, Any]] = field(default_factory=list)
    deferred: int = 0


@dataclass
class AlertCounts:
    """Aggregate-only telemetry (no property data)."""

    events: int = 0
    emails: int = 0
    deferred_quiet: int = 0
    baseline_suppressed: int = 0

    def as_counts(self) -> dict[str, int]:
        return {
            "alert_events": self.events,
            "alert_emails": self.emails,
            "alert_emails_deferred_quiet": self.deferred_quiet,
            "alert_baseline_suppressed": self.baseline_suppressed,
        }


def _dedupe_event(candidate: Candidate) -> str:
    return f"{candidate.property_id}:{candidate.lens}:{candidate.computed_at.astimezone(UTC).isoformat()}"


def _payload(candidate: Candidate, reason: str) -> dict[str, Any]:
    return {
        "property_id": candidate.property_id,
        "lens": candidate.lens,
        "reason": reason,  # "new_qualifier" (no earlier score) or "crossed"
        "score": candidate.score,
        "grade": candidate.grade,
        "percentile": candidate.new.percentile,
        "tier": candidate.new.tier,
        "previous_percentile": candidate.old.percentile if candidate.old else None,
        "previous_tier": candidate.old.tier if candidate.old else None,
        "address": candidate.address,
        "price": candidate.price,
    }


def plan_alerts(
    org: OrgRules,
    market_slug: str,
    candidates: Iterable[Candidate],
    now: datetime,
) -> AlertPlan:
    """Decide which alert events and e-mails one org gets for one push."""
    plan = AlertPlan()
    fired: list[Candidate] = []
    for candidate in candidates:
        threshold = org.thresholds.get(candidate.lens)
        if threshold is None or not crossed(candidate.old, candidate.new, threshold):
            continue
        fired.append(candidate)
    if not fired:
        return plan
    fired.sort(key=lambda c: (-c.new.percentile, c.property_id, c.lens))

    if any(r.tier_alerts and r.in_app for r in org.recipients):
        for candidate in fired:
            plan.events.append({
                "org_id": org.org_id,
                "property_id": candidate.property_id,
                "market_slug": market_slug,
                "lens": candidate.lens,
                "tier": candidate.new.tier,
                "payload": _payload(
                    candidate, "new_qualifier" if candidate.old is None else "crossed"
                ),
                "dedupe_key": _dedupe_event(candidate),
            })

    digest = hashlib.sha1(  # noqa: S324 - an idempotency key, not a security control
        "|".join(sorted(_dedupe_event(c) for c in fired)).encode()
    ).hexdigest()[:16]
    for recipient in org.recipients:
        if not (recipient.tier_alerts and recipient.email):
            continue
        release = send_after(now, org.tz, recipient.quiet)
        if release > now:
            plan.deferred += 1
        plan.emails.append({
            "org_id": org.org_id,
            "user_id": recipient.user_id,
            "template": "threshold_alert",
            "payload": {
                "market_slug": market_slug,
                "total": len(fired),
                "items": [_payload(c, "crossed" if c.old else "new_qualifier")
                          for c in fired[:MAX_EMAIL_ITEMS]],
            },
            "send_after": release,
            "dedupe_key": f"alert:{market_slug}:{recipient.user_id}:{digest}",
        })
    return plan


# ------------------------------------------------------------------------------ database
_ORG_RULES_SQL = text(
    "select o.id as org_id, o.plan, o.tz, o.default_lens, t.lens, t.min_percentile, t.tier "
    "from app.org_markets om join app.orgs o on o.id = om.org_id "
    "left join app.thresholds t on t.org_id = o.id and t.enabled "
    "where om.market_slug = :slug and om.enabled order by o.id"
)
_RECIPIENTS_SQL = text(
    "select m.org_id, m.user_id, ns.tier_alerts, ns.in_app, ns.email, ns.morning_brief, "
    "ns.quiet_hours_enabled, ns.quiet_start, ns.quiet_end "
    "from app.org_members m join app.org_markets om on om.org_id = m.org_id "
    "left join app.notification_settings ns on ns.org_id = m.org_id and ns.user_id = m.user_id "
    "where om.market_slug = :slug and om.enabled"
)


def _flag(value: Any, default: bool = True) -> bool:
    return default if value is None else bool(value)


def recipient_from_row(row: Mapping[Any, Any]) -> Recipient:
    defaults = QuietHours()
    start = row["quiet_start"] if isinstance(row["quiet_start"], time) else defaults.start
    end = row["quiet_end"] if isinstance(row["quiet_end"], time) else defaults.end
    return Recipient(
        user_id=row["user_id"],
        tier_alerts=_flag(row["tier_alerts"]),
        in_app=_flag(row["in_app"]),
        email=_flag(row["email"]),
        morning_brief=_flag(row["morning_brief"]),
        quiet=QuietHours(_flag(row["quiet_hours_enabled"]), start, end),
    )


async def load_org_rules(connection: AsyncConnection, market_slug: str) -> list[OrgRules]:
    """Orgs with this market enabled, their thresholds and their members' preferences."""
    org_rows = (await connection.execute(_ORG_RULES_SQL, {"slug": market_slug})).mappings().all()
    member_rows = (
        await connection.execute(_RECIPIENTS_SQL, {"slug": market_slug})
    ).mappings().all()
    members: dict[UUID, list[Recipient]] = defaultdict(list)
    for row in member_rows:
        members[row["org_id"]].append(recipient_from_row(row))
    orgs: dict[UUID, dict[str, Any]] = {}
    for row in org_rows:
        entry = orgs.setdefault(
            row["org_id"],
            {"plan": row["plan"], "tz": row["tz"], "lens": row["default_lens"], "thresholds": {}},
        )
        if row["lens"] in LENSES:
            minimum = None if row["min_percentile"] is None else float(row["min_percentile"])
            entry["thresholds"][row["lens"]] = Threshold(minimum, row["tier"])
    return [
        OrgRules(
            org_id=org_id,
            plan=str(entry["plan"]),
            tz=str(entry["tz"]),
            default_lens=str(entry["lens"]),
            thresholds=entry["thresholds"],
            recipients=members.get(org_id, []),
        )
        for org_id, entry in orgs.items()
    ]


async def market_score_states(
    connection: AsyncConnection, market_id: UUID
) -> dict[ScoreKey, ScoreState]:
    """The serving cache's current (percentile, tier) per property and lens in a market."""
    rows = (
        await connection.execute(
            text(
                "select s.property_id, s.lens, s.percentile, s.tier from serving.scores s "
                "join serving.properties p on p.id = s.property_id where p.market_id = :mid"
            ),
            {"mid": market_id},
        )
    ).all()
    return {(str(r[0]), str(r[1])): ScoreState(float(r[2]), str(r[3])) for r in rows}


async def insert_rows(connection: AsyncConnection, table: str, rows: Sequence[Mapping[str, Any]],
                      columns: Sequence[str]) -> set[str]:
    """Insert, skipping rows whose dedupe key exists; returns the keys actually inserted."""
    if not rows:
        return set()
    cols = ",".join(f'"{c}"' for c in columns)
    payload = json.dumps(
        [{c: row[c] for c in columns} for row in rows], default=_json_default,
        separators=(",", ":"),
    )
    result = await connection.execute(
        text(
            f"insert into app.{table} ({cols}) select {cols} from "
            f"jsonb_populate_recordset(null::app.{table}, cast(:payload as jsonb)) "
            "on conflict (org_id, dedupe_key) where dedupe_key is not null do nothing "
            "returning dedupe_key"
        ),
        {"payload": payload},
    )
    return {str(key) for key in result.scalars().all()}


def _json_default(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported alert value type: {type(value).__name__}")


async def write_plan(connection: AsyncConnection, plan: AlertPlan, now: datetime) -> tuple[int, int, int]:
    """Write a plan; returns (events inserted, e-mails inserted, of which deferred)."""
    events = await insert_rows(
        connection, "alert_events", plan.events,
        ("org_id", "property_id", "market_slug", "lens", "tier", "payload", "dedupe_key"),
    )
    emails = await insert_rows(
        connection, "email_queue", plan.emails,
        ("org_id", "user_id", "template", "payload", "send_after", "dedupe_key"),
    )
    deferred = sum(
        1 for row in plan.emails if row["dedupe_key"] in emails and row["send_after"] > now
    )
    return len(events), len(emails), deferred


async def enqueue_threshold_alerts(
    connection: AsyncConnection,
    market_slug: str,
    candidates: Sequence[Candidate],
    now: datetime,
    *,
    baseline: bool,
) -> AlertCounts:
    """Evaluate every subscribed org and write their alert rows.

    ``baseline`` is True when the market had no scores in the cache before this push (first
    push, or a rebuild): everything would look like a crossing, so nothing is enqueued.
    """
    counts = AlertCounts()
    if baseline:
        counts.baseline_suppressed = len(candidates)
        return counts
    for org in await load_org_rules(connection, market_slug):
        if not org.thresholds:
            continue
        plan = plan_alerts(org, market_slug, candidates, now)
        events, emails, deferred = await write_plan(connection, plan, now)
        counts.events += events
        counts.emails += emails
        counts.deferred_quiet += deferred
    if counts.events or counts.emails:
        LOGGER.info(
            "Alerts enqueued: market=%s events=%s emails=%s deferred_quiet=%s",
            market_slug, counts.events, counts.emails, counts.deferred_quiet,
        )
    return counts
