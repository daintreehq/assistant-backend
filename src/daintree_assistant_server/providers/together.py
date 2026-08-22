"""Together AI — the serving host for DeepSeek V4 Flash.

Together speaks the SAME wire as OpenRouter for everything this server sends, so
this adapter is the OpenRouter one with three things moved: where it lives, how a
key is probed, and which attribution headers mean anything. Measured against the
real API before it was written — ``max_tokens`` (never ``max_completion_tokens``),
OpenAI-shaped streaming chunks, tool calls with an ``index``, and — the one that
was genuinely in doubt — **the unified ``reasoning`` object is honoured**:
``{"enabled": false}`` drops ``reasoning_tokens`` from the usage block entirely
(12 completion tokens to 2 on the probe, reproduced over 3 trials). That single
fact is why this is a subclass rather than a third adapter: the parent's body is
correct here as written, including the thinking posture.

One measured caveat, recorded because it is invisible until it bills you: the
disable is **per model snapshot**. ``DeepSeek-V4-Flash-0731`` and the floating
``DeepSeek-V4-Pro`` alias honour all three spellings Together accepts
(``reasoning``, ``reasoning_effort``, ``chat_template_kwargs``), while the pinned
``DeepSeek-V4-Pro-0813`` honours ONLY ``chat_template_kwargs`` and ignores the
other two silently — no error, reasoning tokens billed anyway. Flash is the model
this server runs and it is unaffected; re-measure before pinning a new snapshot.

**Why Together rather than OpenRouter for the same weights.** OpenRouter is a
router and this server sends it no routing preferences at all — ``policy.py`` was
deleted with the routing policy and no ``provider`` block is built — so its endpoint
choice is whatever the default is that minute. Replaying the 62 real utility-task
payloads: Together p50 1100ms / p90 1702ms, OpenRouter p50 3414ms / p90 6013ms.
Same model, same prompts, 3.1x. Pinning the host IS the latency decision.

**No key-probe endpoint.** Together has no ``/key`` route, so ``verify_key``
probes ``/models`` instead — a 200 there means the credential is real, which is
the same verdict ``/key`` gives and the same one an adapter would take from
its own model list.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from ..errors import upstream_protocol_error
from .openrouter import OpenRouterProvider

DEFAULT_BASE_URL = "https://api.together.xyz/v1"

#: Together has no ``/key`` route. ``/models`` answers 200 for a usable key and
#: 401 for a dead one, which is the only distinction ``verify_key`` draws.
_KEY_PROBE_PATH = "/models"


class TogetherProvider(OpenRouterProvider):
    """The OpenAI-shape adapter, pointed at Together.

    Everything that makes the parent work — body construction, the unified
    ``reasoning`` object, SSE parsing, tool-call accumulation, error mapping,
    retries, redaction, the dev-only request dump — is inherited unchanged. A
    behavioural difference between the two hosts would be a bug in one of them,
    not something for this class to paper over.
    """

    default_base_url: ClassVar[str] = DEFAULT_BASE_URL
    key_probe_path: ClassVar[str] = _KEY_PROBE_PATH
    #: Together runs no public app ranking, so the parent's attribution headers
    #: would be two bytes on every request buying nothing. Sent empty rather than
    #: inherited.
    attribution_headers: ClassVar[Mapping[str, str]] = {}

    def _decode_key_record(self, payload: Any) -> dict[str, Any]:
        """``/models`` answers a bare LIST, not OpenRouter's ``{"data": {...}}``.

        Measured: 200 with a top-level JSON array of model objects for a good
        key, 401 for a dead one. The parent's decoder requires a mapping under
        ``data`` and would raise ``upstream_protocol_error`` on every VALID
        Together key — reporting "we could not tell" about a credential that had
        just proved itself, which is the one answer this endpoint must never
        give when it does in fact know.

        The record is shaped as ``{"models": [...]}`` to match what a plain
        adapter returns from its own model listing, so a caller reading the
        diagnostic does not have to know which host answered.
        """
        if not isinstance(payload, list):
            raise upstream_protocol_error("The provider returned no model listing.")
        return {
            "models": [
                m["id"] for m in payload if isinstance(m, dict) and isinstance(m.get("id"), str)
            ]
        }
