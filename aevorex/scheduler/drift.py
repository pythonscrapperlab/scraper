"""Clock-drift check. Slots are wall-clock based, so a wrong clock silently shifts every run."""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import httpx

LOGGER = logging.getLogger(__name__)
NTP_UNIX_DELTA = 2_208_988_800  # seconds between 1900-01-01 and 1970-01-01
NTP_SERVERS = ("time.windows.com", "pool.ntp.org")
HTTP_FALLBACK = "https://www.cloudflare.com"


@dataclass(frozen=True)
class DriftReading:
    offset_seconds: float | None  # local clock minus reference; positive = local is fast
    source: str  # ntp host, "http-date", or "unavailable"
    status: str  # ok | warn | fail | unknown


def classify(offset: float | None, warn: float, fail: float) -> str:
    if offset is None:
        return "unknown"
    magnitude = abs(offset)
    return "fail" if magnitude >= fail else "warn" if magnitude >= warn else "ok"


def parse_sntp(packet: bytes) -> float:
    """Server transmit timestamp (unix seconds) from a 48-byte SNTP reply."""
    if len(packet) < 48:
        raise ValueError("ShortSntpPacket")
    seconds, fraction = struct.unpack("!II", packet[40:48])
    if seconds == 0:
        raise ValueError("UnsynchronizedServer")
    return float(seconds - NTP_UNIX_DELTA + fraction / 2**32)


def local_offset(sent: float, received: float, server_time: float) -> float:
    """Local minus server, assuming a symmetric network path."""
    return ((sent + received) / 2) - server_time


def _sntp_once(host: str, timeout: float) -> float:
    request = b"\x1b" + 47 * b"\0"  # LI=0, VN=3, mode=3 (client)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sent = time.time()
        sock.sendto(request, (host, 123))
        reply, _ = sock.recvfrom(512)
        received = time.time()
    return local_offset(sent, received, parse_sntp(reply))


async def _http_date_offset(timeout: float) -> float:
    async with httpx.AsyncClient(timeout=timeout) as client:
        sent = time.time()
        response = await client.head(HTTP_FALLBACK)
        received = time.time()
    server = parsedate_to_datetime(response.headers["date"]).timestamp()
    # Date has 1 s resolution: good enough to catch real drift, not to measure milliseconds.
    return ((sent + received) / 2) - server


async def check_clock(
    warn: float = 5.0, fail: float = 30.0, *, timeout: float = 3.0
) -> DriftReading:
    """Try SNTP, then an HTTPS Date header; never raises."""
    for host in NTP_SERVERS:
        try:
            offset = await asyncio.to_thread(_sntp_once, host, timeout)
            return DriftReading(offset, host, classify(offset, warn, fail))
        except Exception as exc:
            LOGGER.debug("SNTP query failed: host=%s error_class=%s", host, type(exc).__name__)
    try:
        offset = await _http_date_offset(timeout)
        return DriftReading(offset, "http-date", classify(offset, warn, fail))
    except Exception as exc:
        LOGGER.warning("Clock reference unavailable: error_class=%s", type(exc).__name__)
        return DriftReading(None, "unavailable", "unknown")
