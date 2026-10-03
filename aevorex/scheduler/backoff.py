"""Per-source exponential backoff: 5 -> 10 -> 20 -> 40 -> 80 minutes, capped at 2 hours."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class SourceBackoff:
    """Tracks consecutive Redfin-facing failures (405s, blocks, incomplete snapshots)."""

    base_minutes: int = 5
    cap_minutes: int = 120
    failures: int = 0
    until: datetime | None = None

    def delay(self, failures: int) -> timedelta:
        if failures < 1:
            return timedelta(0)
        return timedelta(minutes=min(self.cap_minutes, self.base_minutes * 2 ** (failures - 1)))

    def record_failure(self, now: datetime) -> timedelta:
        """Register a failure and return how long to hold off."""
        self.failures += 1
        delay = self.delay(self.failures)
        self.until = now + delay
        return delay

    def record_success(self) -> None:
        self.failures = 0
        self.until = None

    def active(self, now: datetime) -> bool:
        return self.until is not None and now < self.until

    def remaining(self, now: datetime) -> timedelta:
        if self.until is None or now >= self.until:
            return timedelta(0)
        return self.until - now
