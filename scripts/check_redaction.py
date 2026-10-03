"""Run the publisher's text redaction over live local descriptions and report residue.

Prints counts only (never text). Usage: python scripts/check_redaction.py
"""
import asyncio
import re

from sqlalchemy import text

from aevorex.db.session import async_session_maker
from aevorex.publisher.redact import REMOVED, serving_text

PHONE = re.compile(r"(?<![\w$.])(?:\+?1[\s.\-]*)?(?:\(\s*\d{3}\s*\)|\d{3})[\s.\-]*\d{3}[\s.\-]*\d{4}(?!\d)")
EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+\.[\w.\-]+")
URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


async def main() -> None:
    async with async_session_maker() as session:
        rows = (await session.execute(
            text("select description, ai_summary from properties"))).all()
    stats = {"descriptions": 0, "summaries": 0, "changed": 0, "residue": 0, "over_cap": 0,
             "removed_markers": 0}
    for description, summary in rows:
        for original, limit, key in ((description, 600, "descriptions"), (summary, None, "summaries")):
            if not original:
                continue
            stats[key] += 1
            out = serving_text(original, limit) or ""
            stats["changed"] += int(out != original.strip())
            stats["removed_markers"] += out.count(REMOVED)
            stats["residue"] += int(bool(PHONE.search(out) or EMAIL.search(out) or URL.search(out)))
            stats["over_cap"] += int(limit is not None and len(out) > limit)
    print(stats)


asyncio.run(main())
