"""Active-city discovery: ``app.org_markets`` (enabled) union ``config/demo_markets.yaml``."""

from __future__ import annotations

import csv
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from aevorex.publisher.markets import CITY_DATA, demo_market_slugs, market_definition
from aevorex.scheduler.config import ScheduleConfig
from aevorex.scheduler.slots import stagger_offset_minutes

LOGGER = logging.getLogger(__name__)

# `app.orgs` in the E0 stub carries a plan but no trial/active status, so enablement is the
# only org-level switch the engine can honour today (recorded in docs/decisions.md).
ORG_MARKETS_SQL = text(
    "select distinct om.market_slug from app.org_markets om "
    "join app.orgs o on o.id = om.org_id where om.enabled order by 1"
)

OrgMarketReader = Callable[[], Awaitable[set[str]]]


@dataclass(frozen=True)
class ActiveCity:
    """One market the scheduler is responsible for."""

    slug: str
    city: str
    state: str
    timezone: str
    zips: tuple[str, ...]
    sources: tuple[str, ...]  # "demo" and/or "org"
    stagger_minutes: int


@dataclass
class Discovery:
    """Outcome of one discovery pass, with every gap stated explicitly."""

    active: dict[str, ActiveCity] = field(default_factory=dict)
    unsupported: dict[str, tuple[str, ...]] = field(default_factory=dict)
    remote_ok: bool = True


@lru_cache(maxsize=1)
def _zip_index() -> dict[tuple[str, str], tuple[str, ...]]:
    index: dict[tuple[str, str], tuple[str, ...]] = {}
    with CITY_DATA.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = (row.get("city_ascii", "").casefold(), row.get("state_id", "").upper())
            index[key] = tuple(sorted(row.get("zips", "").split()))
    return index


def city_zips(city: str, state: str) -> tuple[str, ...]:
    return _zip_index().get((city.casefold(), state.upper()), ())


def remote_org_market_reader(engine: AsyncEngine) -> OrgMarketReader:
    """Read enabled org markets from Supabase through the server-side engine."""

    async def read() -> set[str]:
        async with engine.connect() as connection:
            rows = (await connection.execute(ORG_MARKETS_SQL)).scalars().all()
        return {str(slug).strip().lower() for slug in rows if slug}

    return read


async def discover(
    config: ScheduleConfig,
    org_reader: OrgMarketReader | None,
    *,
    previous_org_slugs: set[str] | None = None,
) -> tuple[Discovery, set[str]]:
    """Resolve the active set; returns it plus the org slugs actually used.

    A transient Supabase failure must never drop paying customers' cities, so on a read
    failure the previous org slugs are kept and ``remote_ok`` is reported ``False``.
    """
    discovery = Discovery()
    org_slugs: set[str] = set()
    if org_reader is not None:
        try:
            org_slugs = {slug.strip().lower() for slug in await org_reader() if slug}
        except Exception as exc:
            discovery.remote_ok = False
            org_slugs = set(previous_org_slugs or ())
            LOGGER.warning("Org market discovery failed: error_class=%s", type(exc).__name__)
    demo = set(demo_market_slugs())

    for slug in sorted(demo | org_slugs):
        sources = tuple(
            name for name, members in (("demo", demo), ("org", org_slugs)) if slug in members
        )
        try:
            definition = market_definition(slug)
        except ValueError:
            # Not in scrapers/constants.py: we hold no Redfin region id, so we cannot check it.
            discovery.unsupported[slug] = sources
            continue
        zips = city_zips(definition.city, definition.state)
        discovery.active[definition.slug] = ActiveCity(
            slug=definition.slug,
            city=definition.city,
            state=definition.state,
            timezone=definition.timezone,
            zips=zips,
            sources=sources,
            stagger_minutes=stagger_offset_minutes(
                definition.slug, zips, config.for_market(definition.slug).stagger_minutes
            ),
        )
    for slug, sources in discovery.unsupported.items():
        LOGGER.warning(
            "Active market has no Redfin region id and cannot be scheduled: market=%s sources=%s",
            slug,
            ",".join(sources),
        )
    return discovery, org_slugs
