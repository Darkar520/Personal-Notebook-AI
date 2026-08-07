"""Logging con rotación en `data/logs/app.log`."""

from __future__ import annotations

import logging
import logging.handlers
import sys

from app import paths

_configured = False
FORMAT = "%(asctime)s %(levelname)-7s %(threadName)-14s %(name)s: %(message)s"


def setup_logging(level: str = "INFO") -> None:
    global _configured
    root = logging.getLogger()
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(numeric)
    if _configured:
        for handler in root.handlers:
            handler.setLevel(numeric)
        return

    formatter = logging.Formatter(FORMAT)

    file_handler = logging.handlers.RotatingFileHandler(
        paths.logs_dir() / "app.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(numeric)
    root.addHandler(file_handler)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    stream.setLevel(numeric)
    root.addHandler(stream)

    # httpx/httpcore son extremadamente verbosos en DEBUG.
    logging.getLogger("httpx").setLevel(max(numeric, logging.WARNING))
    logging.getLogger("httpcore").setLevel(max(numeric, logging.WARNING))
    logging.getLogger("multipart").setLevel(logging.WARNING)

    _configured = True
