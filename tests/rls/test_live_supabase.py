"""RLS proof against the real Supabase project (opt-in; never part of the default run).

``AEVORAEX_LIVE_TESTS=1`` plus ``SUPABASE_DIRECT_CONNECTION_URL`` runs the role-switching proof
inside one transaction that is always rolled back, so the deployed policies are proven without
leaving a row behind. If ``SUPABASE_SERVICE_ROLE_KEY`` and ``SUPABASE_PROJECT_URL`` are also set,
the Starter and Pro users are created through the Supabase Auth admin API (confirmed, so no
e-mail is ever sent), signed in with a password grant to prove they can authenticate, and
deleted afterwards; their real ``auth.users`` ids drive the policy checks.
"""

from __future__ import annotations

import asyncio
import os
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
from sqlalchemy.engine import make_url

from tests.alerts.seed import World, seed_world
from tests.rls.test_rls_proof import as_role, denied

LIVE = os.environ.get("AEVORAEX_LIVE_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not LIVE, reason="set AEVORAEX_LIVE_TESTS=1 to run against the live Supabase project"
)


def _env(name: str) -> str | None:
    """Process environment first, then .env (these keys are not all engine settings)."""
    from dotenv import dotenv_values

    return os.environ.get(name) or dotenv_values(".env").get(name) or None


async def _connect() -> asyncpg.Connection:
    url = make_url(_env("SUPABASE_DIRECT_CONNECTION_URL") or "")
    return await asyncpg.connect(
        host=url.host, port=url.port, user=url.username, password=url.password,
        database=url.database, statement_cache_size=0,
    )


class AdminApi:
    """Minimal Auth admin client for two throwaway users."""

    def __init__(self, base: str, service_key: str, anon_key: str) -> None:
        self.base, self.service_key, self.anon_key = base.rstrip("/"), service_key, anon_key
        self.created: list[UUID] = []

    async def create(self, client: httpx.AsyncClient, label: str) -> tuple[UUID, str, str]:
        email = f"rls-{label}-{uuid4().hex[:10]}@example.invalid"
        password = uuid4().hex + "Aa1!"
        response = await client.post(
            f"{self.base}/auth/v1/admin/users",
            headers={"apikey": self.service_key, "Authorization": f"Bearer {self.service_key}"},
            json={"email": email, "password": password, "email_confirm": True},
        )
        response.raise_for_status()
        user = UUID(response.json()["id"])
        self.created.append(user)
        grant = await client.post(
            f"{self.base}/auth/v1/token?grant_type=password",
            headers={"apikey": self.anon_key},
            json={"email": email, "password": password},
        )
        grant.raise_for_status()
        assert grant.json()["access_token"]
        return user, email, password

    async def cleanup(self, client: httpx.AsyncClient) -> None:
        for user in self.created:
            await client.delete(
                f"{self.base}/auth/v1/admin/users/{user}",
                headers={"apikey": self.service_key, "Authorization": f"Bearer {self.service_key}"},
            )


async def _prove(connection: asyncpg.Connection, world: World) -> None:
    async with as_role(connection, "anon") as c:
        assert await denied(c, "select * from serving.markets")
        assert await denied(c, "select 1 from serving.properties")
        assert await c.fetchval(
            "select count(*) from serving.v_market_public where slug = any($1)",
            [world.orlando, world.tampa],
        ) == 2
    async with as_role(connection, "authenticated", world.starter_user) as c:
        assert await c.fetchval(
            "select count(*) from serving.properties where market_id = $1", world.orlando_id
        ) == 2
        assert await c.fetchval("select count(*) from serving.v_agent") == 0
        assert await c.fetchval(
            "select count(*) from serving.properties where market_id = $1", world.tampa_id
        ) == 0
    async with as_role(connection, "authenticated", world.pro_user) as c:
        assert await c.fetchval(
            "select count(*) from serving.v_agent where market_slug = $1", world.tampa
        ) == 2
        assert await c.fetchval(
            "select count(*) from serving.properties where market_id = $1", world.orlando_id
        ) == 0
        assert await denied(c, "insert into serving.heartbeats (at, note) values (now(), 'x')")


def test_live_policies_with_rolled_back_fixtures() -> None:
    async def go() -> None:
        connection = await _connect()
        transaction = connection.transaction()
        await transaction.start()
        try:
            world = await seed_world(connection)
            await _prove(connection, world)
        finally:
            await transaction.rollback()
            await connection.close()

    asyncio.run(go())


@pytest.mark.skipif(
    not (_env("SUPABASE_SERVICE_ROLE_KEY") and _env("SUPABASE_PROJECT_URL")
         and _env("SUPABASE_PUBLISHABLE_KEY")),
    reason="SUPABASE_SERVICE_ROLE_KEY not configured: admin-API variant not run",
)
def test_live_policies_with_admin_api_users() -> None:
    async def go() -> None:
        api = AdminApi(
            _env("SUPABASE_PROJECT_URL") or "", _env("SUPABASE_SERVICE_ROLE_KEY") or "",
            _env("SUPABASE_PUBLISHABLE_KEY") or "",
        )
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                starter, _, _ = await api.create(client, "starter")
                pro, _, _ = await api.create(client, "pro")
                connection = await _connect()
                transaction = connection.transaction()
                await transaction.start()
                try:
                    world = await seed_world(connection)
                    # Re-point the fixture orgs at the real auth.users ids.
                    await connection.execute(
                        "update app.org_members set user_id = $1 where user_id = $2",
                        starter, world.starter_user,
                    )
                    await connection.execute(
                        "update app.org_members set user_id = $1 where user_id = $2",
                        pro, world.pro_user,
                    )
                    from dataclasses import replace

                    await _prove(connection, replace(world, starter_user=starter, pro_user=pro))
                finally:
                    await transaction.rollback()
                    await connection.close()
            finally:
                await api.cleanup(client)

    asyncio.run(go())
