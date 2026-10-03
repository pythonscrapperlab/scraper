"""Pure alert and morning-brief rules (AGENTS.md sections 7, 8 step 6 and 8 "Morning brief").

Nothing here touches a database, the clock or the network, so every rule is unit-testable.
Scoring is frozen: these rules only *read* percentiles and tiers that already exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

TIER_RANK = {"rest": 0, "strong": 1, "top": 2}
LENSES = ("motivated_seller", "fix_flip", "buy_hold", "str", "airbnb")
DEFAULT_BRIEF_COUNT = {"starter": 5, "pro": 10, "growth": 15, "brokerage": 25}
BRIEF_LOCAL_TIME = time(7, 0)
# A laptop that slept through 07:00 still sends the brief, but a brief that would land after
# this local time is stale news and is skipped (and reported), never sent late.
BRIEF_LATEST_LOCAL = time(11, 0)


@dataclass(frozen=True)
class ScoreState:
    """One (property, lens) score as the serving cache holds it."""

    percentile: float
    tier: str


@dataclass(frozen=True)
class Threshold:
    """An org's alert rule for one lens. ``min_percentile`` wins when both are set."""

    min_percentile: float | None = None
    tier: str | None = None

    def __post_init__(self) -> None:
        if self.min_percentile is None and self.tier is None:
            raise ValueError("A threshold needs min_percentile or tier")
        if self.tier is not None and self.tier not in ("top", "strong"):
            raise ValueError("Threshold tier must be 'top' or 'strong'")


def qualifies(state: ScoreState | None, threshold: Threshold) -> bool:
    """Whether a score currently meets the threshold. A missing score never qualifies."""
    if state is None:
        return False
    if threshold.min_percentile is not None:
        return state.percentile >= threshold.min_percentile
    assert threshold.tier is not None
    return TIER_RANK[state.tier] >= TIER_RANK[threshold.tier]


def crossed(old: ScoreState | None, new: ScoreState | None, threshold: Threshold) -> bool:
    """True when the score *entered* the threshold on this push.

    ``old`` is the state in the serving cache before the push. A property with no previous
    score (new listing, or first score) therefore alerts if it qualifies. Staying above the
    line, or falling below it, is not a crossing - so a repeated push alerts exactly once.
    """
    return qualifies(new, threshold) and not qualifies(old, threshold)


@dataclass(frozen=True)
class QuietHours:
    """Org-local do-not-disturb window; ``start > end`` crosses midnight."""

    enabled: bool = True
    start: time = time(21, 0)
    end: time = time(7, 0)

    def contains(self, local: time) -> bool:
        if not self.enabled or self.start == self.end:
            return False
        if self.start < self.end:
            return self.start <= local < self.end
        return local >= self.start or local < self.end


def _local_wall(day: date, clock: time, tz: ZoneInfo) -> datetime:
    """Wall-clock to UTC, mapping a DST-gap time forward (the same rule slots.py uses)."""
    naive = datetime.combine(day, clock)
    aware = naive.replace(tzinfo=tz, fold=0)
    roundtrip = aware.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
    if roundtrip != naive:  # nonexistent local time: spring-forward gap
        aware = (aware.astimezone(UTC) + timedelta(hours=1)).astimezone(tz)
    return aware.astimezone(UTC)


def send_after(now: datetime, tz_name: str, quiet: QuietHours) -> datetime:
    """Earliest UTC instant an e-mail may leave: ``now`` unless inside quiet hours."""
    now = now.astimezone(UTC)
    tz = ZoneInfo(tz_name)
    local = now.astimezone(tz)
    if not quiet.contains(local.time().replace(tzinfo=None)):
        return now
    # Inside the window: wait for the next occurrence of quiet.end strictly after now.
    end_day = local.date()
    if quiet.start > quiet.end and local.time().replace(tzinfo=None) >= quiet.start:
        end_day = end_day + timedelta(days=1)
    release = _local_wall(end_day, quiet.end, tz)
    return release if release > now else now


def brief_count(plan: str, overrides: dict[str, int] | None = None) -> int:
    """Number of properties in a morning brief for an org's plan."""
    table = {**DEFAULT_BRIEF_COUNT, **(overrides or {})}
    return table.get(plan, table["starter"])


@dataclass(frozen=True)
class BriefWindow:
    """Outcome of the 07:00-org-local check for one org on one sweep."""

    due: bool
    local_date: date
    reason: str  # "due" | "too_early" | "too_late"


def brief_window(now: datetime, tz_name: str) -> BriefWindow:
    """Is it time for this org's brief? Due from 07:00 local until 11:00 local."""
    local = now.astimezone(ZoneInfo(tz_name))
    wall = local.time().replace(tzinfo=None)
    if wall < BRIEF_LOCAL_TIME:
        return BriefWindow(False, local.date(), "too_early")
    if wall >= BRIEF_LATEST_LOCAL:
        return BriefWindow(False, local.date(), "too_late")
    return BriefWindow(True, local.date(), "due")
