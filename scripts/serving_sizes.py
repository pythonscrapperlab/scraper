"""Report Supabase serving size: database total plus per-table size / live / dead tuples.

Read-only. Usage: python scripts/serving_sizes.py [label]   (writes logs/e6/<label>.json)
"""
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import text

from aevorex.publisher.remote import publisher_engine

TABLES_SQL = text("""
    select c.relname as table_name,
           pg_total_relation_size(c.oid) as total_bytes,
           pg_relation_size(c.oid) as heap_bytes,
           coalesce(pg_total_relation_size(c.reltoastrelid), 0) as toast_bytes,
           pg_indexes_size(c.oid) as index_bytes,
           coalesce(s.n_live_tup, 0) as live,
           coalesce(s.n_dead_tup, 0) as dead
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    left join pg_stat_user_tables s on s.relid = c.oid
    where n.nspname = 'serving' and c.relkind = 'r'
    order by total_bytes desc
""")


async def main(label: str) -> None:
    engine = publisher_engine()
    async with engine.connect() as conn:
        db_bytes = int(await conn.scalar(text("select pg_database_size(current_database())")))
        rows = [dict(r) for r in (await conn.execute(TABLES_SQL)).mappings().all()]
        published = int(await conn.scalar(text("select count(*) from serving.properties")))
    await engine.dispose()
    report = {
        "label": label,
        "pg_database_size_bytes": db_bytes,
        "pg_database_size_mib": round(db_bytes / 1048576, 2),
        "published_properties": published,
        "kb_per_property": round(sum(r["total_bytes"] for r in rows) / 1024 / max(published, 1), 2),
        "tables": rows,
    }
    Path("logs/e6").mkdir(parents=True, exist_ok=True)
    Path(f"logs/e6/{label}.json").write_text(json.dumps(report, indent=1))
    print(f"pg_database_size {report['pg_database_size_mib']} MiB; properties {published}; "
          f"serving KB/property {report['kb_per_property']}")
    print(f"{'table':16}{'total KiB':>11}{'heap':>9}{'toast':>9}{'index':>9}{'live':>9}{'dead':>9}")
    for r in rows:
        print(f"{r['table_name']:16}{r['total_bytes']//1024:>11}{r['heap_bytes']//1024:>9}"
              f"{r['toast_bytes']//1024:>9}{r['index_bytes']//1024:>9}{r['live']:>9}{r['dead']:>9}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "snapshot"))
