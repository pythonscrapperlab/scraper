"""Quality gate for demo snapshots built by publish_demo_snapshots.py (reads its JSON output).

Checks every open property in every snapshot: full address, at least three photos, at least
three reasons that contain a number, and no contact data or raw breakdown anywhere. Exits 1
and lists each failure, so it can run before ``--publish``.

    python scripts/check_demo_snapshots.py snapshots.json
"""

import json
import re
import sys
from pathlib import Path

ADDRESS = re.compile(r"^\d+[A-Za-z]?\s+\S.*, [A-Z][A-Za-z .'\-]+, [A-Z]{2} \d{5}(-\d{4})?$")
FORBIDDEN_KEYS = {"agent", "broker", "phone", "email", "breakdown", "listing_agent", "mls_id"}
FORBIDDEN_TEXT = re.compile(
    r"@|https?://(?!ssl\.cdn-redfin\.com)|\b(?:agent|broker|brokerage|phone|e-?mail)\b"
    r"|\d{3}[\s.\-)]+\d{3}[\s.\-]+\d{4}|undisclosed",
    re.IGNORECASE,
)
OPEN_KEYS = {"address", "photos", "price", "beds", "baths", "sqft", "days_listed", "property_type",
             "year_built", "rank", "pool_size", "tier", "headline", "reasons", "summary"}


def _walk(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield f"{path}.{key}", key, None
            yield from _walk(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, f"{path}[{index}]")
    else:
        yield path, None, value


def check(rows: list[dict]) -> list[str]:
    failures: list[str] = []
    for row in rows:
        tag = f"{row['market_slug']}/{row['lens']}"
        payload = row["payload"]
        if len(payload["full"]) != 3 or len(payload["stubs"]) != 5:
            failures.append(f"{tag}: expected 3 open + 5 teasers, got "
                            f"{len(payload['full'])} + {len(payload['stubs'])}")
        for path, key, leaf in _walk(payload):
            if key and key.lower() in FORBIDDEN_KEYS:
                failures.append(f"{tag}: forbidden key at {path}")
            if isinstance(leaf, str) and FORBIDDEN_TEXT.search(leaf):
                failures.append(f"{tag}: forbidden text at {path}: {leaf[:60]!r}")
        for stub in payload["stubs"]:
            if set(stub) != {"price_band", "days_listed"}:
                failures.append(f"{tag}: teaser exposes {sorted(stub)}")
        ranks = [p["rank"] for p in payload["full"]]
        if ranks != sorted(ranks):
            failures.append(f"{tag}: open properties are not in rank order {ranks}")
        for position, prop in enumerate(payload["full"], 1):
            where = f"{tag}#{position}"
            if set(prop) != OPEN_KEYS:
                failures.append(f"{where}: keys differ: {sorted(set(prop) ^ OPEN_KEYS)}")
            if not ADDRESS.match(prop["address"]):
                failures.append(f"{where}: incomplete address {prop['address']!r}")
            if len(prop["photos"]) < 3:
                failures.append(f"{where}: {len(prop['photos'])} photos")
            reasons = prop["reasons"]
            numeric = [r for r in reasons if any(ch.isdigit() for ch in r)]
            if not 3 <= len(reasons) <= 5:
                failures.append(f"{where}: {len(reasons)} reasons")
            if len(numeric) < 3:
                failures.append(f"{where}: only {len(numeric)} numeric reasons")
            if not prop["summary"]:
                failures.append(f"{where}: empty summary")
            if not prop["price"] or prop["days_listed"] is None:
                failures.append(f"{where}: missing price or days listed")
    return failures


def main() -> int:
    rows = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    opens = sum(len(r["payload"]["full"]) for r in rows)
    failures = check(rows)
    print(f"{len(rows)} snapshots, {opens} open properties checked, {len(failures)} failures")
    for failure in failures:
        print("FAIL", failure)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
