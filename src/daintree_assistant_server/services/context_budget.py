"""Does the request we just assembled actually FIT?

Every other limit in this server bounds one input: 8 MiB of HTTP body, 1 MiB of
message text, 128 tools, 100 KiB of tool definitions, 16 KiB of workflow state.
None of them answers the only question the upstream will ask — whether the
complete serialized prompt, plus the output we reserved for it, fits the serving
model's context window. A request can satisfy every cap in `config.py` and still
be several times too large, because the caps bound the pieces the CLIENT sent
and the prompt also carries the base prompt, the response contract, ~21.5k
tokens of tool schemas, the active runbook bodies, the integrations, the startup
block and the injected tail — none of which the 8 MiB ceiling counts.

**This module measures; it rarely refuses.** The estimate is characters/4, and
against a window we have not verified on the live host (see
``EXECUTOR_CONTEXT_TOKENS``) that is not a number to fail a paid turn on. So the
posture is deliberately lopsided:

* every turn is measured and logged, which is what makes the constant
  calibratable — ``turn_usage`` carries the estimate beside the upstream's own
  ``prompt_tokens``, so a week of traffic says what the ratio really is;
* a turn is refused only when the estimate clears the window with no safety
  margin subtracted at all, i.e. when no plausible estimator error could make it
  fit. That is a rare, honest 413 instead of a wasted upstream call returning a
  generic ``upstream_request_rejected`` that reads like a backend bug.

What this module does NOT do is shed context to make an oversized turn fit. The
review that prompted it sketches a tiered scheme — protect the system layers,
the current instruction and unresolved tool transactions; shrink settled history
and low-ranked documentation first — and that is the right shape, but it is a
producer of prompts, not a check on one, and it belongs beside compaction rather
than in front of the provider call. ``TODO.md`` carries it. Until then an
oversized turn is named rather than silently truncated, which is the failure mode
we can explain to a caller.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from ..config import EXECUTOR_CONTEXT_TOKENS
from ..contracts.errors import CODE_CONTEXT_WINDOW_EXCEEDED, TYPE_INVALID_REQUEST
from ..errors import DaintreeError
from ..providers.base import ProviderChatRequest
from ..text_utils import estimate_tokens

# Stable code for "this prompt is too big", in both directions: raised here when
# we can see it ourselves, and mapped from the upstream's own rejection when we
# could not (see `providers/openrouter._map_status`). One code, so the CLI needs
# one branch and the operator needs one query — and one DEFINITION, in
# `contracts/errors.py`, because `providers/failover.py` classifies on the same
# literal and a second spelling of it would be invisible until it diverged.

# Chat templates add role markers, separators and a tool-schema preamble that no
# amount of measuring the payload can see, and tokenizers disagree with a
# characters/4 estimate in both directions. This is the slack we assume exists
# BEYOND the measured payload before calling a turn impossible.
_TEMPLATE_OVERHEAD_TOKENS = 512

# How far past the window an estimate must land before a turn is refused.
#
# The docstring above promises a refusal only where "no plausible estimator error
# could make it fit", and `input > window` did not keep that promise: one token
# over is exactly the case estimator error rescues. characters/4 runs LOW on the
# dense JSON, code and file paths this server mostly sends (real tokenizers give
# roughly 3 chars/token there), so the realistic false positive is the opposite —
# unusually compressible text, where 4 chars/token badly overstates. This margin
# is what buys that case room, and it costs nothing worth having: a turn between
# 1.0x and 1.25x of the window is one the upstream will answer or refuse on its
# own, and its refusal now maps to the same code (`_map_status`).
_REFUSAL_MARGIN = 1.25

# What one image costs, as a flat charge, INSTEAD of counting its bytes.
#
# A base64 data URL is not text and a vision model does not tokenize it as text:
# it becomes a fixed number of patch embeddings, largely independent of the file
# size. Counting the encoded string at characters/4 is therefore not imprecise,
# it is the wrong unit — a 6 MiB image (which `cap_image_encoded_bytes` allows)
# reads as 1.57 MILLION tokens against a 131k window, so the validator would
# accept an image the budget then refuses every single time.
#
# The number itself is a placeholder and is admitted as one: nothing in the
# product sends images yet (`multimodal_images.data_urls` is advertised, the CLI
# has no production caller), so there is nothing to calibrate against. It is
# sized as a generous single-image cost for a vision model. What matters is that
# it is a per-image constant rather than a function of bytes, so the two limits
# stop contradicting each other. `TODO.md` carries the calibration.
_IMAGE_TOKENS = 1_600

#: Matches a data URL's base64 payload so it can be replaced by a marker before
#: the text estimate sees it. Deliberately narrow: only `data:` URLs, only the
#: part after the comma.
_DATA_URL_PAYLOAD_RE = re.compile(r"(data:[^,\"\\]{0,128},)[A-Za-z0-9+/=%._~-]+", re.IGNORECASE)


@dataclass(frozen=True)
class ContextMeasurement:
    """A token estimate for one assembled request, broken down by layer.

    The layers are the assembler's own regions, in serialization order, because
    that is the only breakdown that answers the question a reader has when this
    number is bad: WHICH part grew. A single total says a turn is too big; the
    split says whether it was the tool schemas, a runbook, or the transcript.
    """

    system_tokens: int
    tool_tokens: int
    conversation_tokens: int
    reserved_output_tokens: int
    context_window: int

    @property
    def input_tokens(self) -> int:
        return self.system_tokens + self.tool_tokens + self.conversation_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.reserved_output_tokens

    @property
    def headroom_tokens(self) -> int:
        """Tokens left over. Negative when the reservation cannot be honoured."""
        return self.context_window - self.total_tokens

    @property
    def headroom_ratio(self) -> float:
        """Headroom as a fraction of the window — the number worth alerting on.

        Rounded to three places so a log sink can aggregate it without carrying
        float noise, and clamped nowhere: a negative ratio is the finding.
        """
        if self.context_window <= 0:
            return 0.0
        return round(self.headroom_tokens / self.context_window, 3)

    @property
    def dominant_layer(self) -> str:
        """Which region contributed most. The first thing to look at, named."""
        layers = {
            "tools": self.tool_tokens,
            "conversation": self.conversation_tokens,
            "system": self.system_tokens,
        }
        return max(layers, key=lambda name: layers[name])

    def log_fields(self) -> dict[str, Any]:
        """Structured fields for the preparation log. No prompt content — the
        whole point of a token count is that it describes a prompt without
        carrying one."""
        return {
            "context_estimated_input_tokens": self.input_tokens,
            "context_system_tokens": self.system_tokens,
            "context_tool_tokens": self.tool_tokens,
            "context_conversation_tokens": self.conversation_tokens,
            "context_reserved_output_tokens": self.reserved_output_tokens,
            "context_window": self.context_window,
            "context_headroom_tokens": self.headroom_tokens,
            "context_headroom_ratio": self.headroom_ratio,
            "context_dominant_layer": self.dominant_layer,
        }


def _count_images(value: Any) -> int:
    """How many image parts a wire message carries.

    Walks the structure rather than counting `data:` occurrences in the
    serialized form: text content could quote one, and a quoted URL is text the
    model reads as text.
    """
    content = value.get("content") if isinstance(value, dict) else None
    if not isinstance(content, list):
        return 0
    return sum(
        1 for part in content if isinstance(part, dict) and part.get("type") == "image_url"
    )


def _serialize(value: Any) -> str:
    """Render a wire object the way the estimate should read it.

    Serialized rather than walked field by field: tool-call arguments, image data
    URLs and `reasoning_content` all reach the model as part of the same JSON
    object, and a walker that forgets one silently understates exactly the turns
    this exists to catch. `default=str` keeps a non-serializable value from
    raising — a lost measurement must never be a lost turn.

    **`ensure_ascii=False` is load-bearing, not tidiness.** The default escapes
    every non-ASCII character to a six-character escape sequence — so a
    Japanese or Chinese conversation measured with the default reads about 5.2x
    its real size, and a wholly ordinary session in one of those languages would
    have been refused at roughly a fifth of the window. The characters the model
    sees are the characters to count.
    """
    rendered = json.dumps(value, default=str, ensure_ascii=False)
    # Image payloads are charged per image (`_IMAGE_TOKENS`), not per byte, so
    # their bytes must not also be counted as text here.
    return _DATA_URL_PAYLOAD_RE.sub(r"\1", rendered)


def _message_tokens(message: dict[str, Any]) -> int:
    """Estimate one wire message."""
    return estimate_tokens(_serialize(message))


def measure(
    request: ProviderChatRequest, *, context_window: int | None = None
) -> ContextMeasurement:
    """Estimate the assembled request against the serving model's window.

    Split by ROLE rather than by assembler layer, because by this point the
    layers have already been flattened into the wire array and role is what
    survives: the two static system blocks, the tool schemas, and everything
    else (the user-role runbook/integration/startup rows, the visible transcript
    and the injected tail), which move together and are the part a long session
    grows.
    """
    window = EXECUTOR_CONTEXT_TOKENS if context_window is None else context_window
    system = 0
    conversation = 0
    for message in request.messages:
        tokens = _message_tokens(message) + _count_images(message) * _IMAGE_TOKENS
        if message.get("role") == "system":
            system += tokens
        else:
            conversation += tokens

    tools = 0
    if request.tools:
        tools = estimate_tokens(_serialize(request.tools))

    return ContextMeasurement(
        system_tokens=system + _TEMPLATE_OVERHEAD_TOKENS,
        tool_tokens=tools,
        conversation_tokens=conversation,
        # What we TOLD the upstream to reserve. The assembler always fills it
        # (the client's value or `main_default_max_tokens`, clamped to the
        # ceiling), so on the serving path this is never the `0` fallback — that
        # is there for a hand-built request in a test or a benchmark.
        reserved_output_tokens=request.max_tokens or 0,
        context_window=window,
    )


def exceeds_window(measurement: ContextMeasurement) -> bool:
    """Whether this turn is too large to be worth sending.

    Two things are deliberately excluded from the test, and both are the
    difference between a useful check and one that fails paid turns.

    NOT ``headroom_tokens < 0``: the reservation is a ceiling on the reply, not a
    promise the model will use it, so counting it would refuse turns the upstream
    would have answered.

    NOT ``input_tokens > context_window`` either. A characters/4 estimate against
    an unverified window is wrong by tens of percent in both directions, and one
    token over is precisely the case that error rescues. ``_REFUSAL_MARGIN`` is
    how far past the window an estimate has to land before "no reading of this
    could fit" is actually true.
    """
    return measurement.input_tokens > measurement.context_window * _REFUSAL_MARGIN


def context_window_exceeded(measurement: ContextMeasurement) -> DaintreeError:
    """The typed refusal, carrying the dimensions that make it actionable.

    413 rather than 400: it is the same class of answer as ``request_too_large``
    — nothing about the request is malformed, there is simply too much of it —
    and a caller's fix is the same shape, send less. The numbers are in the
    message because there is nowhere else for them to go in the error envelope,
    and without them "too large" tells a caller nothing they can act on.
    """
    return DaintreeError(
        status_code=413,
        message=(
            "This turn does not fit the model's context window: an estimated "
            f"{measurement.input_tokens} input tokens against a {measurement.context_window}-token "
            f"window (largest layer: {measurement.dominant_layer}). "
            "Start a new session or shorten the conversation."
        ),
        type=TYPE_INVALID_REQUEST,
        code=CODE_CONTEXT_WINDOW_EXCEEDED,
        param="input.messages",
    )
