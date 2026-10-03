"""Dry-run schedule preview and nightly-capacity simulation."""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from aevorex.db.models import MarketFreshness, Run
from aevorex.db.session import async_session_maker
from aevorex.scheduler.cities import ActiveCity
from aevorex.scheduler.config import MarketSchedule, ScheduleConfig
from aevorex.scheduler.slots import REFRESH, PlannedRun, next_slot, plan


@dataclass(frozen=True)
class NightlyInput:
    slug: str
    timezone: str
    schedule: MarketSchedule
    offset_minutes: int
    duration_s: float
    estimated: bool


@dataclass(frozen=True)
class CapacityRow:
    slug: str
    start_utc: datetime
    end_utc: datetime
    deadline_utc: datetime
    duration_s: float
    estimated: bool

    @property
    def fits(self) -> bool:
        return self.end_utc <= self.deadline_utc


def simulate_nightly(inputs: Sequence[NightlyInput], after: datetime) -> list[CapacityRow]:
    """Run the single refresh lane to completion and compare each city to its deadline.

    The deadline is the market-local ``window_start`` (07:00) after the nightly slot, the
    moment daytime checks resume (AGENTS.md section 7: 02:00-07:00 budget).
    """
    jobs: list[tuple[datetime, NightlyInput]] = []
    for item in inputs:
        slot = next_slot(
            REFRESH, item.schedule, ZoneInfo(item.timezone), item.offset_minutes, after
        )
        if slot is not None:
            jobs.append((slot, item))
    jobs.sort(key=lambda pair: (pair[0], pair[1].slug))
    rows: list[CapacityRow] = []
    lane_free: datetime | None = None
    for slot, item in jobs:
        start = slot if lane_free is None else max(slot, lane_free)
        end = start + timedelta(seconds=item.duration_s)
        tz = ZoneInfo(item.timezone)
        local_slot = slot.astimezone(tz)
        deadline = datetime.combine(local_slot.date(), item.schedule.window_start, tz).astimezone(UTC)
        rows.append(CapacityRow(item.slug, start, end, deadline, item.duration_s, item.estimated))
        lane_free = end
    return rows


async def refresh_durations(
    cities: Mapping[str, ActiveCity], config: ScheduleConfig
) -> dict[str, tuple[float, bool]]:
    """Median of the last five whole-city refreshes per market; else a stated estimate."""
    result: dict[str, tuple[float, bool]] = {}
    async with async_session_maker() as session:
        for slug in cities:
            durations = list(
                (
                    await session.execute(
                        select(Run.duration_s)
                        .where(
                            Run.kind == "refresh",
                            Run.status == "succeeded",
                            Run.scope["city"].astext == slug,
                            Run.scope["pending"].astext == "false",
                            Run.duration_s.is_not(None),
                        )
                        .order_by(Run.started_at.desc())
                        .limit(5)
                    )
                ).scalars()
            )
            if durations:
                result[slug] = (float(statistics.median(durations)), False)
                continue
            listings = await session.scalar(
                select(MarketFreshness.listings_active).where(MarketFreshness.slug == slug)
            )
            estimate = float(listings or 1500) * config.service.seconds_per_listing_estimate
            result[slug] = (estimate, True)
    return result


def build_preview(
    cities: Mapping[str, ActiveCity],
    config: ScheduleConfig,
    start: datetime,
    hours: float,
) -> list[PlannedRun]:
    return plan(
        [
            (slug, city.timezone, config.for_market(slug), city.stagger_minutes)
            for slug, city in sorted(cities.items())
        ],
        start,
        hours,
    )


def render_preview(
    runs: Sequence[PlannedRun],
    capacity: Sequence[CapacityRow],
    *,
    unsupported: Mapping[str, tuple[str, ...]],
    remote_ok: bool,
) -> str:
    lines = ["Planned runs (nothing is executed):", ""]
    lines.append(f"{'UTC':<17} {'LOCAL':<22} {'MARKET':<16} KIND")
    for item in runs:
        lines.append(
            f"{item.at_utc:%Y-%m-%d %H:%MZ}  {item.at_local:%Y-%m-%d %H:%M %Z}  "
            f"{item.slug:<16} {item.kind}"
        )
    lines += ["", "Nightly refresh capacity (single refresh lane, one city at a time):"]
    for row in capacity:
        verdict = "fits" if row.fits else "OVERRUNS"
        basis = "estimated" if row.estimated else "measured median"
        lines.append(
            f"  {row.slug:<16} {row.start_utc:%H:%MZ}-{row.end_utc:%H:%MZ} "
            f"({row.duration_s / 60:.0f} min, {basis}) deadline {row.deadline_utc:%H:%MZ}: {verdict}"
        )
    if any(not row.fits for row in capacity):
        lines.append("  WARNING: the active set cannot finish nightly inside its window; "
                     "add cities more slowly or lengthen the window.")
    if unsupported:
        lines += ["", "Active but UNSCHEDULABLE (no Redfin region id in scrapers/constants.py):"]
        lines += [f"  {slug} (from {', '.join(src)})" for slug, src in sorted(unsupported.items())]
    if not remote_ok:
        lines += ["", "NOTE: Supabase org markets could not be read; preview shows demo markets only."]
    return "\n".join(lines)


def as_json(runs: Sequence[PlannedRun], capacity: Sequence[CapacityRow]) -> dict[str, Any]:
    return {
        "runs": [
            {"utc": r.at_utc.isoformat(), "local": r.at_local.isoformat(), "market": r.slug,
             "kind": r.kind}
            for r in runs
        ],
        "capacity": [
            {"market": c.slug, "start": c.start_utc.isoformat(), "end": c.end_utc.isoformat(),
             "deadline": c.deadline_utc.isoformat(), "fits": c.fits, "estimated": c.estimated}
            for c in capacity
        ],
    }
