"""Read-only proof: re-run valuation on stored-v2 rows and compare (never commits)."""
import asyncio
import datetime as dt
import inspect
import re
import sys

from sqlalchemy import select, text

from aevorex.db.models import Property, PropertyValuation
from aevorex.db.session import async_session_maker
from aevorex.valuation.engine import ValuationEngine
from aevorex.valuation.rent import build_zip_bed_rent_index

FIELDS = ("market_value", "arv", "rehab_cost_mid", "rent_estimate_monthly")
TOL = 0.01


def _close(old, new) -> bool:
    if old is None or new is None:
        return old is None and new is None
    old, new = float(old), float(new)
    return abs(new - old) <= TOL * max(abs(old), 1.0)


async def main(n: int, as_of=None) -> int:
    async with async_session_maker() as session:
        ids = (await session.execute(text(
            "select property_id from property_valuation where valuation_version='v2' "
            "order by md5(property_id::text) limit :n"), {"n": n})).scalars().all()
        stored = {}
        for pid in ids:
            row = await session.get(PropertyValuation, pid)
            stored[pid] = ({f: getattr(row, f) for f in FIELDS},
                           set(row.data_quality_flags or []), row.computed_at)
        engine = ValuationEngine()
        engine._rent_index = await build_zip_bed_rent_index(session)
        if as_of:
            # Same index, but only from rental events ingested before the stored
            # valuations were computed: separates data drift from code drift.
            import aevorex.valuation.rent as rent_mod
            src = inspect.getsource(rent_mod.build_zip_bed_rent_index)
            sql = re.search(r'text\("""(.*?)"""\)', src, re.S).group(1)
            sql = sql.replace("WHERE ph.is_rental_event", "WHERE ph.created_at <= :as_of AND ph.is_rental_event")
            rows = (await session.execute(text(sql), {
                "min_rent": rent_mod.MIN_RENT, "max_rent": rent_mod.MAX_RENT,
                "min_obs": rent_mod.MIN_CELL_OBSERVATIONS, "as_of": as_of})).mappings().all()
            engine._rent_index = {
                (r["zip_code"], int(r["bedrooms"])): (
                    float(r["median_rent"]), int(r["n"]),
                    float(r["median_sqft"]) if r["median_sqft"] else None) for r in rows}
        diffs = []
        for pid in ids:
            prop = (await session.execute(select(Property).where(Property.id == pid))).scalars().first()
            new = await engine._value_one(session, prop)
            old_vals, old_flags, computed_at = stored[pid]
            bad = [f for f in FIELDS if not _close(old_vals[f], new.get(f))]
            new_flags = set(new.get("data_quality_flags") or [])
            if bad or new_flags != old_flags:
                comp_new = (await session.execute(text(
                    "select count(*) from property_comps where property_id=:p and created_at>:t"),
                    {"p": pid, "t": computed_at})).scalar()
                drift = []
                if prop.updated_at and prop.updated_at > computed_at:
                    drift.append("property_updated_after_valuation")
                if comp_new:
                    drift.append("comps_added_after_valuation")
                diffs.append((pid, drift or ["NO_INPUT_CHANGE"], bad, {f: (old_vals[f], new.get(f)) for f in bad},
                              sorted(old_flags ^ new_flags)))
            await session.rollback()  # discard the in-place ORM update
        await session.rollback()
    from collections import Counter
    print(f"checked={len(ids)} differing={len(diffs)}")
    print(Counter(tuple(d[1]) for d in diffs))
    for d in diffs:
        print(d)
    return 0


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    as_of = dt.datetime.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else None
    sys.exit(asyncio.run(main(n, as_of)))
