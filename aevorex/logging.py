"""
core/logging.py
Structured logging setup — writes to stdout and to a rotating log file.
"""

import logging
import logging.handlers
import os
import sys

from aevorex.config import settings


def setup_logging() -> None:
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Ensure log directory exists
    log_dir = os.path.dirname(settings.log_file) # type: ignore
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(log_level)

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)
    # Rotating file handler (10 MB × 5 backups)
    file_handler = logging.handlers.RotatingFileHandler(
        settings.log_file, # type: ignore
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Quiet noisy third-party loggers
    for noisy in ("httpx", "httpcore", "playwright", "selenium", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
