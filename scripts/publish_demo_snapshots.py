"""Rebuild only public demo snapshots; never enqueue alerts or send mail."""

import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import MetaData, Table

from aevorex.publisher.markets import demo_market_slugs, market_definition
from aevorex.publisher.service import LENSES, Publisher
from aevorex.publisher.snapshots import build_demo_snapshot


async def main() -> None:
    publisher = Publisher()
    try:
        rows = []
        slugs = sorted(demo_market_slugs())
        if not slugs:
            raise RuntimeError("NoDemoMarketsConfigured")
        for slug in slugs:
            definition = market_definition(slug)
            properties, freshness = await publisher._load_local(definition)
            for lens in LENSES:
                payload = build_demo_snapshot(definition, lens, properties, freshness)
                rows.append({"market_slug": slug, "lens": lens, "payload": payload,
                             "generated_at": publisher.now})
        destination = Path(sys.argv[1])
        destination.write_text(json.dumps(rows, default=str, indent=2), encoding="utf-8")
        if "--publish" in sys.argv:
            # This operation writes only the unchanged snapshot table contract.
            # Do not bypass the full publisher's guard for its other table groups.
            metadata = MetaData()
            async with publisher.store.engine.connect() as connection:
                table = await connection.run_sync(lambda sync: Table(
                    "demo_snapshots", metadata, schema="serving", autoload_with=sync
                ))
            if set(table.c.keys()) != {"market_slug", "lens", "payload", "generated_at"}:
                raise RuntimeError("DemoSnapshotSchemaDrift")
            publisher.store.tables["demo_snapshots"] = table
            async with publisher.store.transaction() as connection:
                await publisher.store.upsert(connection, "demo_snapshots", rows,
                                             conflict=("market_slug", "lens"))
        print(json.dumps({"snapshots": len(rows), "published": "--publish" in sys.argv}))
    finally:
        await publisher.close()


if __name__ == "__main__":
    asyncio.run(main())
