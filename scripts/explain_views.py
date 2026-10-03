"""EXPLAIN ANALYZE the web views on a real market, as a Pro org member, then roll back.

Usage:  python scripts/explain_views.py [market-slug]

Everything runs in one transaction that is always rolled back: a throwaway user/org is created
only to make RLS evaluate exactly as it does for the web, and nothing persists.
Output is aggregate plan text only (no row data).
"""

from __future__ import annotations

import asyncio
import re
import sys
from uuid import uuid4

from sqlalchemy import text

from aevorex.publisher.remote import publisher_engine

QUERIES = {
    "v_shortlist top 50 by motivated_seller percentile": (
        "select * from serving.v_shortlist where market_slug = :slug "
        "order by motivated_seller_percentile desc nulls last limit 50"
    ),
    "v_shortlist tier=top on fix_flip": (
        "select property_id, address, fix_flip_percentile from serving.v_shortlist "
        "where market_slug = :slug and fix_flip_tier = 'top' order by fix_flip_percentile desc"
    ),
    "v_shortlist zip filter + price cap": (
        "select property_id, price from serving.v_shortlist where market_slug = :slug "
        "and zip = (select zip from serving.properties where market_id = "
        "(select id from serving.markets where slug = :slug) limit 1) "
        "and price <= 600000 order by price"
    ),
    "v_property single": (
        "select * from serving.v_property where property_id = "
        "(select id from serving.properties where market_id = "
        "(select id from serving.markets where slug = :slug) limit 1)"
    ),
    "v_agent by market": "select * from serving.v_agent where market_slug = :slug",
    "v_market_public": "select * from serving.v_market_public",
}


async def main(slug: str) -> None:
    engine = publisher_engine()
    user, org = uuid4(), uuid4()
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(text("insert into auth.users (id) values (:u)"), {"u": user})
            await connection.execute(
                text("insert into app.orgs (id, name, plan) values (:o, 'explain-probe', 'pro')"),
                {"o": org},
            )
            await connection.execute(
                text("insert into app.org_members (org_id, user_id, role) values (:o, :u, 'owner')"),
                {"o": org, "u": user},
            )
            await connection.execute(
                text("insert into app.org_markets (org_id, market_slug) values (:o, :s)"),
                {"o": org, "s": slug},
            )
            await connection.execute(text("set local role authenticated"))
            await connection.execute(
                text("select set_config('request.jwt.claim.sub', :u, true), "
                     "set_config('request.jwt.claims', :c, true)"),
                {"u": str(user), "c": f'{{"sub": "{user}", "role": "authenticated"}}'},
            )
            for name, sql in QUERIES.items():
                rows = (
                    await connection.execute(
                        text(f"explain (analyze, buffers) {sql}"), {"slug": slug}
                    )
                ).scalars().all()
                plan = "\n".join(rows)
                timing = re.search(r"Execution Time: ([\d.]+) ms", plan)
                planning = re.search(r"Planning Time: ([\d.]+) ms", plan)
                print(f"=== {name}\nplanning {planning.group(1) if planning else '?'} ms, "
                      f"execution {timing.group(1) if timing else '?'} ms")
                if "--plans" in sys.argv:
                    print(plan)
        finally:
            await transaction.rollback()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main(next((a for a in sys.argv[1:] if not a.startswith("--")), "orlando-fl")))
