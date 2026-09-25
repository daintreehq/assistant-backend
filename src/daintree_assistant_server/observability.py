"""Structured logging and request identifiers.

Logs carry metadata, never content: a search query is user content and stays
out of the log plane, like every other request body.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
import time
from typing import Any

import structlog

# Crockford-ish base32 alphabet for compact, sortable identifiers.
_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def _encode_base32(value: int, width: int) -> str:
    chars = []
    for _ in range(width):
        value, rem = divmod(value, 32)
        chars.append(_ALPHABET[rem])
    return "".join(reversed(chars))


def _sortable_id(prefix: str) -> str:
    """A lexicographically-sortable id: ``<prefix>_<time><random>``."""
    millis = int(time.time() * 1000)
    time_part = _encode_base32(millis, 10)
    rand_part = _encode_base32(secrets.randbits(40), 8)
    return f"{prefix}_{time_part}{rand_part}"


def new_request_id() -> str:
    return _sortable_id("req")


_configured = False


def configure_logging(level: str = "INFO") -> None:
    """Configure structlog to emit structured JSON to stdout."""
    global _configured
    if _configured:
        return

    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=log_level)

    renderer: structlog.types.Processor
    if os.getenv("DAINTREE_LOG_CONSOLE"):
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str = "daintree") -> Any:
    return structlog.get_logger(name)


def bind_request_context(**fields: Any) -> None:
    structlog.contextvars.bind_contextvars(**fields)


def clear_request_context() -> None:
    structlog.contextvars.clear_contextvars()
