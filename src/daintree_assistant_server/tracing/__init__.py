"""The training-trace plane: content-bearing, restricted, and separate.

This is NOT ``observability.py``. That module is the operational plane — metadata
only, broad operator access, short retention — and the two are kept apart on
purpose. Merging them would produce a logging system that carries user content to
everyone who can read logs, and an access-control problem with no good answer.

Nothing here may reach ``logger.info``, an exception message, a metric label or a
request id. See ``docs/PROVIDER_POLICY.md`` and ``docs/INTERNAL_BETA.md``.
"""

from __future__ import annotations

from .eligibility import TraceEligibility, decide
from .models import (
    TRACE_SCHEMA_VERSION,
    ContentRef,
    InvocationUsage,
    ModelInvocationTrace,
    TraceEvent,
    TraceHeader,
    TraceOutcome,
)
from .recorder import (
    NULL_SCOPE,
    DefaultTraceRecorder,
    NullTraceRecorder,
    NullTraceScope,
    TraceRecorder,
)
from .scope import TraceScope
from .sinks.base import TraceSink
from .sinks.filesystem import FilesystemTraceSink
from .sinks.null import NullTraceSink

__all__ = [
    "NULL_SCOPE",
    "TRACE_SCHEMA_VERSION",
    "ContentRef",
    "DefaultTraceRecorder",
    "FilesystemTraceSink",
    "InvocationUsage",
    "ModelInvocationTrace",
    "NullTraceRecorder",
    "NullTraceScope",
    "NullTraceSink",
    "TraceEligibility",
    "TraceEvent",
    "TraceHeader",
    "TraceOutcome",
    "TraceRecorder",
    "TraceScope",
    "TraceSink",
    "decide",
]
