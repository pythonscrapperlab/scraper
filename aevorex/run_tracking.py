"""PII-free lifecycle tracking for command-line runs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import TypeAlias, TypeVar
from uuid import UUID, uuid4

from sqlalchemy import select, update

from aevorex.db.models import Run, utc_now
from aevorex.db.session import async_session_maker

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
T = TypeVar("T")


def numeric_counts(value: object) -> dict[str, JsonValue]:
    """Keep only aggregate numeric/boolean counters from a command result."""
    if not isinstance(value, Mapping):
        return {}

    result: dict[str, JsonValue] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        if isinstance(raw_value, bool | int | float) or raw_value is None:
            result[key] = raw_value
        elif isinstance(raw_value, Mapping):
            nested = numeric_counts(raw_value)
            if nested:
                result[key] = nested
    return result


async def start_run(kind: str, scope: Mapping[str, JsonValue]) -> UUID:
    """Insert and commit a running ledger row before work starts."""
    run_id = uuid4()
    async with async_session_maker() as session:
        record = Run(
            id=run_id,
            kind=kind,
            scope=dict(scope),
            status="running",
            counts={},
        )
        session.add(record)
        await session.commit()
        return run_id


async def finish_run(
    run_id: UUID,
    *,
    status: str,
    counts: Mapping[str, JsonValue] | None = None,
    error_class: str | None = None,
) -> None:
    """Finalize a run without persisting exception messages or user data."""
    finished_at = utc_now()
    async with async_session_maker() as session:
        result = await session.execute(select(Run.started_at).where(Run.id == run_id))
        started_at = result.scalar_one_or_none()
        if started_at is None:
            raise RuntimeError("Run ledger row disappeared before finalization")
        await session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(
                finished_at=finished_at,
                status=status,
                counts=dict(counts or {}),
                error_class=error_class,
                duration_s=_duration_seconds(started_at, finished_at),
            )
        )
        await session.commit()


async def tracked(
    kind: str,
    scope: Mapping[str, JsonValue],
    operation: Callable[[], Awaitable[T]],
) -> T:
    """Execute an async operation and persist its terminal state."""
    run_id = await start_run(kind, scope)
    try:
        result = await operation()
    except BaseException as exc:
        await finish_run(
            run_id,
            status="cancelled" if isinstance(exc, KeyboardInterrupt) else "failed",
            error_class=type(exc).__name__,
        )
        raise

    await finish_run(run_id, status="succeeded", counts=numeric_counts(result))
    return result


def _duration_seconds(started_at: datetime, finished_at: datetime) -> float:
    return max(0.0, (finished_at - started_at).total_seconds())
