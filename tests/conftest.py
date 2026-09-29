"""Shared test fixtures, including the isolated local PostgreSQL database."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg
import pytest

from aevorex.config import settings

TEST_DATABASE = "aevorex_test"
ROOT = Path(__file__).resolve().parents[1]
SUPABASE_MIGRATIONS = ROOT / "supabase" / "migrations"


@dataclass(frozen=True)
class LocalTestDatabase:
    """Connection parameters for the disposable, explicitly named test DB."""

    host: str
    port: int
    user: str
    password: str
    database: str = TEST_DATABASE

    async def connect(self) -> asyncpg.Connection:
        return await asyncpg.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=self.database,
        )


@pytest.fixture(scope="session")
def aevorex_test_db() -> LocalTestDatabase:
    """Rebuild only ``aevorex_test`` and apply both migration families."""
    if TEST_DATABASE != "aevorex_test":
        raise RuntimeError("Refusing to rebuild a database not named aevorex_test")

    database = LocalTestDatabase(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password.get_secret_value(),
    )
    asyncio.run(_recreate_database(database))
    _apply_alembic(database)
    asyncio.run(_apply_supabase_migrations(database))
    return database


async def _recreate_database(database: LocalTestDatabase) -> None:
    connection = await asyncpg.connect(
        host=database.host,
        port=database.port,
        user=database.user,
        password=database.password,
        database="postgres",
    )
    try:
        await connection.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity "
            "where datname = $1 and pid <> pg_backend_pid()",
            TEST_DATABASE,
        )
        await connection.execute(f'drop database if exists "{TEST_DATABASE}"')
        await connection.execute(f'create database "{TEST_DATABASE}"')
    finally:
        await connection.close()


def _apply_alembic(database: LocalTestDatabase) -> None:
    environment = os.environ.copy()
    environment["DB_NAME"] = database.database
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


async def _apply_supabase_migrations(database: LocalTestDatabase) -> None:
    connection = await database.connect()
    try:
        await connection.execute(_supabase_test_bootstrap())
        for migration in sorted(SUPABASE_MIGRATIONS.glob("*.sql")):
            await connection.execute(migration.read_text(encoding="utf-8"))
    finally:
        await connection.close()


def _supabase_test_bootstrap() -> str:
    """Provide only the Supabase auth roles/functions needed to compile RLS."""
    return """
    do $$ begin
      if not exists (select 1 from pg_roles where rolname = 'anon') then
        create role anon nologin;
      end if;
      if not exists (select 1 from pg_roles where rolname = 'authenticated') then
        create role authenticated nologin;
      end if;
      if not exists (select 1 from pg_roles where rolname = 'service_role') then
        create role service_role nologin bypassrls;
      end if;
    end $$;
    create schema if not exists auth;
    create table if not exists auth.users (id uuid primary key);
    create or replace function auth.uid() returns uuid
      language sql stable
      as $$ select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid $$;
    grant usage on schema auth to authenticated;
    grant execute on function auth.uid() to authenticated;
    """
