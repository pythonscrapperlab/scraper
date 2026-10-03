"""Typed, validated scheduler configuration with per-market overrides."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "config" / "scheduler.yaml"


class ScheduleConfigError(ValueError):
    """Raised when scheduler configuration cannot be trusted."""


@dataclass(frozen=True)
class MarketSchedule:
    """When one market is checked and refreshed (market-local wall clock)."""

    check_cadence_minutes: int = 120
    window_start: time = time(7, 0)
    window_end: time = time(21, 0)
    overnight_check: time | None = time(3, 0)
    nightly_refresh: time | None = time(2, 0)
    stagger_minutes: int = 30

    def validate(self) -> None:
        if not 15 <= self.check_cadence_minutes <= 24 * 60:
            raise ScheduleConfigError("check_cadence_minutes must be 15..1440")
        if self.window_start >= self.window_end:
            raise ScheduleConfigError("window_start must be before window_end")
        if not 0 <= self.stagger_minutes < self.check_cadence_minutes:
            raise ScheduleConfigError("stagger_minutes must be 0..cadence-1")


@dataclass(frozen=True)
class ServiceSettings:
    """Service-wide timing, backoff and safety limits."""

    discovery_minutes: int = 5
    late_detector_minutes: int = 5
    late_grace_minutes: int = 30
    heartbeat_minutes: int = 10
    clock_check_minutes: int = 60
    clock_warn_seconds: float = 5.0
    clock_fail_seconds: float = 30.0
    misfire_grace_minutes: int = 30
    backoff_base_minutes: int = 5
    backoff_cap_minutes: int = 120
    block_rate_backoff: float = 0.5
    market_stats_min_interval_hours: int = 20
    nightly_finalize: time = time(6, 30)
    nightly_finalize_tz: str = "America/New_York"
    nightly_finalize_max_wait_minutes: int = 150
    seconds_per_listing_estimate: float = 0.6
    brief_sweep_minutes: int = 10
    brief_counts: dict[str, int] = field(
        default_factory=lambda: {"starter": 5, "pro": 10, "growth": 15, "brokerage": 25}
    )
    job_timeout_minutes: dict[str, int] = field(
        default_factory=lambda: {"check": 15, "refresh": 240, "analyze": 90, "publish": 20}
    )


@dataclass(frozen=True)
class ScheduleConfig:
    """Defaults plus per-market overrides."""

    defaults: MarketSchedule = field(default_factory=MarketSchedule)
    service: ServiceSettings = field(default_factory=ServiceSettings)
    overrides: dict[str, MarketSchedule] = field(default_factory=dict)

    def for_market(self, slug: str) -> MarketSchedule:
        return self.overrides.get(slug.strip().lower(), self.defaults)


def _time(value: Any, name: str) -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, int):  # YAML 1.1 reads bare 07:00 as sexagesimal minutes
        raise ScheduleConfigError(f"{name} must be a quoted HH:MM string")
    try:
        hour, minute = str(value).split(":")
        return time(int(hour), int(minute))
    except (ValueError, TypeError) as exc:
        raise ScheduleConfigError(f"{name} must be HH:MM") from exc


def _optional_time(value: Any, name: str) -> time | None:
    return None if value is None else _time(value, name)


_SCHEDULE_KEYS = {
    "check_cadence_minutes", "window_start", "window_end", "overnight_check",
    "nightly_refresh", "stagger_minutes",
}


def _schedule(raw: dict[str, Any], base: MarketSchedule) -> MarketSchedule:
    unknown = set(raw) - _SCHEDULE_KEYS
    if unknown:
        raise ScheduleConfigError(f"Unknown schedule keys: {sorted(unknown)}")
    values: dict[str, Any] = {}
    for key, value in raw.items():
        if key in {"window_start", "window_end"}:
            values[key] = _time(value, key)
        elif key in {"overnight_check", "nightly_refresh"}:
            values[key] = _optional_time(value, key)
        else:
            values[key] = int(value)
    result = replace(base, **values)
    result.validate()
    return result


def parse_config(raw: dict[str, Any] | None) -> ScheduleConfig:
    """Validate a parsed YAML mapping; fail closed on typos and bad windows."""
    raw = raw or {}
    unknown_top = set(raw) - {"defaults", "service", "markets"}
    if unknown_top:
        raise ScheduleConfigError(f"Unknown top-level keys: {sorted(unknown_top)}")
    defaults = _schedule(dict(raw.get("defaults") or {}), MarketSchedule())

    service_raw = dict(raw.get("service") or {})
    base = ServiceSettings()
    known = set(ServiceSettings.__dataclass_fields__)
    if set(service_raw) - known:
        raise ScheduleConfigError(f"Unknown service keys: {sorted(set(service_raw) - known)}")
    service_values: dict[str, Any] = {}
    for key, value in service_raw.items():
        if key == "nightly_finalize":
            service_values[key] = _time(value, key)
        elif key == "job_timeout_minutes":
            service_values[key] = {**base.job_timeout_minutes, **{k: int(v) for k, v in value.items()}}
        elif key == "brief_counts":
            service_values[key] = {**base.brief_counts, **{str(k): int(v) for k, v in value.items()}}
        elif key == "nightly_finalize_tz":
            ZoneInfo(str(value))
            service_values[key] = str(value)
        elif isinstance(getattr(base, key), float):
            service_values[key] = float(value)
        else:
            service_values[key] = int(value)
    service = replace(base, **service_values)

    overrides = {
        str(slug).strip().lower(): _schedule(dict(values or {}), defaults)
        for slug, values in (raw.get("markets") or {}).items()
    }
    return ScheduleConfig(defaults=defaults, service=service, overrides=overrides)


def load_config(path: Path | str | None = None) -> ScheduleConfig:
    """Load ``config/scheduler.yaml`` (or built-in defaults when it is absent)."""
    target = Path(path) if path else DEFAULT_PATH
    if not target.exists():
        return ScheduleConfig()
    return parse_config(yaml.safe_load(target.read_text(encoding="utf-8")))
