"""VACUUM FULL ANALYZE every serving table (ACCESS EXCLUSIVE per table, one at a time)."""
import asyncio
import time

import asyncpg
from sqlalchemy.engine import make_url

from aevorex.config import settings


async def main() -> None:
    url = make_url(settings.supabase_direct_connection_url.get_secret_value())
    conn = await asyncpg.connect(
        host=url.host, port=url.port, user=url.username, password=url.password,
        database=url.database, timeout=30, statement_cache_size=0,
    )
    try:
        await conn.execute("set statement_timeout = 0")
        tables = [r["relname"] for r in await conn.fetch(
            "select c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace "
            "where n.nspname='serving' and c.relkind='r' order by pg_total_relation_size(c.oid)")]
        for name in tables:
            start = time.monotonic()
            await conn.execute(f'vacuum (full, analyze) serving."{name}"')
            print(f"{name:16}{time.monotonic() - start:7.1f}s", flush=True)
    finally:
        await conn.close()


asyncio.run(main())
