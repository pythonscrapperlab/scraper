"""Estimate serving bytes per published property from local data (no network, no writes).

Usage: python scripts/measure_payload.py orlando-fl [more slugs]
Payload JSON bytes understate Postgres on-disk size (tuple headers, indexes) by ~1.3-1.8x.
"""
import asyncio
import json
import sys
from collections import Counter
from typing import Any
from uuid import UUID

from aevorex.publisher.diff import _default
from aevorex.publisher.markets import market_definition
from aevorex.publisher.service import Publisher, _market_id


def size(value: Any) -> int:
    return len(json.dumps(value, default=_default, separators=(",", ":")).encode())


async def main(slugs: list[str]) -> None:
    publisher = Publisher.__new__(Publisher)
    from aevorex.db.session import async_session_maker
    publisher.local_sessions = async_session_maker
    publisher._fixed_now = None
    for slug in slugs:
        definition = market_definition(slug)
        properties, _ = await publisher._load_local(definition)
        market_id: UUID = _market_id(slug)
        totals: Counter[str] = Counter()
        for item in properties:
            totals["properties"] += size(publisher._property_row(item, market_id))
            for name, rows in publisher._children_for(item).items():
                totals[name] += size(rows)
        scores, _rejected = publisher._score_rows(properties)
        totals["scores"] = sum(size(row) for row in scores)
        n = max(len(properties), 1)
        print(f"{slug}: {len(properties)} properties, {len(scores)} score rows")
        for name, total in totals.most_common():
            print(f"  {name:14}{total / n / 1024:8.2f} KB/property")
        print(f"  {'TOTAL':14}{sum(totals.values()) / n / 1024:8.2f} KB/property (payload JSON)")


asyncio.run(main(sys.argv[1:]))
