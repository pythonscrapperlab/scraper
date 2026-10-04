"""Shared fixtures: a local source of truth and a 'remote' serving cache in the test database."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import URL, delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from aevorex.db.models import (
    ChangeEvent,
    ListingPresence,
    MarketFreshness,
    Property,
    PropertyAnalysis,
    PropertyImage,
    PublishMarketState,
    PublishState,
    utc_now,
)
from aevorex.publisher.markets import market_definition
from aevorex.publisher.service import Publisher
from tests.conftest import LocalTestDatabase

SLUG = "orlando-fl"  # configured Redfin market; also a demo market, so 8+ scored rows are needed
WRITE = re.compile(
    r'^\s*(insert\s+into|update|delete\s+from)\s+((?:serving|app)\."?\w+"?)', re.IGNORECASE
)


def _url(db: LocalTestDatabase) -> URL:
    return URL.create(
        "postgresql+asyncpg", username=db.user, password=db.password,
        host=db.host, port=db.port, database=db.database,
    )


@dataclass
class Rig:
    """Local sessions, a remote engine over the same test database, and a write recorder."""

    engine: AsyncEngine
    sessions: async_sessionmaker
    writes: list[tuple[str, str]] = field(default_factory=list)
    publishers: list[Publisher] = field(default_factory=list)

    def publisher(self, **kwargs: object) -> Publisher:
        publisher = Publisher(local_sessions=self.sessions, remote_engine=self.engine, **kwargs)  # type: ignore[arg-type]

        async def no_ping(*, failed: bool) -> None:  # never touch Healthchecks from tests
            return None

        publisher._ping_healthcheck = no_ping  # type: ignore[method-assign]
        self.publishers.append(publisher)
        return publisher

    def write_tables(self) -> set[tuple[str, str]]:
        """(verb, table) of every write statement seen since the last ``clear``."""
        return {
            (verb.split()[0].lower(), table.replace('"', "").lower()) for verb, table in self.writes
        }

    def clear(self) -> None:
        self.writes.clear()


def lens_factors(lens: str, price: int) -> dict[str, object]:
    """Factors rich enough for every lens to state three numeric, favourable reasons."""
    if lens == "motivated_seller":
        return {"price_reduction_count": 2, "cumulative_price_cut_pct": 10.0,
                "original_list_price": round(price / 0.9), "days_since_last_price_cut": 5,
                "dom_vs_market_median": 3.0, "market_median_dom": 20.0}
    if lens == "fix_flip":
        return {"arv": round(price * 1.5), "max_allowable_offer": round(price * 1.1),
                "projected_profit": 50_000, "cash_invested": 60_000, "roi_pct": 40.0,
                "hold_months": 6.0, "valuation_confidence": 0.8}
    if lens == "buy_hold":
        return {"cap_rate_all_in_pct": 7.5, "all_in_basis": round(price * 1.1),
                "gross_yield_pct": 9.0, "market_median_gross_yield_pct": 6.0,
                "monthly_rent_estimate": 2500, "rent_method": "source_avm",
                "monthly_cash_flow_after_debt": 300, "dscr": 1.3}
    if lens == "str":
        return {"in_season_monthly_rent": 4000, "monthly_rent_long_term": 3000,
                "in_season_multiplier": 1.33, "season_months": 4.0, "net_annual_income": 20_000,
                "net_yield_pct": 6.0, "all_in_basis": round(price * 1.1),
                "uplift_vs_annual_lease_pct": 12.0}
    return {"proxy_nightly_rate": 300, "proxy_occupancy": 0.5, "proxy_gross_annual_revenue": 54_750,
            "proxy_net_annual_income": 20_000, "proxy_net_yield_pct": 6.0,
            "all_in_basis": round(price * 1.1), "bedrooms": 3}


def scored_property(position: int) -> tuple[Property, PropertyAnalysis]:
    score = 90.0 - position
    breakdown = {
        "components": [
            {"key": "one", "label": "First", "weight": 0.5, "subscore": score},
            {"key": "two", "label": "Second", "weight": 0.5, "subscore": score},
        ],
        "adjustments": [],
    }
    item = Property(
        id=uuid4(),
        primary_source="redfin",
        redfin_id=f"e6-{position}",
        address=f"{position + 100} publisher street",
        city="Orlando",
        state="FL",
        zip_code="32801",
        price=300_000 + position * 10_000,
        bedrooms=3,
        bathrooms=2,
        sqft=1500,
        property_type="Single Family Residential",
        year_built=1990,
        days_on_market=position + 1,
        listing_status="Active",
        listing_status_normalized="active",
        listing_url=f"https://example.invalid/home/{position}",
        description="Charming home. Call Bob at 407-555-0100 or bob@example.com today.",
        last_seen_at=datetime(2026, 10, 3),
    )
    item.property_images = [
        PropertyImage(source="redfin", url=f"https://img.example.invalid/{position}/{index}.jpg",
                      sort_order=index)
        for index in range(6)
    ]
    fields: dict[str, object] = {"property_id": item.id, "scoring_config_version": "v3",
                                 "computed_at": datetime(2026, 10, 3)}
    for lens in ("motivated_seller", "fix_flip", "buy_hold", "str", "airbnb"):
        fields.update({
            f"{lens}_score": score, f"{lens}_grade": "A", f"{lens}_percentile": 99.0 - position,
            f"{lens}_confidence": 1.0, f"{lens}_breakdown": breakdown,
            f"{lens}_factors": lens_factors(lens, item.price),
            f"{lens}_rationale": f"Grade A ({score:.0f}/100). Sale priced 10% under its original ask. "
                                 "Key risks: none.",
        })
    analysis = PropertyAnalysis(**fields)
    return item, analysis


async def seed_market(rig: Rig, count: int = 9) -> list[UUID]:
    """Nine scored, listed properties (the demo snapshot needs at least eight)."""
    ids: list[UUID] = []
    async with rig.sessions() as session:
        definition = market_definition(SLUG)
        session.add(
            MarketFreshness(
                slug=SLUG, city=definition.city, state=definition.state,
                region_id=definition.region_id, check_status="ok", listings_active=count,
                changed_last_check=0, last_checked_at=utc_now(), next_check_at=utc_now(),
            )
        )
        await session.flush()
        for position in range(count):
            item, analysis = scored_property(position)
            session.add(item)
            await session.flush()
            session.add(analysis)
            session.add(
                ListingPresence(
                    market_slug=SLUG, redfin_id=item.redfin_id, property_id=item.id,
                    listing_url=item.listing_url, price=item.price, absence_count=0,
                    is_delisted=False, last_seen_at=utc_now(),
                )
            )
            ids.append(item.id)
        await session.commit()
    return ids


async def reset(rig: Rig) -> None:
    """Forget everything a previous test left behind (local truth, local state, remote cache)."""
    async with rig.sessions() as session:
        await session.execute(delete(PublishState).where(PublishState.market_slug == SLUG))
        await session.execute(
            delete(PublishMarketState).where(PublishMarketState.market_slug == SLUG)
        )
        await session.execute(delete(ChangeEvent).where(ChangeEvent.market_slug == SLUG))
        await session.execute(delete(ListingPresence).where(ListingPresence.market_slug == SLUG))
        for table in ("property_analysis", "price_history", "property_comps", "property_images",
                      "tax_history", "property_features"):
            await session.execute(text(
                f"delete from {table} where property_id in "
                "(select id from properties where redfin_id like 'e6-%')"))
        await session.execute(delete(Property).where(Property.redfin_id.like("e6-%")))
        await session.execute(delete(MarketFreshness).where(MarketFreshness.slug == SLUG))
        # The test database's serving schema belongs to the tests: clear every market's cascade.
        await session.execute(text("delete from serving.markets"))
        await session.execute(text("delete from serving.heartbeats"))
        await session.execute(text("delete from serving.runs"))
        await session.execute(text("delete from serving.market_daily"))
        await session.commit()


@pytest_asyncio.fixture
async def rig(aevorex_test_db: LocalTestDatabase) -> AsyncIterator[Rig]:
    engine = create_async_engine(_url(aevorex_test_db), connect_args={"statement_cache_size": 0})
    writes: list[tuple[str, str]] = []
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        match = WRITE.match(statement)
        if match:  # only serving.* / app.* statements: the cache, not local bookkeeping
            writes.append((match.group(1), match.group(2)))

    rig_ = Rig(engine=engine, sessions=async_sessionmaker(engine, expire_on_commit=False), writes=writes)
    await reset(rig_)
    writes.clear()
    yield rig_
    await reset(rig_)
    await engine.dispose()


pytestmark = pytest.mark.database
