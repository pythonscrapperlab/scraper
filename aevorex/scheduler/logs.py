"""JSON service logging with daily rotation and a defence-in-depth PII scrubber.

Application code already avoids logging PII (AGENTS.md section 10). The scrubber exists
because exception text and third-party libraries are outside our control: anything that
looks like a URL, e-mail address, phone number or street address is redacted before it
reaches disk or stdout, and exception *messages* are never written, only their class.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

_STREET = (
    r"Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Court|Ct|Circle|Cir|"
    r"Way|Place|Pl|Terrace|Ter|Trail|Trl|Parkway|Pkwy|Highway|Hwy"
)
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE), "[url]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"), "[phone]"),
    (
        re.compile(rf"\b\d{{1,6}}\s+(?:[A-Za-z0-9.'-]+\s+){{1,4}}(?:{_STREET})\b\.?", re.IGNORECASE),
        "[address]",
    ),
)


def scrub(text: str) -> str:
    """Redact URLs, e-mail addresses, phone numbers and street addresses."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class ScrubFilter(logging.Filter):
    """Resolve the message, scrub it, and drop exception text (class only is kept)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = ()
        if record.exc_info and record.exc_info[0] is not None:
            record.exc_class = record.exc_info[0].__name__
            record.exc_info = None
            record.exc_text = None
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, msg, plus selected context."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": scrub(record.getMessage()),
        }
        exc_class = getattr(record, "exc_class", None)
        if exc_class:
            payload["error_class"] = exc_class
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(
    log_path: Path | str,
    *,
    level: str = "INFO",
    console_level: str = "INFO",
    backups: int = 30,
) -> None:
    """Install JSON handlers on the root logger (replacing any existing ones)."""
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    file_handler = logging.handlers.TimedRotatingFileHandler(
        path, when="midnight", backupCount=backups, encoding="utf-8", utc=True, delay=True
    )
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, console_level.upper(), logging.INFO))
    for handler in (file_handler, console):
        handler.setFormatter(JsonFormatter())
        handler.addFilter(ScrubFilter())
        root.addHandler(handler)

    for noisy in ("httpx", "httpcore", "playwright", "selenium", "urllib3", "apscheduler.executors"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    def excepthook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        del exc, tb
        logging.getLogger("aevorex.scheduler").critical(
            "Unhandled exception: error_class=%s", exc_type.__name__
        )

    sys.excepthook = excepthook
