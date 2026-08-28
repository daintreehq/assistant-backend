"""Baseten Model APIs — the serving host for GLM-5.3-Flash.

Baseten speaks the same OpenAI wire as OpenRouter for nearly everything this
server sends, so this adapter is the OpenRouter one with four things changed:
where it lives, how a key is probed, how the thinking posture is spelled, and
what it does NOT report. Every claim below was measured against the live API
before this file was written.

**The thinking posture INVERTED here, and it is the fact most likely to be
misremembered.** Every other adapter in this package exists in a world where
non-thinking is reachable: the OpenRouter/Together parent sends
``reasoning: {"enabled": false}`` to hold a reasoning model down, and the
Cerebras adapter omits the field entirely because its model does not reason at
all. GLM-5.3-Flash admits neither. It reasons unconditionally — Baseten lists it
"always enabled", ``none`` is not an accepted effort, and the only control is
``reasoning_effort`` over ``low`` | ``high`` | ``max``, defaulting to ``high``
when the field is absent. So OMISSION IS EXPENSIVE here, in the same way it is
for the parent but with no floor of zero underneath it. Measured over 6 trials
on one proof-and-verify prompt: sending nothing reasoned on 6 of 6, median 479.5
reasoning tokens, spread 161–2519. The same prompt with
``reasoning_effort: "low"`` reasoned on 1 of 6, median 0, the single non-zero
trial costing 16 tokens. ``low`` is the floor this model exposes — it is a floor
and not an off switch, which is why the number is "almost always nothing" rather
than "nothing". `config.EXECUTOR_REASONING_EFFORT` pins it there.

**The unified ``reasoning`` object is NEVER sent, and the reason is subtle
enough to be worth writing down.** Sending the parent's ``{"enabled": false}``
to this host does not error, and it MEASURES much like ``low`` did — 2 of 6
trials, median 0. That is not evidence that it works. Baseten accepts unknown
top-level body fields silently (measured: a made-up ``daintree_nonsense`` field
answers 200), and an ``enabled`` value that is not even a boolean answers 200
too, so a field this host does not document cannot be distinguished from one it
is quietly discarding — and the arms are close enough that the measurement
cannot separate them either. Holding the single most expensive posture decision
in the server on a coincidence that survived six trials is the trade being
refused here. ``reasoning_effort`` is the documented, supported spelling, and it
is the one this adapter sends.

**``reasoning_content`` is this host's OWN spelling, inbound and outbound.**
Both the non-streaming ``message`` and the streaming ``delta`` carry
``reasoning_content`` (measured), which is already the name everything above the
provider boundary speaks. So the parent's two rename shims are the wrong shape
here in opposite ways: ``_normalize_choices`` (inbound ``reasoning`` →
``reasoning_content``) is inherited unchanged because it is simply a no-op on a
payload that never carries ``reasoning``, while the OUTBOUND rewrite is undone —
replaying an assistant turn's chain-of-thought under the parent's ``reasoning``
would hand this host a field it does not use and silently drop the context. Both
spellings are accepted with a 200 (measured), so this is a correctness choice
rather than an error being avoided: the field is sent under the name the host
actually reads.

**It does not report cost.** ``usage`` carries no ``cost`` on either the
streaming or the non-streaming path (measured), which is the one capability lost
in the move off Together. ``ProviderUsage.from_wire`` was built to survive that —
``cost`` stays ``None`` rather than becoming a fabricated ``0.0``, and the
accounting chain carries the unknown through — so a turn is still attributed,
just not priced. Per-token pricing IS published in the ``/models`` listing
(``prompt``, ``completion``, ``input_cache_read``), so computing it here is
possible and deliberately not done: that would be this server inventing a number
the host declined to state.

**No key-probe endpoint.** Baseten has no ``/key`` route, so ``verify_key``
probes ``/models`` — a 200 there means the credential is real, which is the same
verdict ``/key`` gives.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from ..errors import upstream_protocol_error
from .base import ProviderChatRequest
from .openrouter import OpenRouterProvider

DEFAULT_BASE_URL = "https://inference.baseten.co/v1"

#: Baseten has no ``/key`` route. ``/models`` answers 200 for a usable key and
#: 401 for a dead one, which is the only distinction ``verify_key`` draws.
_KEY_PROBE_PATH = "/models"

#: The floor ``reasoning_effort`` this model exposes. There is no "off" — see
#: the module docstring.
_MIN_EFFORT = "low"


class BasetenProvider(OpenRouterProvider):
    """The OpenAI-shape adapter, pointed at Baseten's Model APIs.

    Everything that makes the parent work — SSE parsing, tool-call
    accumulation, error mapping, retries, redaction, the dev-only request dump,
    and the ``max_tokens`` spelling — is inherited unchanged. Only the thinking
    posture and the key-probe shape genuinely differ on the wire.
    """

    default_base_url: ClassVar[str] = DEFAULT_BASE_URL
    key_probe_path: ClassVar[str] = _KEY_PROBE_PATH
    #: Baseten runs no public app ranking, so the parent's attribution headers
    #: would be bytes on every request buying nothing. Sent empty rather than
    #: inherited.
    attribution_headers: ClassVar[Mapping[str, str]] = {}

    def __init__(self, *args: Any, reasoning_effort: str = _MIN_EFFORT, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Injected rather than read from `config` so the product decision stays
        # beside the model id (`config.EXECUTOR_REASONING_EFFORT`) and this
        # module keeps its one job — the same split the Cerebras adapter uses for
        # `send_prompt_cache_key`. The default is the floor, so a provider built
        # without one is built cheap.
        self._reasoning_effort = reasoning_effort

    def _build_body(self, request: ProviderChatRequest, *, stream: bool) -> dict[str, Any]:
        body = super()._build_body(request, stream=stream)

        # The parent always writes a unified `reasoning` object — the field that
        # holds DeepSeek down. Baseten does not document it, accepts it silently
        # like any unknown field, and serves a model that cannot be held down at
        # all. Dropped unconditionally, then replaced with the ONE control this
        # host exposes. See the module docstring on why its apparent effect is
        # not evidence.
        body.pop("reasoning", None)

        # `low` is the floor, not an "off": this model has no `none`. An absent
        # field means `high`, so there is no posture in which omitting this is
        # the cheap choice — which is the exact inverse of the Cerebras adapter,
        # where omission IS the non-thinking posture.
        #
        # `thinking_disabled` is honoured as a request for the CONFIGURED FLOOR
        # rather than ignored, because the callers that set it (the
        # forced-tool-call path in `structured_tool_call`, the assembler's
        # non-thinking pins) are asking for the cheapest, least discursive call
        # this host can serve. Dropping the field there — the literal reading of
        # "no thinking" — would send them at `high`, making every forced
        # single-object call the most expensive shape available.
        if request.thinking_disabled or not request.reasoning_effort:
            body["reasoning_effort"] = self._reasoning_effort
        else:
            body["reasoning_effort"] = request.reasoning_effort

        # The parent rewrote message-level `reasoning_content` into `reasoning`
        # on every assistant row (`openrouter._rewrite_messages`). This host
        # spells it `reasoning_content` in its own responses, so the rewrite is
        # undone rather than the field dropped — the chain-of-thought that
        # produced a tool-call turn is replayed under the name the host reads.
        #
        # COPY-ON-WRITE, matching the parent, and not merely for symmetry. The
        # parent passes rows it did not rewrite through BY REFERENCE, so a row
        # arriving with a raw `reasoning` key is the caller's own dict — popping
        # in place would mutate `request.messages`, which every retry, the
        # bare-intent re-roll and both response paths go on to read. Today's
        # assembler only ever emits `reasoning_content` (it builds each wire row
        # key by key), so no such row is reachable in production; this keeps
        # `_build_body` a pure function of its input anyway, because that is the
        # property the rest of the pipeline is entitled to assume.
        # `body["messages"]` is always the list `_rewrite_messages` just returned,
        # so it is indexed rather than defensively type-checked — a guard here
        # would be an unreachable branch pretending to be care.
        # Every row here is a mapping: the parent's `_rewrite_messages` calls
        # `.get()` on each one before this runs, so a non-dict row has already
        # raised. Re-checking would be an unreachable branch pretending to be care.
        messages = body["messages"]
        for index, message in enumerate(messages):
            if "reasoning" not in message:
                continue
            rebuilt = {k: v for k, v in message.items() if k != "reasoning"}
            # An existing `reasoning_content` WINS. A row carrying both is
            # malformed input rather than something the parent produces, and
            # overwriting the canonical field with the other one would resolve the
            # contradiction in favour of the spelling we were trying to leave.
            if rebuilt.get("reasoning_content") is None:
                rebuilt["reasoning_content"] = message["reasoning"]
            messages[index] = rebuilt

        return body

    def _decode_key_record(self, payload: Any) -> dict[str, Any]:
        """``/models`` answers OpenAI's ``{"object": "list", "data": [...]}``.

        A LIST under ``data`` — which is neither OpenRouter's ``{"data": {...}}``
        mapping nor the bare top-level array the retired Together adapter had to
        decode. The parent's decoder requires the mapping and would raise
        ``upstream_protocol_error`` on every VALID Baseten key, reporting "we
        could not tell" about a credential that had just proved itself, which is
        the one answer this endpoint must never give when it does in fact know.

        The record is shaped as ``{"models": [...]}`` so a caller reading the
        diagnostic does not have to know which host answered.
        """
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise upstream_protocol_error("The provider returned no model listing.")
        return {
            "models": [
                m["id"] for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)
            ]
        }

