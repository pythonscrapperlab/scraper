"""Typed publisher results and size-guard policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SizeAction = Literal["ok", "warn", "stop_new_cities", "page"]


@dataclass(frozen=True)
class SizeGuard:
    """Supabase Free database-size policy from AGENTS.md section 5."""

    size_bytes: int
    size_mb: float
    action: SizeAction


def classify_size(size_bytes: int) -> SizeGuard:
    """Classify a database size at the exact 350/420/450 MB boundaries."""
    size_mb = size_bytes / (1024 * 1024)
    if size_mb >= 450:
        action: SizeAction = "page"
    elif size_mb >= 420:
        action = "stop_new_cities"
    elif size_mb >= 350:
        action = "warn"
    else:
        action = "ok"
    return SizeGuard(size_bytes=size_bytes, size_mb=round(size_mb, 2), action=action)


@dataclass(frozen=True)
class MarketDefinition:
    """Stable product-market identity and cloud presentation metadata."""

    slug: str
    city: str
    state: str
    region_id: str
    timezone: str
    is_demo: bool


@dataclass
class PublishResult:
    """Aggregate-only result safe for the local and remote run ledgers."""

    market: str
    status: Literal["succeeded", "partial"] = "succeeded"
    counts: dict[str, int] = field(default_factory=dict)
    rejected_property_ids: list[str] = field(default_factory=list)
    size_action: SizeAction = "ok"

    def telemetry(self) -> dict[str, int | float | bool]:
        """Return only aggregate values for PII-free CLI tracking."""
        result: dict[str, int | float | bool] = dict(self.counts)
        result["partial"] = self.status == "partial"
        result["rejected_scores"] = len(self.rejected_property_ids)
        return result
