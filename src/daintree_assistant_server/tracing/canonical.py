"""Deterministic serialization for trace blobs, and why it sorts.

This module holds the ONE encoder in the service that canonicalizes key order,
and the reason is worth stating next to it because the codebase deliberately
refuses to sort everywhere else.

``services/prompt_assembler.py::_wire_bytes`` and ``benchmarks/_harness/wire.py``
both encode with insertion order preserved, because the question they answer is
"will the provider's prefix cache match these bytes?" — and a prefix cache matches
literal bytes, so two objects with the same keys in a different order are a MISS
that a sorted digest would report as a hit.

A trace blob answers a different question: "is this the same content I already
stored?" Two recordings of the same request must deduplicate to one blob whether
or not a dict happened to be built in the same order, and an exporter reading a
trace a year from now must be able to re-derive the hash. So this one sorts.

Both hashes are kept where both matter (``canonical_sha256`` for identity,
``wire_sha256`` for provider-byte identity) rather than one being made to serve
both purposes — see ``ModelInvocationTrace``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: Trace content is stored and compared as UTF-8.
ENCODING = "utf-8"


def canonical_bytes(value: Any) -> bytes:
    """Serialize for trace IDENTITY: sorted, compact, non-ASCII preserved.

    ``allow_nan=False`` on purpose: NaN/Infinity are not JSON, and a blob an
    exporter cannot parse with a standard reader is not a durable record. A
    payload carrying them fails here, loudly, at capture time rather than in a
    training pipeline months later.

    ``ensure_ascii=False`` for the same reason it is set in the assembler: the
    default escapes one CJK character to six, which would make a Japanese
    session's stored bytes disagree with its real size.
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode(ENCODING)


def wire_bytes(value: Any) -> bytes:
    """Serialize as the PROVIDER will see it: insertion order kept.

    Mirrors ``prompt_assembler._wire_bytes``. Recorded beside the canonical form
    for a model invocation, because "what exactly went on the wire" and "is this
    semantically the request I already have" are different questions and a single
    hash cannot answer both.
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(ENCODING)


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_hex(canonical_bytes(value))


def wire_sha256(value: Any) -> str:
    return sha256_hex(wire_bytes(value))


def as_blob(content: bytes | str | dict[str, Any] | list[Any]) -> tuple[bytes, str]:
    """Normalize any recordable payload to ``(raw_bytes, media_type)``.

    Bytes pass through as opaque; ``str`` is stored as text; anything else is
    canonicalized as JSON. Keeping this in one place is what stops a caller
    hand-rolling ``json.dumps`` with default separators and storing a blob whose
    hash nothing else can reproduce.
    """
    if isinstance(content, bytes):
        return content, "application/octet-stream"
    if isinstance(content, str):
        return content.encode(ENCODING), "text/plain"
    return canonical_bytes(content), "application/json"
