"""One E6 soak sample appended to logs/e6/soak.jsonl (run every 6 hours).

Records: cloud DB size, publisher runs since the previous sample (remote rows written per push,
zero-change pushes), checks rejected by the snapshot guard, and valuation rows whose version is not v2.
"""
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from aevorex.db.session import async_session_maker
from aevorex.publisher.remote import publisher_engine

LOG = Path("logs/e6/soak.jsonl")


async def main() -> None:
    now = datetime.now(UTC)
    since = None
    if LOG.exists() and LOG.read_text().strip():
        since = json.loads(LOG.read_text().strip().splitlines()[-1])["at"]
    since_dt = datetime.fromisoformat(since).replace(tzinfo=None) if since else datetime(2026, 10, 4, 0, 0)
    engine = publisher_engine()
    async with engine.connect() as conn:
        size = int(await conn.scalar(text("select pg_database_size(current_database())")))
        props = int(await conn.scalar(text("select count(*) from serving.properties")))
    await engine.dispose()
    async with async_session_maker() as s:
        pushes = (await s.execute(text(
            "select (counts->>'remote_rows_written')::int w from runs "
            "where kind='publish' and status in ('succeeded','partial') and started_at > :t"),
            {"t": since_dt})).scalars().all()
        rejected = int(await s.scalar(text(
            "select count(*) from runs where error_class='SnapshotRejected' and started_at > :t"),
            {"t": since_dt}) or 0)
        versions = (await s.execute(text(
            "select valuation_version, count(*) from property_valuation group by 1"))).all()
    sample = {
        "at": now.replace(tzinfo=None).isoformat(),
        "cloud_db_mib": round(size / 1048576, 2),
        "published_properties": props,
        "pushes": len(pushes),
        "pushes_zero_change": sum(1 for w in pushes if not w),
        "remote_rows_written_per_push_avg": round(sum(w or 0 for w in pushes) / len(pushes), 1) if pushes else None,
        "remote_rows_written_max": max((w or 0 for w in pushes), default=None),
        "checks_rejected_by_snapshot_guard": rejected,
        "valuation_versions": {v or "null": n for v, n in versions},
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as handle:
        handle.write(json.dumps(sample) + "\n")
    print(sample)


asyncio.run(main())
