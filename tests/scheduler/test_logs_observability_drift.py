"""PII scrubbing, Sentry event scrubbing and clock-drift arithmetic."""

from __future__ import annotations

import json
import logging
import struct

import pytest

from aevorex.scheduler.drift import NTP_UNIX_DELTA, classify, local_offset, parse_sntp
from aevorex.scheduler.logs import JsonFormatter, ScrubFilter, scrub
from aevorex.scheduler.observability import scrub_event


@pytest.mark.parametrize(
    ("raw", "forbidden"),
    [
        ("fetch https://www.redfin.com/FL/Orlando/123-Main-St-32801/home/42 failed", "redfin.com"),
        ("agent jane.doe@example.com called", "jane.doe"),
        ("phone (407) 555-0143 or 407-555-0143 or +1 407 555 0143", "555"),
        ("listing at 1234 Oak Street, unit 5", "1234 Oak"),
        ("listing at 88 Palm Beach Blvd.", "Palm Beach"),
    ],
)
def test_scrub_redacts_pii_shapes(raw: str, forbidden: str) -> None:
    assert forbidden not in scrub(raw)


def test_scrub_leaves_operational_text_alone() -> None:
    line = "Stage complete: stage=check market=orlando-fl seconds=25.3 counts={\"listings\": 2539}"
    assert scrub(line) == line


def test_json_formatter_emits_one_object_with_scrubbed_message() -> None:
    record = logging.LogRecord(
        "aevorex.scheduler", logging.ERROR, __file__, 1,
        "failed at %s", ("https://example.invalid/a/b",), None,
    )
    payload = json.loads(JsonFormatter().format(record))
    assert payload["level"] == "ERROR"
    assert payload["msg"] == "failed at [url]"
    assert payload["ts"].endswith("+00:00")


def test_exception_text_never_reaches_the_log_only_its_class() -> None:
    logger = logging.getLogger("aevorex.test.pii")
    logger.setLevel(logging.INFO)
    captured: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(self.format(record))

    handler = Capture()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ScrubFilter())
    logger.addHandler(handler)
    try:
        try:
            raise ValueError("owner Jane Roe at 12 Elm Street https://x.invalid/p")
        except ValueError:
            logger.exception("job failed")
    finally:
        logger.removeHandler(handler)
    payload = json.loads(captured[0])
    assert payload["error_class"] == "ValueError"
    assert "Jane" not in captured[0] and "Elm" not in captured[0] and "x.invalid" not in captured[0]
    assert "Traceback" not in captured[0]


def test_sentry_event_is_stripped_to_classes_and_counts() -> None:
    event = {
        "exception": {
            "values": [
                {
                    "type": "HTTPStatusError",
                    "value": "405 for https://www.redfin.com/FL/x/1-Main-St",
                    "stacktrace": {
                        "frames": [
                            {"function": "f", "vars": {"url": "secret"}, "context_line": "x = 1",
                             "pre_context": ["a"], "post_context": ["b"]}
                        ]
                    },
                }
            ]
        },
        "request": {"url": "https://x"},
        "user": {"email": "a@b.co"},
        "breadcrumbs": [{"message": "m"}],
        "extra": {"address": "1 Main St"},
        "message": "owner a@b.co failed",
        "server_name": "SECRET-LAPTOP",
    }
    cleaned = scrub_event(event, None)
    assert cleaned is not None
    values = cleaned["exception"]["values"]
    assert values[0]["type"] == "HTTPStatusError"
    assert values[0]["value"] == ""
    frame = values[0]["stacktrace"]["frames"][0]
    assert set(frame) == {"function"}
    for key in ("request", "user", "breadcrumbs", "extra"):
        assert key not in cleaned
    assert "a@b.co" not in cleaned["message"]
    assert cleaned["server_name"] == "aevoraex-engine"


def _ntp_packet(unix_seconds: float) -> bytes:
    seconds = int(unix_seconds) + NTP_UNIX_DELTA
    fraction = int((unix_seconds % 1) * 2**32)
    return b"\x1c" + bytes(39) + struct.pack("!II", seconds, fraction)


def test_parse_sntp_round_trips_the_transmit_timestamp() -> None:
    assert parse_sntp(_ntp_packet(1_790_000_000.5)) == pytest.approx(1_790_000_000.5, abs=1e-6)


@pytest.mark.parametrize("packet", [b"", b"\x1c" * 10, b"\x1c" + bytes(47)])
def test_parse_sntp_rejects_short_or_unsynchronised_replies(packet: bytes) -> None:
    with pytest.raises(ValueError):
        parse_sntp(packet)


def test_local_offset_is_local_minus_server_with_symmetric_path() -> None:
    assert local_offset(100.0, 100.2, 99.0) == pytest.approx(1.1)
    assert local_offset(100.0, 100.2, 101.1) == pytest.approx(-1.0)


@pytest.mark.parametrize(
    ("offset", "status"),
    [(0.0, "ok"), (4.99, "ok"), (-5.0, "warn"), (29.9, "warn"), (30.0, "fail"), (-90.0, "fail"),
     (None, "unknown")],
)
def test_drift_classification(offset: float | None, status: str) -> None:
    assert classify(offset, 5.0, 30.0) == status
