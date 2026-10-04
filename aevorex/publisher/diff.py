"""Change detection for the publisher: hash the exact serving payloads, push only what differs.

Three hashes per property, so a change in one group never rewrites the others:

* ``content``  - the ``serving.properties`` row;
* ``scores``   - every ``serving.scores`` row of the property (all lenses);
* ``children`` - every child-table row (valuation, agents, images, comps, history, features,
  neighbourhood, tax_history).

Hashes cover the payload exactly as it would be sent, minus *volatile stamps* that move on
every scrape without the web's picture of the property changing (see ``VOLATILE_*``). Those
stamps still travel with the row whenever the row is sent for a real reason.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

# Re-stamped by every nightly refresh (or every day, for the counters) of an otherwise
# identical listing; hashing them would re-send the whole market nightly.
VOLATILE_PROPERTY_KEYS = frozenset(
    {"updated_at", "last_seen_at", "refreshed_at", "dom", "dom_mls"}
)
# Recomputed on every analysis pass even when the numbers come out identical.
VOLATILE_SCORE_KEYS = frozenset({"computed_at", "prev_score", "prev_percentile"})
VOLATILE_CHILD_KEYS = frozenset({"computed_at"})


def _default(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Unhashable publisher value type: {type(value).__name__}")


def digest(payload: object) -> str:
    """Stable SHA-256 of a JSON-able structure (sorted keys, no whitespace)."""
    encoded = json.dumps(payload, default=_default, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _without(row: Mapping[str, Any], keys: frozenset[str]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in keys}


def content_hash(row: Mapping[str, Any]) -> str:
    return digest(_without(row, VOLATILE_PROPERTY_KEYS))


def scores_hash(rows: Iterable[Mapping[str, Any]]) -> str:
    ordered = sorted(
        (_without(row, VOLATILE_SCORE_KEYS) for row in rows), key=lambda r: str(r["lens"])
    )
    return digest(ordered)


def children_hash(children: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    """Hash every child table's rows; each table's rows are sorted by their own digest."""
    payload: dict[str, list[str]] = {}
    for name in sorted(children):
        cleaned = [digest(_without(row, VOLATILE_CHILD_KEYS)) for row in children[name]]
        payload[name] = sorted(cleaned)
    return digest(payload)


@dataclass(frozen=True)
class KnownState:
    """The hashes recorded at the last successful push of one property ('' = never pushed)."""

    content: str = ""
    scores: str = ""
    children: str = ""


@dataclass
class ChangeSet:
    """Which properties need which group sent, and which remote rows must go."""

    properties: set[UUID] = field(default_factory=set)
    scores: set[UUID] = field(default_factory=set)
    children: set[UUID] = field(default_factory=set)
    departed: set[UUID] = field(default_factory=set)
    unchanged: int = 0


def plan_changes(
    *,
    current: Mapping[UUID, KnownState],
    known: Mapping[UUID, KnownState],
    remote_ids: set[UUID],
) -> ChangeSet:
    """Compare the freshly computed hashes with the last pushed ones.

    A property the cache does not hold (first push, rebuild, manual deletion) is re-sent in
    every group, whatever the local state says: the cache is the thing being kept in sync.
    """
    plan = ChangeSet()
    for property_id, now in current.items():
        before = known.get(property_id, KnownState())
        missing = property_id not in remote_ids
        changed = False
        if missing or before.content != now.content:
            plan.properties.add(property_id)
            changed = True
        if missing or before.scores != now.scores:
            plan.scores.add(property_id)
            changed = True
        if missing or before.children != now.children:
            plan.children.add(property_id)
            changed = True
        if not changed:
            plan.unchanged += 1
    plan.departed = remote_ids - set(current)
    return plan
