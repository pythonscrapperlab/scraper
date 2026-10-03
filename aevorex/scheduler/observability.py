"""Sentry and Healthchecks.io integrations; every one is a silent no-op when unconfigured."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

import httpx
from pydantic import SecretStr

from aevorex.scheduler.logs import scrub

LOGGER = logging.getLogger(__name__)
_SENTRY_ACTIVE = False


def scrub_event(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Sentry ``before_send``: keep error classes and counts, drop everything free-text."""
    del hint
    for exception in event.get("exception", {}).get("values", []) or []:
        exception["value"] = ""  # exception messages may embed listing URLs or addresses
        for frame in (exception.get("stacktrace") or {}).get("frames", []) or []:
            for key in ("vars", "pre_context", "context_line", "post_context"):
                frame.pop(key, None)
    for key in ("request", "user", "breadcrumbs", "extra", "contexts_extra"):
        event.pop(key, None)
    if isinstance(event.get("message"), str):
        event["message"] = scrub(event["message"])
    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        logentry["message"] = scrub(str(logentry.get("message", "")))
        logentry.pop("params", None)
    event["server_name"] = "aevoraex-engine"
    return event


def init_sentry(dsn: SecretStr | None, environment: str = "production") -> bool:
    """Initialise Sentry with PII collection disabled. Returns whether it is active."""
    global _SENTRY_ACTIVE
    if dsn is None or not dsn.get_secret_value():
        return False
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn.get_secret_value(),
        environment=environment,
        release="aevoraex-engine@e4",
        send_default_pii=False,
        include_local_variables=False,
        include_source_context=False,
        max_breadcrumbs=0,
        traces_sample_rate=0.0,
        server_name="aevoraex-engine",
        before_send=scrub_event,  # type: ignore[arg-type]
    )
    _SENTRY_ACTIVE = True
    return True


def report_failure(job: str, error_class: str, market: str | None = None) -> None:
    """Send a class-only failure event (no message, no stack locals)."""
    if not _SENTRY_ACTIVE:
        return
    import sentry_sdk

    with sentry_sdk.new_scope() as scope:
        scope.set_tag("job", job)
        scope.set_tag("error_class", error_class)
        if market:
            scope.set_tag("market", market)
        scope.fingerprint = [job, error_class]
        sentry_sdk.capture_message(f"{job} failed: {error_class}", level="error")


def report_warning(topic: str, market: str | None = None) -> None:
    if not _SENTRY_ACTIVE:
        return
    import sentry_sdk

    with sentry_sdk.new_scope() as scope:
        scope.set_tag("topic", topic)
        if market:
            scope.set_tag("market", market)
        scope.fingerprint = [topic, market or "-"]
        sentry_sdk.capture_message(topic, level="warning")


class Healthchecks:
    """Minimal Healthchecks.io client: heartbeat and per-run start/success/fail pings."""

    def __init__(self, *, timeout: float = 10.0, attempts: int = 2) -> None:
        self.timeout = timeout
        self.attempts = attempts

    async def ping(
        self,
        base: SecretStr | None,
        *,
        status: str = "",
        run_id: UUID | None = None,
        body: str | None = None,
    ) -> bool:
        """Ping ``base`` (+ ``/start`` or ``/fail``). Never raises; returns delivery success."""
        if base is None or not base.get_secret_value():
            return False
        url = base.get_secret_value().rstrip("/") + (f"/{status}" if status else "")
        params = {"rid": str(run_id)} if run_id is not None else None
        for attempt in range(self.attempts):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    if body is not None:
                        response = await client.post(url, params=params, content=body[:10_000])
                    else:
                        response = await client.get(url, params=params)
                    response.raise_for_status()
                return True
            except Exception as exc:
                LOGGER.warning(
                    "Healthchecks ping failed: attempt=%s error_class=%s", attempt + 1, type(exc).__name__
                )
                await asyncio.sleep(1)
        return False
