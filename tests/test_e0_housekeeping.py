"""E0 schema, credential, and run-ledger acceptance tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import pytest
from click.testing import CliRunner
from pydantic import ValidationError
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aevorex import run_tracking
from aevorex.config import Settings
from aevorex.run_tracking import JsonValue, numeric_counts
from tests.conftest import LocalTestDatabase


@pytest.mark.parametrize("missing", ["db_password", "proxy_user", "proxy_password"])
def test_required_credentials_fail_fast(missing: str) -> None:
    values = {
        "db_password": "test-only",
        "proxy_user": "test-only",
        "proxy_password": "test-only",
    }
    del values[missing]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_run_counts_drop_strings_and_lists() -> None:
    result = numeric_counts(
        {
            "success": 4,
            "failed": 1,
            "errors": ["https://example.invalid/address"],
            "message": "private text",
            "stages": {"scored": 3, "detail": "private text"},
        }
    )
    assert result == {"success": 4, "failed": 1, "stages": {"scored": 3}}


@pytest.mark.parametrize(
    ("arguments", "expected_kind"),
    [
        (["scrape", "--source", "redfin", "--state", "FL"], "scrape"),
        (["retry", "--source", "redfin", "--state", "FL"], "retry"),
        (["scheduler"], "scheduler"),
        (["market-stats"], "market-stats"),
        (["value"], "value"),
        (["score"], "score"),
        (["analyze"], "analyze"),
        (["init-db"], "init-db"),
    ],
)
def test_every_cli_command_routes_through_run_tracking(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    expected_kind: str,
) -> None:
    from alembic import command

    import main

    seen: list[str] = []

    def fake_run_command(
        kind: str,
        scope: Mapping[str, JsonValue],
        operation: Callable[[], Awaitable[Any]],
    ) -> None:
        del scope, operation
        seen.append(kind)

    monkeypatch.setattr(main, "_run_command", fake_run_command)
    monkeypatch.setattr(command, "upgrade", lambda config, revision: None)

    result = CliRunner().invoke(main.cli, arguments)
    assert result.exit_code == 0, result.output
    assert seen == [expected_kind]


@pytest.mark.database
def test_local_and_supabase_migrations_apply_cleanly(
    aevorex_test_db: LocalTestDatabase,
) -> None:
    async def inspect() -> None:
        connection = await aevorex_test_db.connect()
        try:
            local_columns = await connection.fetch(
                """
                select table_name, column_name
                from information_schema.columns
                where table_schema = 'public'
                  and (
                    table_name = 'runs'
                    or (table_name = 'property_analysis' and column_name like '%_breakdown')
                  )
                """
            )
            columns = {(row["table_name"], row["column_name"]) for row in local_columns}
            assert ("runs", "counts") in columns
            assert {
                ("property_analysis", f"{lens}_breakdown")
                for lens in ("motivated_seller", "fix_flip", "buy_hold", "str", "airbnb")
            }.issubset(columns)

            tables = await connection.fetchval(
                """
                select count(*) from information_schema.tables
                where (table_schema = 'app' and table_name in (
                    'orgs', 'org_members', 'org_markets', 'thresholds',
                    'notification_settings', 'alert_events', 'email_queue'
                )) or (table_schema = 'serving' and table_name in (
                    'markets', 'runs', 'properties', 'scores', 'valuation',
                    'agents', 'images', 'comps', 'history', 'features',
                    'neighbourhood', 'tax_history', 'change_events', 'market_daily',
                    'demo_snapshots', 'heartbeats', 'schema_version'
                ))
                """
            )
            assert tables == 24

            serving_columns = await connection.fetch(
                """
                select table_name, column_name
                from information_schema.columns
                where table_schema = 'serving'
                """
            )
            contract_columns = {
                (row["table_name"], row["column_name"]) for row in serving_columns
            }
            assert {
                ("markets", "region_id"),
                ("markets", "tz"),
                ("markets", "pool_size"),
                ("runs", "market_id"),
                ("runs", "trigger"),
                ("properties", "listing_status_normalized"),
                ("scores", "version"),
                ("valuation", "rehab_mid"),
                ("agents", "listing_agent_phone"),
                ("change_events", "observed_at"),
            }.issubset(contract_columns)

            rls_without_policies = await connection.fetchval(
                """
                select count(*)
                from pg_class c
                join pg_namespace n on n.oid = c.relnamespace
                where n.nspname in ('app', 'serving')
                  and c.relkind = 'r'
                  and not c.relrowsecurity
                """
            )
            assert rls_without_policies == 0

            policies = await connection.fetchval(
                "select count(*) from pg_policies where schemaname in ('app', 'serving')"
            )
            assert policies >= 23

            anon_market_columns = await connection.fetch(
                """
                select column_name
                from information_schema.column_privileges
                where table_schema = 'serving' and table_name = 'markets'
                  and grantee = 'anon' and privilege_type = 'SELECT'
                """
            )
            assert {row["column_name"] for row in anon_market_columns} == {
                "slug", "city", "state", "tz", "active", "is_demo",
                "last_checked_at", "next_check_at", "last_refreshed_at",
                "check_status", "listings_active", "changed_last_check",
            }
        finally:
            await connection.close()

    asyncio.run(inspect())


@pytest.mark.database
def test_run_lifecycle_persists_sanitized_terminal_rows(
    aevorex_test_db: LocalTestDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        engine = create_async_engine(
            URL.create(
                "postgresql+asyncpg",
                username=aevorex_test_db.user,
                password=aevorex_test_db.password,
                host=aevorex_test_db.host,
                port=aevorex_test_db.port,
                database=aevorex_test_db.database,
            )
        )
        maker = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(run_tracking, "async_session_maker", maker)

        async def operation() -> dict[str, object]:
            return {"success": 2, "detail": "must not persist"}

        await run_tracking.tracked(
            "test-command", {"source": "redfin", "states": ["FL"]}, operation
        )
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "select status, counts, error_class, duration_s "
                        "from runs where kind = 'test-command' order by started_at desc limit 1"
                    )
                )
            ).mappings().one()
        await engine.dispose()

        assert row["status"] == "succeeded"
        assert row["counts"] == {"success": 2}
        assert row["error_class"] is None
        assert row["duration_s"] >= 0

    asyncio.run(exercise())
