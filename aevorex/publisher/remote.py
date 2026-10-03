"""Strictly server-side PostgreSQL access to the Supabase serving cache."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import ARRAY, MetaData, Table, bindparam, delete, func, inspect, select, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from aevorex.config import settings
from aevorex.publisher.types import SCHEMA_VERSION

JsonRow = Mapping[str, Any]

# Append-only serving tables are capped remotely so a long soak stays flat.
PRUNE_MARKET_DAILY_DAYS = 90
PRUNE_RUNS_DAYS = 30
PRUNE_HEARTBEATS_DAYS = 7

EXPECTED_COLUMNS: dict[str, set[str]] = {
    "markets": set("id city state region_id slug tz zips active is_demo check_cadence_minutes last_checked_at next_check_at last_refreshed_at check_status listings_active changed_last_check pool_size source_status updated_at".split()),
    "runs": set("id market_id kind trigger started_at finished_at status counts error_class duration_s".split()),
    "properties": set("id market_id redfin_id apn address unit city state zip lat lng county property_type beds baths sqft lot_sqft year_built year_renovated stories hoa_monthly price price_is_placeholder price_per_sqft dom dom_mls listing_status listing_status_normalized listed_at listing_url first_seen_at last_seen_at delisted_at refreshed_at flags climate mobility description ai_summary photo_count updated_at".split()),
    "scores": set("property_id lens score grade percentile confidence tier prev_score prev_percentile rationale flags breakdown version computed_at".split()),
    "valuation": set("property_id market_value market_value_method arv arv_method price_to_value_ratio comp_count comp_median_ppsf comp_p75_ppsf rehab_low rehab_mid rehab_high condition_class rent_estimate_monthly rent_method gross_yield annual_taxes annual_insurance annual_hoa annual_operating_expenses noi_annual cap_rate max_allowable_offer valuation_confidence flags version computed_at".split()),
    "agents": set("property_id listing_agent listing_agent_phone listing_broker mls_id source".split()),
    "images": set("property_id sort_order url".split()),
    "comps": set("id property_id address price beds baths sqft sold_date".split()),
    "history": set("id property_id event_type event price event_date event_source is_rental".split()),
    "features": {"property_id", "features"},
    "neighbourhood": {"property_id", "schools", "location_scores", "transport_count"},
    "tax_history": {"property_id", "tax_year", "tax_amount", "assessed_value"},
    "change_events": {"id", "property_id", "market_id", "kind", "detail", "observed_at"},
    "market_daily": {"market_id", "day", "listings_active", "tier_counts"},
    "demo_snapshots": {"market_slug", "lens", "payload", "generated_at"},
    "heartbeats": {"id", "at", "note"},
    "schema_version": {"version"},
}


def _json_default(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Unsupported publisher value type: {type(value).__name__}")


def publisher_engine() -> AsyncEngine:
    """Build the remote async engine without rendering its credential."""
    secret = settings.supabase_direct_connection_url
    if secret is None:
        raise RuntimeError("SUPABASE_DIRECT_CONNECTION_URL is required")
    parsed = make_url(secret.get_secret_value())
    url: URL = parsed.set(drivername="postgresql+asyncpg")
    return create_async_engine(
        url,
        pool_size=2,
        max_overflow=0,
        pool_pre_ping=True,
        connect_args={"statement_cache_size": 0, "timeout": settings.db_timeout},
    )


class RemoteStore:
    """Reflected serving-v2 tables plus chunked PostgreSQL upserts."""

    def __init__(self, engine: AsyncEngine, *, batch_size: int = 250) -> None:
        self.engine = engine
        self.batch_size = batch_size
        self.metadata = MetaData()
        self.tables: dict[str, Table] = {}

    async def initialize(self) -> None:
        """Reflect only the serving schema and verify the expected schema version."""
        async with self.engine.connect() as connection:
            await connection.run_sync(
                lambda sync: self.metadata.reflect(bind=sync, schema="serving")
            )
            self.tables = {
                key.split(".", 1)[1]: table
                for key, table in self.metadata.tables.items()
                if key.startswith("serving.")
            }
            required = EXPECTED_COLUMNS.keys()
            if required - self.tables.keys():
                raise RuntimeError("ServingSchemaDrift")
            version = await connection.scalar(select(self.table("schema_version").c.version))
            if version != SCHEMA_VERSION:
                raise RuntimeError("ServingSchemaVersionMismatch")

    def table(self, name: str) -> Table:
        try:
            return self.tables[name]
        except KeyError as exc:
            raise RuntimeError("Remote store is not initialized") from exc

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncConnection]:
        async with self.engine.begin() as connection:
            yield connection

    async def upsert(
        self,
        connection: AsyncConnection,
        table_name: str,
        rows: Sequence[JsonRow],
        *,
        conflict: Sequence[str],
        preserve_previous_scores: bool = False,
        do_nothing: bool = False,
    ) -> int:
        """Upsert rows in bounded multi-value statements."""
        if not rows:
            return 0
        table = self.table(table_name)
        total = 0
        columns = list(rows[0].keys())
        if any(set(row.keys()) != set(columns) for row in rows):
            raise ValueError("All rows in one publisher batch must have identical columns")
        if set(columns) - {column.name for column in table.columns}:
            raise ValueError("Publisher row contains an unknown serving column")
        quoted_columns = ",".join(f'"{name}"' for name in columns)
        conflict_sql = ",".join(f'"{name}"' for name in conflict)
        preserved = {"prev_score", "prev_percentile"} if preserve_previous_scores else set()
        updates = [name for name in columns if name not in conflict and name not in preserved]
        update_sql = ",".join(
            f'"{name}"=excluded."{name}"' for name in updates
        )
        if preserve_previous_scores:
            update_sql = ",".join(
                part
                for part in (
                    update_sql,
                    '"prev_score"=case when target."score" is distinct from excluded."score" '
                    'then target."score" else target."prev_score" end',
                    '"prev_percentile"=case when target."percentile" is distinct from '
                    'excluded."percentile" then target."percentile" '
                    'else target."prev_percentile" end',
                )
                if part
            )
        action_sql = (
            f"do update set {update_sql}" if update_sql and not do_nothing else "do nothing"
        )
        statement = text(
            f'insert into serving."{table_name}" as target ({quoted_columns}) '
            f'select {quoted_columns} from jsonb_populate_recordset('
            f'null::serving."{table_name}", cast(:payload as jsonb)) '
            f'on conflict ({conflict_sql}) {action_sql}'
        )
        effective_batch = self.batch_size
        for offset in range(0, len(rows), effective_batch):
            chunk = [dict(row) for row in rows[offset : offset + effective_batch]]
            outcome = await connection.execute(
                statement,
                {"payload": json.dumps(chunk, default=_json_default, separators=(",", ":"))},
            )
            # DO NOTHING reports only the rows really inserted; repeats must count as zero.
            total += int(outcome.rowcount or 0) if do_nothing else len(chunk)
        return total

    async def database_size(self, connection: AsyncConnection) -> int:
        value = await connection.scalar(text("select pg_database_size(current_database())"))
        return int(value or 0)

    async def market_exists(self, connection: AsyncConnection, slug: str) -> bool:
        table = self.table("markets")
        return bool(await connection.scalar(select(func.count()).where(table.c.slug == slug)))

    async def table_counts(self, connection: AsyncConnection) -> dict[str, int]:
        result: dict[str, int] = {}
        for name in sorted(self.tables):
            if name == "schema_version":
                continue
            result[name] = int(await connection.scalar(select(func.count()).select_from(self.table(name))) or 0)
        return result

    async def schema_columns(self, connection: AsyncConnection) -> dict[str, set[str]]:
        def columns(sync: Any) -> dict[str, set[str]]:
            inspector = inspect(sync)
            return {
                name: {str(item["name"]) for item in inspector.get_columns(name, schema="serving")}
                for name in inspector.get_table_names(schema="serving")
            }

        return await connection.run_sync(columns)

    async def property_ids(self, connection: AsyncConnection, market_id: Any) -> set[UUID]:
        """Ids of the properties the cache holds for one market (a read, not a write)."""
        table = self.table("properties")
        rows = await connection.execute(select(table.c.id).where(table.c.market_id == market_id))
        return {row[0] for row in rows}

    async def delete_by_ids(
        self, connection: AsyncConnection, table_name: str, column: str, ids: Sequence[Any]
    ) -> int:
        """Delete rows whose ``column`` is in ``ids``; issues no statement for an empty set."""
        if not ids:
            return 0
        table = self.table(table_name)
        total = 0
        for offset in range(0, len(ids), 1000):
            result = await connection.execute(
                delete(table).where(table.c[column].in_(list(ids[offset : offset + 1000])))
            )
            total += int(result.rowcount or 0)
        return total

    async def delete_stale_scores(
        self,
        connection: AsyncConnection,
        property_ids: Sequence[Any],
        keep: Sequence[tuple[Any, str]],
    ) -> int:
        """For these properties only, drop lens rows that are no longer published."""
        if not property_ids:
            return 0
        payload = json.dumps(
            [{"property_id": pid, "lens": lens} for pid, lens in keep],
            default=_json_default,
            separators=(",", ":"),
        )
        result = await connection.execute(
            text(
                "delete from serving.scores as score where score.property_id = any(:ids) "
                "and not exists (select 1 from jsonb_to_recordset(cast(:payload as jsonb)) "
                "as kept(property_id uuid,lens text) where kept.property_id=score.property_id "
                "and kept.lens=score.lens)"
            ).bindparams(bindparam("ids", type_=ARRAY(PGUUID))),
            {"ids": list(property_ids), "payload": payload},
        )
        return int(result.rowcount or 0)

    async def prune_old(self, connection: AsyncConnection, now: datetime) -> dict[str, int]:
        """Cap the append-only tables; checks first so a clean table costs no write."""
        limits = (
            ("market_daily", "day", PRUNE_MARKET_DAILY_DAYS),
            ("runs", "started_at", PRUNE_RUNS_DAYS),
            ("heartbeats", "at", PRUNE_HEARTBEATS_DAYS),
        )
        removed: dict[str, int] = {}
        for name, column, days in limits:
            table = self.table(name)
            moment = now - timedelta(days=days)
            cutoff: datetime | date = moment.date() if name == "market_daily" else moment
            stale = await connection.scalar(
                select(func.count()).select_from(table).where(table.c[column] < cutoff)
            )
            if not stale:
                continue
            result = await connection.execute(delete(table).where(table.c[column] < cutoff))
            removed[name] = int(result.rowcount or 0)
        return removed

    async def delete_market_properties(
        self, connection: AsyncConnection, market_id: Any, keep_ids: Sequence[Any]
    ) -> int:
        table = self.table("properties")
        statement = delete(table).where(table.c.market_id == market_id)
        if keep_ids:
            statement = statement.where(table.c.id.not_in(keep_ids))
        result = await connection.execute(statement)
        return int(result.rowcount or 0)

    async def delete_market_scores_not_in(
        self,
        connection: AsyncConnection,
        market_id: Any,
        keep: Sequence[tuple[Any, str]],
    ) -> int:
        """Remove stale/null/rejected lens rows while preserving valid previous values."""
        payload = json.dumps(
            [{"property_id": property_id, "lens": lens} for property_id, lens in keep],
            default=_json_default,
            separators=(",", ":"),
        )
        result = await connection.execute(
            text(
                "delete from serving.scores as score using serving.properties as property "
                "where score.property_id=property.id and property.market_id=:market_id "
                "and not exists (select 1 from jsonb_to_recordset(cast(:payload as jsonb)) "
                "as kept(property_id uuid,lens text) where kept.property_id=score.property_id "
                "and kept.lens=score.lens)"
            ),
            {"market_id": market_id, "payload": payload},
        )
        return int(result.rowcount or 0)

    async def close(self) -> None:
        await self.engine.dispose()
