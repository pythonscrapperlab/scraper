"""Apply one supabase/migrations/*.sql file to the linked project and record it in the history.

Stands in for `supabase db push` on a machine without the Supabase CLI. One transaction: the SQL
and the history row commit together or not at all. Refuses to run a version already recorded.

Usage: python scripts/apply_supabase_migration.py supabase/migrations/<version>_<name>.sql
"""
import asyncio
import sys
from pathlib import Path

import asyncpg
from sqlalchemy.engine import make_url

from aevorex.config import settings


async def main(path: Path) -> None:
    version, _, name = path.stem.partition("_")
    secret = settings.supabase_direct_connection_url
    if secret is None:
        raise SystemExit("SUPABASE_DIRECT_CONNECTION_URL is required")
    url = make_url(secret.get_secret_value())
    conn = await asyncpg.connect(
        host=url.host, port=url.port, user=url.username, password=url.password,
        database=url.database, timeout=30, statement_cache_size=0,
    )
    try:
        if await conn.fetchval(
            "select 1 from supabase_migrations.schema_migrations where version = $1", version
        ):
            raise SystemExit(f"Migration {version} is already recorded; nothing applied.")
        sql = path.read_text(encoding="utf-8")
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute(
                "insert into supabase_migrations.schema_migrations (version, name, statements) "
                "values ($1, $2, $3)", version, name, [sql],
            )
        print(f"Applied {version} ({name}).")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
