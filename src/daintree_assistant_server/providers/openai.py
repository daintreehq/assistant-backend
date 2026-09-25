"""OpenAI — a bring-your-own-key host, reached only with the caller's credential.

This adapter is never built with a server key. It exists so a caller who picked
OpenAI in Daintree's assistant settings can run the whole pipeline — selection,
assembly, tools, tasks — against their own account. See ``upstream.py`` for how a
request is routed here.

The OpenAI-shape parent carries nearly everything. What differs, every item
measured against ``gpt-6-luna`` before this file was written:

* **``max_completion_tokens``, never ``max_tokens``.** Reasoning models answer
  ``max_tokens`` with a 400 ("Use 'max_completion_tokens' instead"). The older
  chat models accept the new spelling too, so it is sent unconditionally.
* **No unified ``reasoning`` object — OpenAI spells it ``reasoning_effort``.**
* **The effort rules differ by family, and each was measured with tools on:**

  ============================  ======  =======  ======
  model                          absent  "none"   "low"
  ============================  ======  =======  ======
  gpt-6-luna / -sol, gpt-5.6-*   400     OK       400
  o3, o4-mini                    OK      400      OK
  gpt-4.1                        OK      400      400
  gpt-6-astra                    400     400      400
  ============================  ======  =======  ======

  So the gpt-5.x/gpt-6 family gets ``none`` whenever tools ride the call (every
  executor turn), the o-series gets ``low`` and never ``none``, and anything else
  gets no field at all. ``gpt-6-astra`` cannot take function tools on this API at
  any effort; the 400 it returns is surfaced to the caller naming the model.
* **``stop`` is unsupported on reasoning models** and is dropped there.
* **``temperature`` is only accepted at effort ``none``** on reasoning models,
  so it is dropped whenever an effort above that is sent.
* **An exhausted balance is a 429**, which the parent reads as a rate limit and
  retries. OpenAI's own ``insufficient_quota`` is mapped to the credit error.

The family is decided from the model id because the caller can type any id and
OpenAI publishes no capability flag on its ``/models`` listing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, ClassVar

import httpx

from ..errors import DaintreeError, provider_insufficient_credits, upstream_protocol_error
from .base import ProviderChatRequest
from .openrouter import _QUOTA_MARKERS, OpenRouterProvider

DEFAULT_BASE_URL = "https://api.openai.com/v1"

#: OpenAI has no ``/key`` route; ``/models`` answers 200 for a usable key and 401
#: for a dead one.
_KEY_PROBE_PATH = "/models"

#: gpt-5.1 onward and gpt-6: an effort ladder that starts at ``none``, and tools
#: only at ``none`` on Chat Completions. The original gpt-5 has no ``none``.
_NONE_LADDER = re.compile(r"^gpt-(5\.\d|[6-9])")
#: The o-series and the original gpt-5: ``low`` upward, never ``none``.
_LOW_LADDER = re.compile(r"^(o[1-9]|gpt-5(-|$))")

_NO_REASONING = "none"
_LOW = "low"



def is_reasoning_model(model: str) -> bool:
    return bool(_NONE_LADDER.match(model) or _LOW_LADDER.match(model))


class OpenAIProvider(OpenRouterProvider):
    """The OpenAI-shape adapter, pointed at OpenAI itself."""

    default_base_url: ClassVar[str] = DEFAULT_BASE_URL
    key_probe_path: ClassVar[str] = _KEY_PROBE_PATH
    attribution_headers: ClassVar[Mapping[str, str]] = {}

    def _build_body(self, request: ProviderChatRequest, *, stream: bool) -> dict[str, Any]:
        body = super()._build_body(request, stream=stream)

        if "max_tokens" in body:
            body["max_completion_tokens"] = body.pop("max_tokens")
        body.pop("reasoning", None)

        # The parent rewrote `reasoning_content` into `reasoning` on assistant
        # rows. OpenAI reads neither, so both are dropped copy-on-write rather
        # than replayed as a field this host ignores.
        body["messages"] = [
            {k: v for k, v in m.items() if k not in ("reasoning", "reasoning_content")}
            if "reasoning" in m or "reasoning_content" in m
            else m
            for m in body["messages"]
        ]

        wanted = None if request.thinking_disabled else request.reasoning_effort
        if _NONE_LADDER.match(request.model):
            effort = _NO_REASONING if request.tools or not wanted else wanted
        elif _LOW_LADDER.match(request.model):
            effort = wanted if wanted and wanted != _NO_REASONING else _LOW
        else:
            return body
        body["reasoning_effort"] = effort
        body.pop("stop", None)
        if effort != _NO_REASONING:
            body.pop("temperature", None)
        return body

    def _map_http_error(
        self, status: int, headers: httpx.Headers, detail: str | None = None
    ) -> DaintreeError:
        if status == 429 and detail and any(m in detail.lower() for m in _QUOTA_MARKERS):
            return provider_insufficient_credits()
        return super()._map_http_error(status, headers, detail)

    def _decode_key_record(self, payload: Any) -> dict[str, Any]:
        """``/models`` answers ``{"object": "list", "data": [...]}``."""
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise upstream_protocol_error("The provider returned no model listing.")
        return {
            "models": [
                m["id"] for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)
            ]
        }
