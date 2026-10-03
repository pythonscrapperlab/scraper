"""Cross-process concurrency limits via PostgreSQL session-level advisory locks.

The service and ``scheduler run-now`` are separate processes on one laptop, so an
in-process semaphore cannot enforce "one check + one refresh at a time". Advisory locks
live in the local database both already use, vanish if a process dies, and need no files.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

LOGGER = logging.getLogger(__name__)

NAMESPACE = 0x41455634  # "AEV4" - first key of the two-int advisory lock form
SERVICE = "service"
CHECK = "check"
REFRESH = "refresh"
ANALYZE = "analyze"  # market-stats, valuation and scoring share one lane
PUBLISH = "publish"
_KEYS = {SERVICE: 1, CHECK: 2, REFRESH: 3, ANALYZE: 4, PUBLISH: 5}


class LockBusy(RuntimeError):
    """Raised when a lock is held elsewhere and the caller chose not to wait."""


def _default_engine() -> AsyncEngine:
    from aevorex.db.session import engine

    return engine


@asynccontextmanager
async def advisory_lock(
    name: str,
    *,
    wait: bool = True,
    poll_seconds: float = 2.0,
    timeout_seconds: float | None = None,
    engine: AsyncEngine | None = None,
) -> AsyncIterator[None]:
    """Hold lock ``name`` for the duration of the block.

    ``wait=False`` raises :class:`LockBusy` immediately. Waiting polls with
    ``pg_try_advisory_lock`` (never blocks a pooled connection or the event loop).
    """
    key = _KEYS[name]
    bound = engine or _default_engine()
    connection = await bound.connect()
    await connection.execution_options(isolation_level="AUTOCOMMIT")
    started = time.monotonic()
    acquired = False
    try:
        while True:
            acquired = bool(
                await connection.scalar(
                    text("select pg_try_advisory_lock(:ns, :key)"), {"ns": NAMESPACE, "key": key}
                )
            )
            if acquired:
                break
            if not wait or (
                timeout_seconds is not None and time.monotonic() - started >= timeout_seconds
            ):
                raise LockBusy(name)
            await asyncio.sleep(poll_seconds)
        yield
    finally:
        try:
            if acquired:
                await connection.scalar(
                    text("select pg_advisory_unlock(:ns, :key)"), {"ns": NAMESPACE, "key": key}
                )
        except Exception as exc:  # a dead session already released the lock
            LOGGER.warning(
                "Advisory unlock failed: lock=%s error_class=%s", name, type(exc).__name__
            )
            await connection.invalidate()
        finally:
            await connection.close()
