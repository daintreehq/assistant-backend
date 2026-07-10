"""Exact wire serialization + the DeepSeek-cacheable-region model.

The flagship cost/cache benchmark's core number is "what fraction of the request
stays byte-identical turn-over-turn". That must be computed over the SAME bytes
DeepSeek tokenizes, i.e. the exact JSON body ``httpx`` POSTs — not a canonical
(sorted-key) re-encoding, which would diverge from the wire and could mask a real
tool-key reordering that busts the cache. So:

- :func:`wire_bytes` is ground truth: it hands the assembled body to ``httpx``
  and reads the bytes it would send.
- :func:`dumps` reproduces that encoder for per-fragment work; the flagship
  asserts ``dumps(body) == wire_bytes(body)`` once, so if httpx ever changes its
  JSON settings the mismatch fails loudly instead of measuring the wrong bytes.
- :func:`cacheable_region_bytes` models DeepSeek's serialization order — ``[all
  system messages] → [tools] → [non-system conversation]`` (the hoist documented
  in ``prompt_assembler.build_main_request``) — as the unit sequence whose stable
  byte prefix is the cache-reuse proxy. Units are joined with a record-separator
  so a boundary shift cannot fake a shared prefix across two units.

This is a byte PROXY (no tokenizer ships in the repo); the live ``cached_tokens``
cross-check is what validates its magnitude.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

# Record separator between serialized units, so the longest common byte prefix of
# two regions can never run past a unit boundary and over-credit stability.
_UNIT_SEP = b"\x1e"


def dumps(obj: Any) -> bytes:
    """Serialize as httpx serializes a ``json=`` body (compact, non-ASCII kept)."""
    return json.dumps(
        obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def wire_bytes(body: dict[str, Any]) -> bytes:
    """The exact bytes ``httpx`` would POST for ``json=body`` — ground truth."""
    return httpx.Request("POST", "http://benchmark.invalid/v1", json=body).read()


def cacheable_region_bytes(body: dict[str, Any]) -> bytes:
    """Reconstruct DeepSeek's cache-relevant prefix: system messages (array order),
    then tools, then the non-system conversation (array order)."""
    messages = body.get("messages") or []
    system = [m for m in messages if m.get("role") == "system"]
    other = [m for m in messages if m.get("role") != "system"]
    units: list[bytes] = [dumps(m) for m in system]
    if body.get("tools"):
        units.append(dumps(body["tools"]))
    units.extend(dumps(m) for m in other)
    return _UNIT_SEP.join(units)


def common_prefix_len(a: bytes, b: bytes) -> int:
    """Length of the longest common byte prefix of ``a`` and ``b``."""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def layer_segments(named_layers: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Per-layer byte + token-estimate budget for an ordered list of
    ``(name, rendered_text)`` prompt layers.

    ``token_est`` is a chars/4 PROXY (``text_utils.estimate_tokens``; no tokenizer in
    the repo) — trend-only, and it systematically under-counts the punctuation-heavy
    tool-schema region; never put it on the same axis as a live ``prompt_tokens``.
    """
    from daintree_assistant_server.text_utils import estimate_tokens

    out: list[dict[str, Any]] = []
    offset = 0
    for name, text in named_layers:
        raw = text.encode("utf-8")
        out.append(
            {
                "layer": name,
                "bytes": len(raw),
                "token_est": estimate_tokens(text),
                "byte_offset": offset,
            }
        )
        offset += len(raw)
    return out
