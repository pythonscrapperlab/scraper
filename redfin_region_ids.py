"""
Build a Redfin city -> region_id mapping for any US state.

How it works:
  1. Take a seed list of city names for the state (Census / SimpleMaps / your own).
  2. Hit Redfin's location-autocomplete endpoint for each "City, ST".
  3. Pull /city/<region_id>/<ST>/<Slug> out of the JSON response.
  4. Keep only rows whose state matches and whose slug matches the seed name.

Notes:
  - Redfin prefixes its JSON with "{}&&" as a JSON-hijacking guard. Strip it.
  - The endpoint is geo-fenced to US IPs and rate-limits by IP. Use a US proxy.
  - Response schema drifts. This walks the whole JSON tree looking for city URLs
    instead of depending on payload.sections[i].rows[j].url, so a schema change
    won't silently break it.

Usage:
    python redfin_region_ids.py CA --seed uscities.csv --min-pop 25000
"""

import argparse
import csv
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

AUTOCOMPLETE = "https://www.redfin.com/stingray/do/location-autocomplete"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.redfin.com/",
}

# /city/13655/FL/Orlando  ->  ("13655", "FL", "Orlando")
CITY_URL = re.compile(r"^/city/(\d+)/([A-Z]{2})/([^/?#]+)")

PROXIES = {
    "http": "http://zoroupwork-US-rotate:burhanburhan@p.webshare.io:80",
    "https": "http://zoroupwork-US-rotate:burhanburhan@p.webshare.io:80",
}  # e.g. {"https": "http://user:pass@us-residential-proxy:port"}


# ---------------------------------------------------------------- helpers

def normalize(name: str) -> str:
    """Fold 'St. Petersburg', 'St-Petersburg', 'st petersburg' to one key."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def strip_json_guard(text: str) -> dict:
    if text.startswith("{}&&"):
        text = text[4:]
    return json.loads(text)


def walk_city_urls(node):
    """Yield (region_id, state, slug) for every city URL anywhere in the JSON."""
    if isinstance(node, dict):
        for value in node.values():
            yield from walk_city_urls(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk_city_urls(value)
    elif isinstance(node, str):
        match = CITY_URL.match(node)
        if match:
            yield match.groups()


# ---------------------------------------------------------------- fetching

def lookup_city(session: requests.Session, city: str, state: str,
                retries: int = 4):
    """Return (slug, region_id) for one city, or None if Redfin has no match."""
    params = {
        "location": f"{city}, {state}",
        "start": 0,
        "count": 10,
        "v": 2,
        "market": "",
        "al": 1,
        "iss": "false",
        "ooa": "true",
        "mrs": "false",
    }

    for attempt in range(retries):
        try:
            response = session.get(
                AUTOCOMPLETE,
                params=params,
                headers=HEADERS,
                proxies=PROXIES,
                timeout=20,
            )
            if response.status_code in (403, 429, 502, 503):
                raise requests.HTTPError(f"status {response.status_code}")
            response.raise_for_status()
            payload = strip_json_guard(response.text)
        except Exception as exc:  # noqa: BLE001
            if attempt == retries - 1:
                print(f"  fail {city}: {exc}", file=sys.stderr)
                return None
            time.sleep((2 ** attempt) + random.random())
            continue

        target = normalize(city)
        fallback = None

        for region_id, result_state, slug in walk_city_urls(payload):
            if result_state != state:
                continue
            if normalize(slug) == target:
                return slug, int(region_id)
            if fallback is None:
                fallback = (slug, int(region_id))

        # Redfin sometimes returns a differently-worded exact city
        # ("Winston-Salem" for "Winston Salem"). Take the top in-state hit.
        return fallback

    return None


def build_mapping(cities, state, workers=4, delay=0.4):
    """cities: iterable of city-name strings. Returns {slug: region_id}."""
    mapping = {}
    session = requests.Session()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for city in cities:
            futures[pool.submit(lookup_city, session, city, state)] = city
            time.sleep(delay)  # crude throttle on submission, not completion

        for future in as_completed(futures):
            city = futures[future]
            result = future.result()
            if result:
                slug, region_id = result
                mapping[slug] = region_id
                print(f"  {city:<28} -> {slug} ({region_id})")

    return dict(sorted(mapping.items()))


# ---------------------------------------------------------------- seed list

def load_seed_cities(path, state, min_pop=0):
    """
    Read a SimpleMaps-style uscities.csv (columns: city, state_id, population).
    Swap this out for the Census Gazetteer place file or your own list.
    """
    cities = []
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("state_id") != state:
                continue
            try:
                population = float(row.get("population") or 0)
            except ValueError:
                population = 0
            if population >= min_pop:
                cities.append((population, row["city"]))

    cities.sort(reverse=True)
    return [name for _, name in cities]


# ---------------------------------------------------------------- entry

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("state", help="two-letter state code, e.g. CA")
    parser.add_argument("--seed", required=True, help="path to uscities.csv")
    parser.add_argument("--min-pop", type=int, default=25000)
    parser.add_argument("--limit", type=int, default=None,
                        help="cap number of cities (largest first)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", default=None, help="write JSON here")
    args = parser.parse_args()

    state = args.state.upper()
    cities = load_seed_cities(args.seed, state, args.min_pop)
    if args.limit:
        cities = cities[: args.limit]

    print(f"Resolving {len(cities)} cities in {state}...", file=sys.stderr)
    mapping = build_mapping(cities, state, workers=args.workers)

    print(f"\nResolved {len(mapping)}/{len(cities)}", file=sys.stderr)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump({state: mapping}, handle, indent=4)

    # Print in the same shape as your existing REDFIN dict
    print("REDFIN = {")
    print(f'    "{state}": {{')
    for slug, region_id in mapping.items():
        print(f'        "{slug}": {region_id},')
    print("    }")
    print("}")


if __name__ == "__main__":
    main()