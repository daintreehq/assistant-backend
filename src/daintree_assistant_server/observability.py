"""Structured logging and request identifiers — the OPERATIONAL plane.

Logs carry *metadata*, never content: no message text, tool arguments,
project instructions, memories, terminal tails, runbook bodies, bearer keys, or
state tokens. Helpers here make that the easy default.

**This is not the only plane, and the distinction is load-bearing.** ``tracing/``
is the other one: content-bearing, restricted, off by default, and written for
replay and model training. They are NOT two verbosity levels of one system.
Merging them would put user content in front of everyone who can read a log line
and leave access control with no good answer — which is exactly why the trace
store exists separately rather than as a `DEBUG` level here.

Nothing from the trace plane may cross into this one. That includes content
hashes: a stable digest in Cloud Logging is a cross-session fingerprint, so two
turns sharing one are two turns sharing the text. Counts and byte totals are
fine; hashes stay in the restricted trace manifest.
``tests/unit/test_tracing_log_separation.py`` holds the boundary.
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


def new_call_id() -> str:
    """A server-originated synthetic tool-call id (``call_srv_...``)."""
    return f"call_srv_{_sortable_id('x').split('_', 1)[1]}"


def new_task_run_id() -> str:
    return _sortable_id("taskrun")


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
