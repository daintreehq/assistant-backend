"""Cerebras — the FAST-RESPONSE host, and the second provider role.

**This is a stage, not a product tier.** A Cerebras/Gemma tier lived here once,
with a `modes.py` registry, per-mode credentials and per-turn route threading, and
it was deleted when its pricing could not be made to work (CLAUDE.md). None of
that comes back. A request still targets the single public model
`daintree-assistant`; the backend composes two internal calls, and no caller can
select between them. The kill switch below is operational, not a menu.

**Three wire differences from the parent, all of them measured or documented:**

* **``max_completion_tokens``, not ``max_tokens``.** The exact inverse of
  `openrouter.py`, which spells the same normalized field the OpenAI-standard way.
  Each adapter resolves it to its own single name; sending both is the failure
  mode this rule exists to prevent.
* **No ``reasoning`` object, ever — Cerebras spells it ``reasoning_effort``.**
  The parent MUST send `{"enabled": false}` because DeepSeek V4 Flash reasons by
  default and omission means "think". Cerebras does not speak that object at all,
  so it is dropped unconditionally and `reasoning_effort` is sent only for an
  EXPLICIT effort a caller asked for — never synthesised from `thinking_disabled`,
  which would put a parameter on the wire to buy a disable that does not exist.
  This matters more than it used to: the model this adapter reaches is now
  `gpt-oss-120b`, which DOES reason and which Cerebras defaults to `medium`, so
  omitting the field is not neutral. `services/fast_response.py` pins it at `low`
  and pairs it with `thinking_disabled=False`, because that flag is what gates the
  branch below.
* **``prompt_cache_key``, and only when enabled.** Cerebras routes requests
  sharing a key to the same engine to reuse its KV cache. It is off by default:
  it is account-gated, and an unadvertised parameter is a needless 400 risk on a
  path whose entire value is being fast.

**The live wire is UNVERIFIED.** `tests/live/` does not exist and CI bars paid
calls, so what Cerebras accepts, what it 400s and whether it reports `cost` are
pinned by nothing here. What IS pinned offline is the body this adapter builds
(`tests/unit/test_provider_cerebras.py`) — the half that is ours to get wrong.
`TODO.md` carries the contract suite, alongside the same gap for Together.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from ..errors import upstream_protocol_error
from .base import ProviderChatRequest
from .openrouter import OpenRouterProvider

DEFAULT_BASE_URL = "https://api.cerebras.ai/v1"

#: Cerebras has no `/key` route. `/models` answers 200 for a usable key and 401
#: for a dead one, which is the only distinction `verify_key` draws — the same
#: reasoning, and the same endpoint, as the Together adapter.
_KEY_PROBE_PATH = "/models"


class CerebrasProvider(OpenRouterProvider):
    """The OpenAI-shape adapter, pointed at Cerebras, with its own body rules."""

    default_base_url: ClassVar[str] = DEFAULT_BASE_URL
    key_probe_path: ClassVar[str] = _KEY_PROBE_PATH
    #: Cerebras runs no public app ranking, so the parent's attribution headers
    #: would be bytes on every request buying nothing.
    attribution_headers: ClassVar[Mapping[str, str]] = {}

    def __init__(
        self,
        *args: Any,
        send_prompt_cache_key: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._send_prompt_cache_key = send_prompt_cache_key

    def _build_body(self, request: ProviderChatRequest, *, stream: bool) -> dict[str, Any]:
        body = super()._build_body(request, stream=stream)

        # One spelling per adapter. The parent set `max_tokens`; replace it rather
        # than adding, so a body can never carry both.
        if "max_tokens" in body:
            body["max_completion_tokens"] = body.pop("max_tokens")

        # The parent always writes a `reasoning` object. Cerebras does not speak
        # it, and for a non-reasoning model there is nothing to disable — see the
        # module docstring. Dropped unconditionally, then re-added only for an
        # EXPLICIT effort a caller asked for.
        body.pop("reasoning", None)
        if not request.thinking_disabled and request.reasoning_effort:
            body["reasoning_effort"] = request.reasoning_effort

        # The parent also rewrites message-level `reasoning_content` into
        # `reasoning` on every assistant row (`openrouter._rewrite_messages`), so
        # dropping the top-level field alone is not enough. Today's fast prompt
        # sends role/content only and never hits this — but an adapter that is
        # correct only because of what one caller happens to send is not correct.
        for message in body.get("messages", []):
            if isinstance(message, dict) and "reasoning" in message:
                message.pop("reasoning", None)

        # Off unless the account has it. Derived by the CALLER from the stable
        # prompt prefix, never a raw session id: the point is to group requests
        # that share a prefix, and a per-session key would defeat exactly that.
        if self._send_prompt_cache_key and request.cache_session_id:
            body["prompt_cache_key"] = request.cache_session_id

        return body

    def _decode_key_record(self, payload: Any) -> dict[str, Any]:
        """``/models`` answers OpenAI's ``{"object": "list", "data": [...]}``.

        The parent's decoder wants OpenRouter's ``{"data": {...}}`` mapping and
        would raise ``upstream_protocol_error`` on a perfectly good Cerebras key —
        reporting "we could not tell" about a credential that had just proved
        itself, which is the one answer a verification endpoint must never give
        when it does in fact know.
        """
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise upstream_protocol_error("The provider returned no model listing.")
        return {
            "models": [
                m["id"] for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)
            ]
        }
