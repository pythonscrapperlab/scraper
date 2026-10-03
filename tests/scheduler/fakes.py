"""In-memory doubles: operations, lane locks and Healthchecks."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

from aevorex.scheduler import locks
from aevorex.scheduler.cities import ActiveCity
from aevorex.scheduler.observability import Healthchecks

ORLANDO = ActiveCity(
    "orlando-fl", "Orlando", "FL", "America/New_York", ("32801",), ("demo",), 14
)
MIAMI = ActiveCity("miami-fl", "Miami", "FL", "America/New_York", ("33101",), ("demo",), 25)
SAN_JOSE = ActiveCity(
    "san-jose-ca", "San Jose", "CA", "America/Los_Angeles", ("95101",), ("demo",), 11
)


class Clock:
    def __init__(self, now: datetime | None = None) -> None:
        self.now = now or datetime(2026, 10, 3, 15, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class FakeLocks:
    """Lane locks with the real wait/no-wait semantics and overlap accounting."""

    def __init__(self) -> None:
        self.lanes: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.held: Counter[str] = Counter()
        self.max_held: Counter[str] = Counter()

    @asynccontextmanager
    async def __call__(self, name: str, *, wait: bool = True, **_: Any) -> AsyncIterator[None]:
        lane = self.lanes[name]
        if not wait and lane.locked():
            raise locks.LockBusy(name)
        async with lane:
            self.held[name] += 1
            self.max_held[name] = max(self.max_held[name], self.held[name])
            try:
                yield
            finally:
                self.held[name] -= 1


class FakeHealthchecks(Healthchecks):
    def __init__(self) -> None:
        super().__init__()
        self.pings: list[tuple[str, str | None]] = []

    async def ping(
        self, base: SecretStr | None, *, status: str = "", run_id: Any = None, body: str | None = None
    ) -> bool:
        if base is None:
            return False
        self.pings.append((status, body))
        return True


class FakeOps:
    """Records every call; any method can be made to fail or take time."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.results: dict[str, Mapping[str, Any]] = {
            "check": {"listings": 10, "events": 0, "changed": 0, "queued": 0, "pages": 1},
            "refresh_pending": {"queued": 3, "success": 3, "failed": 0, "block_rate": 0.0},
            "refresh_city": {"queued": 10, "success": 10, "failed": 0, "block_rate": 0.0},
            "market_stats": {"cells_written": 5},
            "analyze": {"changed": 3},
            "publish_full": {"properties": 10, "partial": False},
        }
        self.errors: dict[str, Callable[[], Exception]] = {}
        self.delay: dict[str, float] = {}
        self.active: Counter[str] = Counter()
        self.max_active: Counter[str] = Counter()
        self.freshness_state: dict[str, tuple[datetime | None, str | None]] = {}
        self.stats_at: datetime | None = None
        self.light_pushed = True
        self.recovered = 0
        self.due: dict[str, int] = {}

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    async def _run(self, name: str, *args: Any) -> Mapping[str, Any]:
        self.calls.append((name, args))
        self.active[name] += 1
        self.max_active[name] = max(self.max_active[name], self.active[name])
        try:
            if name in self.delay:
                await asyncio.sleep(self.delay[name])
            if name in self.errors:
                raise self.errors[name]()
            return self.results[name]
        finally:
            self.active[name] -= 1

    async def check(self, slug: str, trigger: str) -> Mapping[str, Any]:
        return await self._run("check", slug, trigger)

    async def recover_queue(self) -> int:
        self.calls.append(("recover_queue", ()))
        return self.recovered

    async def due_refresh_slugs(self) -> dict[str, int]:
        self.calls.append(("due_refresh_slugs", ()))
        return dict(self.due)

    async def refresh_pending(self, slug: str, trigger: str) -> Mapping[str, Any]:
        return await self._run("refresh_pending", slug, trigger)

    async def refresh_city(self, slug: str, trigger: str) -> Mapping[str, Any]:
        return await self._run("refresh_city", slug, trigger)

    async def market_stats(self, trigger: str) -> Mapping[str, Any]:
        return await self._run("market_stats", trigger)

    async def analyze(self, trigger: str) -> Mapping[str, Any]:
        return await self._run("analyze", trigger)

    async def publish_full(self, slug: str, trigger: str, cadence: int) -> Mapping[str, Any]:
        return await self._run("publish_full", slug, trigger, cadence)

    async def publish_freshness(self, slug: str) -> bool:
        self.calls.append(("publish_freshness", (slug,)))
        if "publish_freshness" in self.errors:
            raise self.errors["publish_freshness"]()
        return self.light_pushed

    async def mark_status(self, slug: str, status: str, next_check_at: datetime | None) -> bool:
        self.calls.append(("mark_status", (slug, status)))
        if "mark_status" in self.errors:
            raise self.errors["mark_status"]()
        return True

    async def set_next_check(self, slug: str, next_check_at: datetime) -> None:
        self.calls.append(("set_next_check", (slug, next_check_at)))

    async def set_local_status(self, slug: str, status: str) -> None:
        self.calls.append(("set_local_status", (slug, status)))

    async def freshness(self, slug: str) -> tuple[datetime | None, str | None]:
        return self.freshness_state.get(slug, (None, None))

    async def last_market_stats_at(self) -> datetime | None:
        return self.stats_at

    async def heartbeat(self, note: str) -> None:
        self.calls.append(("heartbeat", (note,)))

    async def close(self) -> None:
        return None
