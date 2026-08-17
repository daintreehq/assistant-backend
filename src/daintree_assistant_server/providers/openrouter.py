"""The OpenRouter provider adapter (OpenAI-compatible wire format).

Translates the normalized :class:`ProviderChatRequest` to OpenRouter's
``/chat/completions`` API and maps upstream failures to Daintree errors. A
single process-wide ``httpx.AsyncClient`` is injected; a client is never built
per request.

OpenRouter is a router, not a model host: it fronts ~24 independent endpoints
serving the same DeepSeek V4 Flash weights, and the ``provider`` block on every
request is what decides which one answers (see ``_PROVIDER_ROUTING``). Three
OpenRouter-specific details separate this from a direct-to-DeepSeek adapter:

* **Reasoning control** is OpenRouter's unified ``reasoning`` object
  (``{"enabled": false}`` / ``{"effort": "high"}``), NOT DeepSeek's native
  ``thinking`` object — OpenRouter translates it to whatever the winning endpoint
  speaks. It is sent explicitly on every call because a reasoning model's default
  is ON, and the explicit disable is what keeps the non-thinking selector
  non-thinking. ``send_reasoning=False`` drops it for a model that has no
  reasoning mode at all.
* **Chain-of-thought is called ``reasoning``** on the OpenRouter wire, while the
  Daintree contract (and the CLI) speak ``reasoning_content``. The rename is
  confined to this adapter — outbound in ``_rewrite_messages``, inbound in
  ``_normalize_choices`` — so nothing above the provider boundary knows.
* **No ``prompt_cache_key``**: no endpoint for this model advertises it, and with
  ``require_parameters`` on, sending an unadvertised parameter can empty the
  eligible-endpoint pool. Prefix caching is implicit at whichever endpoint serves
  the request.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import os
import time
from typing import Any

import httpx
from pydantic import SecretStr

from ..errors import (
    DaintreeError,
    invalid_api_key,
    provider_insufficient_credits,
    provider_invalid_api_key,
    provider_key_forbidden,
    upstream_error,
    upstream_no_compliant_provider,
    upstream_protocol_error,
    upstream_rate_limited,
    upstream_request_rejected,
    upstream_timeout,
    upstream_unavailable,
)
from ..observability import get_logger
from .base import (
    ProviderChatRequest,
    ProviderChatResult,
    ProviderStream,
    ProviderToolCallResult,
    ProviderUsage,
)
from .policy import DEFAULT_POLICY, RoutingPreference
from .sse import (
    ChunkShapeError,
    SseProtocolError,
    iter_sse_data,
    iter_sse_lines,
    parse_chunk,
    validate_message_shape,
)

logger = get_logger("daintree.openrouter")

_CHAT_PATH = "/chat/completions"

# Cap on how much of an upstream error body we log / surface — enough to carry the
# provider's reason ("temperature must be between 0.0 and 2.0", a schema complaint)
# without dumping an unbounded payload into logs or the client error message.
_ERROR_DETAIL_CAP = 400

# Stands in for a credential the upstream echoed back inside an error body, so the
# key never reaches a log line or a client error envelope (see observability.py:
# logs carry no bearer keys).
_REDACTED = "[REDACTED]"

# Replaces the diagnostic entirely when the credential is still recoverable from
# it after scrubbing — a doubly encoded echo. Deliberately says nothing about the
# key beyond the fact that the upstream sent it back.
_CREDENTIAL_ECHOED = "Upstream rejected the credential and echoed it back; detail withheld."

# How many escape-decoding passes the leak check looks through. Two covers the
# doubly encoded case; the bound just stops a crafted body from spinning here.
_ESCAPE_DECODE_PASSES = 3

# A credential must be printable ASCII with no spaces to be a legal header value.
# An allowlist rather than a blocklist of the bad characters: blocklisting misses
# DEL (0x7f, which h11 happily accepts) and everything above 0x7f, which instead
# explodes inside httpx's header encoder (it encodes str values as ASCII). Real
# OpenRouter keys are `sk-or-v1-` plus hex, so nothing legitimate is near this
# boundary. Keeping the credential ASCII is also what makes the escape-decoding
# leak check in `_still_reveals` sound.
_HEADER_SAFE_MIN = 0x21
_HEADER_SAFE_MAX = 0x7E

# Cap on the logged serving-endpoint name ("CoreWeave", "Baidu", …).
_ENDPOINT_NAME_CAP = 64

# Length of the routing-cohort suffix on an affinity key (see `_cohort_scoped`).
# It separates cohorts, it does not authenticate one: a collision costs two
# cohorts a shared sticky record, which is the pre-existing behaviour, not a
# correctness failure. Short enough to keep the key legible upstream.
_COHORT_DIGEST_CHARS = 8

# Attribution headers. OpenRouter uses them for its public model/app rankings;
# they carry no auth weight and are safe to send on every call.
_REFERER = "https://daintree.dev"
_APP_TITLE = "Daintree Assistant"

# Read budget for the key-verification probe. Hardcoded, not a setting: it is a
# single tokenless introspection GET behind a sign-in spinner, so it has nothing in
# common with the main model's budget and there is no deployment that wants it long.
_VERIFY_TIMEOUT_SECONDS = 10.0

# THE endpoint-selection policy, sent on EVERY request. One policy, deliberately:
# the adapter does not inspect a request to decide how to route it. The policy
# OBJECT lives in providers/policy.py (RoutingPolicy / PrivacyMode); this
# module-level dict is its rendered wire form, kept here because the surrounding
# adapter and its tests read it directly.
#
# The shipped defaults are hardcoded like the model ids and the thinking posture
# (config.py): there is one optimum and the server ships it. A caller may narrow
# or re-rank it through a validated RoutingPreference, but can never disable the
# no-training floor. Three product decisions are encoded:
#
# FASTEST, ON BOTH AXES. A request's wall-clock is time-to-first-token plus
# tokens/throughput, and OpenRouter ranks on either but not on the sum. So rank on
# one and put a floor under the other: ``sort: "throughput"`` takes the fastest
# generator (explicitly at the cost of price, and it disables OpenRouter's default
# price-weighted load balancing), while ``preferred_max_latency`` stops that being
# an endpoint which streams fast and takes seconds to say the first word. Ranking
# on throughput ALONE is what elected the measured p99 12.6s and 13.9s endpoints.
#
# This pair replaced a set of per-call-shape policies, and the generality is the
# point: a rule that needs to know whether a request is a classifier, a chat turn
# or a greeting is a rule that will misjudge the next call shape someone adds.
# Bounding the axis you did not sort on needs no such knowledge and is safe
# everywhere, because the bound deprioritises rather than filters (see
# LATENCY_BOUND) and therefore can never empty the pool. Selection stays automatic
# and per-request, so a newly-faster endpoint is picked up with no code change.
#
# PRIVACY FLOOR. ``data_collection: "deny"`` restricts routing to endpoints that
# do not COLLECT prompt data or train on it — the pool is filtered FIRST, then
# sorted, so throughput never buys its speed with user prompts.
#
# BE PRECISE ABOUT WHAT THIS PROMISES, because it is a user trust decision and
# Daintree carries source code, terminal output and project instructions.
# ``data_collection: "deny"`` is a NO-TRAINING / no-collection guarantee. It is
# NOT a no-retention one: an endpoint may still hold a request transiently for
# abuse monitoring. Zero data retention is OpenRouter's SEPARATE ``"zdr": True``
# flag (PrivacyMode.ZDR), which narrows the pool much further and is not sent by
# default. Every user-facing statement must say "does not collect or train on",
# never "does not store" (docs/PROVIDER_POLICY.md is the authority, and
# tests/unit/test_docs_drift.py fails CI if the copy and this policy disagree).
#
# ``require_parameters`` drops endpoints that would silently ignore something we
# sent. It matters most for the json_object calls (selector, utility tasks):
# several otherwise-fast endpoints for this model do not implement
# ``response_format``, and without this they would answer with unconstrained prose
# that fails our parse. Per-request, so a plain chat turn still reaches them.
#
# AND IT IS THE LARGEST REMAINING SPEED COST, which is worth writing down where
# the flag lives rather than in a document nobody opens. For this model the
# endpoints it drops from a json_object call include the FASTEST one — CoreWeave,
# measured p50 361ms TTFT at 101 tok/s, and the endpoint a plain chat turn
# actually lands on. Pinning it for those calls is not available either:
# CoreWeave plus ``response_format`` is a hard ``404 No endpoints found``. So every
# JSON call in this system is routed within a smaller, slower pool, and no amount
# of sorting or bounding inside that pool recovers what was removed before the
# sort ran. Getting it back means not sending ``response_format`` at all and
# parsing tolerantly instead — a real change with a real risk, not a tuning knob.
# (Measured 2026-07; see docs/PROVIDER_POLICY.md section 8 on dated observations.)
#
# ``allow_fallbacks`` keeps the next-fastest compliant endpoint available when the
# winner is down. With filters this strict, turning it off would make any single
# outage a hard 404/503 ("No allowed providers are available").
_PROVIDER_ROUTING: dict[str, Any] = DEFAULT_POLICY.to_wire()

# The same policy with its ranking axis flipped, for a call that declares
# ``latency_critical``. Everything else — pool, filters, privacy floor, bound — is
# identical; only ``sort`` differs, and only because this call's shape is known at
# the call site rather than guessed from a request.
#
# Today that is the skill selector and nothing else: ~10.3k prompt tokens for ~90
# of answer (438 trials, `benchmarks/skill_selection`), sitting on the turn's
# first-byte path, and with a winning speculation the turn's whole
# time-to-first-token IS this call's latency.
#
# Generation is not negligible even here — ~90 tokens at ~100 tok/s is ~0.9s of a
# 2.044s p50, call it 45% — so this is a lean, not a landslide. What tips it is
# that the other ~55% is the half that MOVES: prefill over 10.3k tokens plus
# queueing, which scales with the catalog and which an affinity key can drive
# toward zero. The same run measured p99 9.878s against that p50, and a spread
# like that is a slow first byte, not a slow generator.
_PROVIDER_ROUTING_FIRST_BYTE: dict[str, Any] = DEFAULT_POLICY.ranked_on_first_byte().to_wire()


def _cohort_scoped(
    cache_session_id: str, provider_block: dict[str, Any], routing: RoutingPreference | None
) -> str:
    """Scope an affinity key to the pool of endpoints the call can actually reach.

    A sticky key names an endpoint, but only endpoints that pass this request's
    FILTERS are reachable. Two calls that share a key while routing under
    different filters therefore fight over one record: the stricter call cannot
    use the endpoint the looser one pinned, so it re-elects, and the looser call
    then inherits a pin it never asked for. Under a **shared server credential**
    that is no longer one caller's problem — sticky records are scoped to the API
    key, so one caller's ``zdr`` or ``only`` would be re-pointing every other
    caller's selector traffic. Folding the emitted pool into the key gives each
    routing cohort its own record instead.

    The suffix is derived from the ``provider`` block THIS request sends, not from
    a re-derivation of the policy, so it cannot drift from what was actually
    routed. Sorted keys: the block's key ORDER is deliberately fixed for prompt-
    cache reasons, but two blocks with the same constraints in any order are the
    same cohort, and the digest should say so.

    The default policy — overwhelmingly the common path — gets **no suffix at
    all**, so the shared selector and task keys stay short, readable in an upstream
    dashboard, and stable across a deploy that never touches routing.
    """
    if routing is None:
        return cache_session_id
    canonical = json.dumps(provider_block, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_COHORT_DIGEST_CHARS]
    return f"{cache_session_id}-{digest}"


def _extract_upstream_error(
    body: bytes | str | None, *, secret: SecretStr | None = None
) -> str | None:
    """Pull a short human-readable reason out of an upstream error body.

    OpenRouter returns ``{"error": {"message": "...", ...}}`` on a 4xx/5xx. That
    message is the single most useful diagnostic for a malformed request, so we
    surface it rather than discard it. Best-effort: any parse failure falls back to
    the raw (capped) text, and an empty body yields ``None``.

    ``secret`` is the credential this call used, scrubbed from the text if the
    upstream echoed it back. That reflection is the one way a caller's key could
    escape: this return value goes BOTH to the structured log and — for a non-auth
    4xx — into the client-facing error envelope via :meth:`_map_http_error`.

    Order matters twice, and both orderings are deliberate:

    * Scrub AFTER extraction, not on the raw body. JSON can encode an ordinary
      key as ``\\u0073\\u006b…``, which no literal match on the undecoded bytes
      would find but ``json.loads`` faithfully turns back into the plaintext.
      ``_upstream_error_detail`` therefore hands back decoded text on every
      branch. (It also keeps the parse honest: replacing inside the raw document
      could make it invalid JSON and silently demote a structured error to raw
      text.)
    * Scrub BEFORE capping. Capping first can slice a key in half and leave a
      usable prefix behind.

    A literal scrub still cannot promise to catch *every* encoding — a doubly
    encoded body hides the key from one decode pass. So the scrub is backed by a
    check rather than trusted: if the credential is still recoverable from the
    result, the diagnostic is dropped wholesale. Losing a rare error message is
    the right trade against leaking a key.
    """
    if not body:
        return None
    raw = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    raw = raw.strip()
    if not raw:
        return None
    detail = _upstream_error_detail(raw)
    if secret is not None:
        plain = secret.get_secret_value()
        # An empty secret must not be "replaced": str.replace("") splices the
        # placeholder between every character and destroys the diagnostic.
        if plain:
            detail = detail.replace(plain, _REDACTED)
            if _still_reveals(detail, plain):
                return _CREDENTIAL_ECHOED
    return detail[:_ERROR_DETAIL_CAP]


def _upstream_error_detail(raw: str) -> str:
    """The best available reason in ``raw``, uncapped and unredacted.

    Split out so redaction and capping apply once, to whichever branch won. Every
    branch returns DECODED text: the fallbacks re-serialize the parsed document
    rather than echoing ``raw``, so a ``\\uXXXX``-escaped credential becomes the
    literal characters an exact match can find. Only a body that is not JSON at
    all is returned verbatim — there, escapes denote nothing and nothing decodes
    them.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        msg = err.get("message") or err.get("code") or err.get("type")
        if msg:
            return str(msg)
    elif isinstance(err, str) and err:
        return err
    # No single reason to quote: hand back the whole document, decoded.
    return json.dumps(data, ensure_ascii=False)


def _still_reveals(text: str, plain: str) -> bool:
    """Whether ``plain`` is recoverable from ``text`` by decoding escapes.

    The backstop for a doubly encoded echo, where one decode pass leaves the key
    hidden from an exact match but a second restores it. Bounded passes, and the
    credential is guaranteed ASCII by :meth:`OpenRouterProvider._resolve_key`, so
    the latin-1-based ``unicode_escape`` view is sound for this comparison.
    """
    if plain in text:
        return True
    probe = text
    for _ in range(_ESCAPE_DECODE_PASSES):
        try:
            decoded = probe.encode("latin-1", "backslashreplace").decode("unicode_escape")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return False
        if decoded == probe:
            return False
        if plain in decoded:
            return True
        probe = decoded
    return False


def _endpoint_name(payload: dict[str, Any]) -> str | None:
    """The serving endpoint OpenRouter names on a response/chunk (``provider``).

    Best-effort and metadata-only: a non-string value is discarded rather than
    coerced, since this is only ever logged. Capped because it is upstream-supplied
    text landing in our logs.
    """
    name = payload.get("provider")
    return name[:_ENDPOINT_NAME_CAP] if isinstance(name, str) and name else None


def _rewrite_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rename outbound ``reasoning_content`` to OpenRouter's ``reasoning``.

    The assembler replays an assistant tool-call turn's chain-of-thought so the
    model can continue the reasoning that produced those calls. OpenRouter's
    normalized name for it is ``reasoning`` (it re-encodes it into whatever the
    winning endpoint speaks); an unrecognized ``reasoning_content`` would simply be
    dropped, silently losing the context rather than erroring. Copy-on-write: only
    the messages that actually carry the field are rebuilt, so the byte-stable
    prefix the prompt cache depends on is untouched everywhere else.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        cot = msg.get("reasoning_content")
        if cot is None:
            out.append(msg)
            continue
        rewritten = {k: v for k, v in msg.items() if k != "reasoning_content"}
        rewritten["reasoning"] = cot
        out.append(rewritten)
    return out


def _normalize_choices(choices: Any, key: str) -> None:
    """Rename inbound ``reasoning`` back to ``reasoning_content``, in place.

    ``key`` is ``"message"`` (non-streaming) or ``"delta"`` (streaming). Everything
    above the provider boundary — the orchestrator, the SSE contract, the CLI —
    speaks ``reasoning_content``, so the OpenRouter spelling stops here. A
    non-string value is dropped rather than renamed: the shape validators
    guarantee ``reasoning_content`` is a string when present, and this runs
    outside their reach.
    """
    if not isinstance(choices, list):
        return
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        part = choice.get(key)
        if not isinstance(part, dict) or "reasoning" not in part:
            continue
        value = part.pop("reasoning")
        # reasoning_details (the structured/encrypted variant) is deliberately not
        # consumed: the plaintext field is the documented equivalent for replay,
        # and it is the one the Daintree contract already carries.
        if isinstance(value, str) and part.get("reasoning_content") is None:
            part["reasoning_content"] = value


# Markers OpenRouter uses when routing preferences matched no endpoint. It
# answers this with a 404 *or* a 503 depending on which filter emptied the pool
# ("No endpoints found matching your data policy", "No allowed providers are
# available for the selected model", "There is no available model provider that
# meets your routing requirements"), so on the 503 branch the message is the only
# thing separating a policy dead-end from a real outage.
#
# Matched conservatively and biased toward the outage reading: mistaking an
# outage for a policy problem sends the operator to inspect a routing policy that
# is fine, and — worse — marks a transient failure permanent, killing a retry
# that would have succeeded.
# Each entry is a set of fragments that must ALL appear. Requiring a combination
# rather than any single phrase is deliberate: "no endpoints found" on its own
# also occurs in messages like "No endpoints found because the service is
# temporarily unavailable", and classifying that as a policy dead-end would mark
# a transient failure permanent and kill a retry that would have succeeded.
_UNROUTABLE_MARKER_SETS = (
    ("no endpoints found", "data policy"),
    ("no endpoints found", "routing"),
    ("no allowed providers",),
    ("no available model provider",),
    ("meets your routing requirements",),
)


def _looks_unroutable(detail: str | None) -> bool:
    """Whether an upstream message says the routing policy matched no endpoint.

    Biased toward reading an ambiguous message as an OUTAGE, not a policy
    dead-end. Getting it wrong in that direction costs one wasted retry; getting
    it wrong the other way marks a transient failure permanent and sends an
    operator to inspect a routing policy that is fine.
    """
    if not detail:
        return False
    blob = detail.lower()
    return any(
        all(fragment in blob for fragment in fragments)
        for fragments in _UNROUTABLE_MARKER_SETS
    )


def _looks_rate_limited(err: dict[str, Any]) -> bool:
    """Whether an in-band stream error is a rate limit.

    Deliberately does NOT match "quota". A quota message is far more often an
    exhausted *balance* — a permanent, caller-fixable condition — than a rate
    limit, and calling it a rate limit makes it retryable, so the turn replays a
    doomed request and reports the wrong fix. Insufficient credit is matched
    explicitly in ``_stream_error_status`` instead.
    """
    blob = " ".join(str(err.get(k, "")) for k in ("code", "type", "message")).lower()
    return any(
        token in blob
        for token in ("rate limit", "rate_limit", "429", "overloaded", "too many requests")
    )


def _stream_error_status(err: dict[str, Any]) -> int | None:
    """The HTTP-ish status an in-band stream error is reporting, if it says.

    OpenRouter puts a numeric ``code`` on most in-band errors. When it is there,
    it is a far better signal than prose — so the streaming path can reuse the
    exact same taxonomy as the HTTP path rather than inventing a second, weaker
    one.
    """
    code = err.get("code")
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code if 100 <= code <= 599 else None
    if isinstance(code, str):
        stripped = code.strip()
        if stripped.isdigit() and len(stripped) == 3:
            value = int(stripped)
            return value if 100 <= value <= 599 else None
    return None


# Phrases that identify a spent balance in an in-band error, where there is no
# HTTP status to read. Kept narrow and specific.
_INSUFFICIENT_CREDIT_MARKERS = (
    "insufficient credit",
    "insufficient funds",
    "insufficient balance",
    "not enough credit",
    "payment required",
    "add more credits",
)


def map_stream_error(err: dict[str, Any]) -> DaintreeError:
    """Map an in-band SSE error onto the SAME taxonomy as an HTTP failure.

    Without this, the streaming path had its own two-outcome world — rate limited
    or generic ``upstream_error`` — which recreated the exact bug the taxonomy
    exists to fix: an invalid key, a spent balance and a routing dead-end all
    arrived as one retryable-looking code, and the CLI replayed each of them.

    Most turns are streamed, so this is the path that matters most.
    """
    status = _stream_error_status(err)
    if status is not None:
        # Reuse the HTTP mapping wholesale. No headers are available in-band, so
        # a rate limit here simply carries no Retry-After.
        detail = str(err.get("message") or "")
        return _map_status(status, retry_after=None, detail=detail or None)

    # No usable status: fall back to prose, narrowly.
    blob = " ".join(str(err.get(k, "")) for k in ("code", "type", "message")).lower()
    if any(marker in blob for marker in _INSUFFICIENT_CREDIT_MARKERS):
        return provider_insufficient_credits()
    if _looks_rate_limited(err):
        return upstream_rate_limited()
    if _looks_unroutable(blob):
        return upstream_no_compliant_provider()
    return upstream_error("The upstream model stream reported an error.")


def _map_status(
    status: int, *, retry_after: str | None, detail: str | None
) -> DaintreeError:
    """Status -> Daintree error. Shared by the HTTP and in-band stream paths.

    The organizing question is **whose problem is this, and what do they do about
    it?** These outcomes used to collapse into one 502 ``upstream_error``, which
    told a tester with an exhausted balance that their credentials were rejected
    — so they replaced a perfectly good key. The CLI branches on ``error.code``,
    so only distinct codes can produce distinct advice.

    Retryability is set per branch and is load-bearing: an account failure or an
    empty routing pool is deterministic, and replaying it burns the CLI's retry
    budget to arrive at the same answer more slowly.
    """
    if status == 429:
        return upstream_rate_limited(retry_after=retry_after)
    if status in (408, 504):
        return upstream_timeout()

    # ---- The caller's provider account -------------------------------
    # Their own key rode this request, so these are their settings to fix, and
    # each fix is different. Upstream prose is deliberately NOT echoed: the log
    # already carries it, and account/quota detail is not ours to relay.
    if status == 401:
        return provider_invalid_api_key()
    if status == 402:
        return provider_insufficient_credits()
    if status == 403:
        return provider_key_forbidden()

    # ---- Routing ------------------------------------------------------
    # OpenRouter answers "no endpoint matched your preferences" with EITHER a 404
    # or a 503 depending on which filter emptied the pool, so status alone cannot
    # separate routing from a real outage. A 404 on a chat completion is
    # unambiguous (the path is fixed and correct, so it is never a bad URL); a
    # 503 has to be disambiguated by its message.
    if status == 404:
        return upstream_no_compliant_provider()
    if status == 503 and _looks_unroutable(detail):
        return upstream_no_compliant_provider()

    if status >= 500:
        # Genuine upstream fault or capacity shortfall. Transient, so retry while
        # no model output is exposed.
        return upstream_unavailable()

    # Any other 4xx means the upstream rejected OUR request as malformed:
    # permanent, and almost always a server-side bug in what we sent. Carry the
    # upstream's own reason through so the failure is diagnosable instead of an
    # opaque "status 400" — the CLI and logs then name the offending field.
    suffix = f" {detail}" if detail else ""
    return upstream_request_rejected(
        f"The upstream provider rejected this server's request (HTTP {status}).{suffix}"
    )


class OpenRouterStream(ProviderStream):
    """An open OpenRouter SSE stream yielding OpenAI-shaped chunk dicts."""

    def __init__(self, stream_ctx: Any, response: httpx.Response, model: str) -> None:
        self._ctx = stream_ctx
        self._response = response
        self._model = model
        # Bytes, not aiter_lines: see iter_sse_lines — aiter_lines would also split
        # on U+0085/U+2028/U+2029, which are legal unescaped inside JSON content.
        self._events = iter_sse_data(iter_sse_lines(response.aiter_bytes()))
        self._closed = False
        self._done = False
        self._saw_done = False
        self.usage: ProviderUsage | None = None
        self.endpoint: str | None = None

    async def _read_one(self) -> dict[str, Any] | None:
        if self._done:
            return None
        try:
            data = await self._events.__anext__()
        except StopAsyncIteration as exc:
            self._done = True
            # A clean end WITHOUT the upstream [DONE] marker means the stream was
            # truncated; never treat that as a successful finish.
            if not self._saw_done:
                raise upstream_protocol_error("Upstream stream ended before completion.") from exc
            return None
        except httpx.TimeoutException as exc:
            self._done = True
            raise upstream_timeout() from exc
        except httpx.TransportError as exc:
            self._done = True
            # A dropped connection is transient. Retrying is only acted on during
            # open/prefetch, before any model delta is exposed; the mid-stream
            # consumer ignores the flag and surfaces a terminal error instead.
            raise upstream_unavailable("The upstream model stream was interrupted.") from exc
        except SseProtocolError as exc:
            self._done = True
            # Framing violation (oversized line, invalid UTF-8). Surface it as an
            # upstream error like every other malformed-stream case — letting it
            # escape raw would render an internal-error envelope instead of a 502.
            raise upstream_protocol_error("Malformed SSE stream from upstream.") from exc

        try:
            chunk = parse_chunk(data)
        except json.JSONDecodeError as exc:
            self._done = True
            raise upstream_protocol_error("Malformed SSE payload from upstream.") from exc
        except ChunkShapeError as exc:
            # Valid JSON, unusable structure (list root, non-dict choice/delta, …):
            # an upstream protocol violation, surfaced here as a DaintreeError so
            # the consumer never crashes indexing into the chunk.
            self._done = True
            raise upstream_protocol_error(f"Malformed stream chunk from upstream ({exc}).") from exc

        if chunk is None:  # [DONE]
            self._saw_done = True
            self._done = True
            return None
        # parse_chunk guarantees the shape: chunk is a dict, and error/usage, when
        # present, are dicts.
        if chunk.get("error"):
            self._done = True
            raise map_stream_error(chunk["error"])
        if chunk.get("usage"):
            self.usage = ProviderUsage.from_wire(chunk["usage"])
        # Every chunk repeats it; keep the first non-empty one so the value is
        # already available when the caller logs the stream-open timing.
        if self.endpoint is None:
            self.endpoint = _endpoint_name(chunk)
        _normalize_choices(chunk.get("choices"), "delta")
        return chunk

    async def prefetch_first_event(self) -> dict[str, Any] | None:
        return await self._read_one()

    def __aiter__(self) -> OpenRouterStream:
        return self

    async def __anext__(self) -> dict[str, Any]:
        chunk = await self._read_one()
        if chunk is None:
            raise StopAsyncIteration
        return chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):  # pragma: no cover - best-effort close
            await self._ctx.__aexit__(None, None, None)


class OpenRouterProvider:
    """A :class:`ChatProvider` backed by the OpenRouter hosted API."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: SecretStr | None = None,
        base_url: str = "https://openrouter.ai/api/v1",
        default_timeout: float = 60.0,
        send_reasoning: bool = True,
        dump_dir: str | None = None,
    ) -> None:
        self._client = client
        # The FALLBACK credential only: a request carrying its own ``api_key``
        # overrides it. The SERVER passes None here (it holds no upstream key —
        # every call carries the caller's own); standalone tooling (benchmarks,
        # live tests) may pass a default. Held as a SecretStr and unwrapped solely
        # where a header or a redaction pattern is built, never stored as
        # plaintext on the instance.
        self._default_api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_timeout = default_timeout
        self._send_reasoning = send_reasoning
        # Dev-only diagnostic (Settings.dump_upstream_dir, gated out of production
        # by readiness): when set, every upstream request body is written there as
        # numbered pretty-printed JSON. This is THE tool for prompt-cache forensics
        # — diff two dumps to find the exact byte where two "identical" requests
        # diverge (a cache miss is always a prefix divergence). The dump contains
        # full prompts.
        self._dump_dir = dump_dir or None
        self._dump_seq = itertools.count(1)

    def _maybe_dump_body(self, body: dict[str, Any]) -> None:
        if not self._dump_dir:
            return
        try:
            os.makedirs(self._dump_dir, exist_ok=True)
            n = next(self._dump_seq)
            path = os.path.join(self._dump_dir, f"{time.strftime('%H%M%S')}-{n:04d}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(body, f, indent=1, ensure_ascii=False, sort_keys=False)
        except OSError:  # pragma: no cover - diagnostic only, never break a request
            pass

    # ---- helpers --------------------------------------------------------

    @property
    def _url(self) -> str:
        return f"{self._base_url}{_CHAT_PATH}"

    def _resolve_key(self, request_api_key: SecretStr | None) -> SecretStr:
        """The credential for one call: the request's own, else the default.

        Deliberately an ``is not None`` test rather than truthiness. A credential
        supplied explicitly by a caller is never silently replaced by the server's
        own — even when it is empty or malformed, which is rejected below rather
        than papered over. Falling back would spend THIS server's OpenRouter
        account on a request whose owner failed to authenticate.

        Single source of truth for the fallback, because two paths need the same
        answer: the ``Authorization`` header and the redaction of an echoed key out
        of upstream error text. If they ever disagreed, the redaction would miss.

        A credential that cannot become a header value is rejected HERE rather than
        handed to httpx. h11 refuses an empty, whitespace-padded, or control-char
        value with ``LocalProtocolError`` — and that exception embeds the offending
        header value verbatim ("Illegal header value b'Bearer sk-…'"), which
        httpcore logs at DEBUG before this adapter ever sees it. So the permissive
        path leaks the key into logs, exactly what this must not do. Failing fast
        also turns a confusing retryable transport error into an honest permanent
        one, and the message deliberately carries no part of the value.
        """
        key = request_api_key if request_api_key is not None else self._default_api_key
        if key is None:
            # No per-call credential and no configured default. On the serving
            # path this is a propagation bug (auth guarantees every request
            # carries a key): fail loudly here rather than ever spending a
            # shared account.
            raise upstream_error("No upstream credential was supplied for this call.")
        plain = key.get_secret_value()
        if not plain or not all(_HEADER_SAFE_MIN <= ord(c) <= _HEADER_SAFE_MAX for c in plain):
            raise upstream_error("The upstream credential is empty or malformed.")
        return key

    async def verify_key(self, api_key: SecretStr | None = None) -> dict[str, Any]:
        """Ask the provider whether a caller's key is actually usable.

        This is the ONLY honest answer to "is my key valid?". Our own auth is
        structural (see auth.py) — a well-formed string that no provider has ever
        issued passes it — so without this probe the first real turn is where a bad
        key surfaces, long after the user thought they had signed in.

        ``GET /key`` is the right probe: it is the provider's own key-introspection
        endpoint, costs no tokens, and answers for the credential rather than for a
        model. Returns the provider's key record on success.

        Raises ``invalid_api_key`` when the provider rejects the credential (a 401/403
        — a definite "this key is not usable"), and ``upstream_error`` when the probe
        could not be completed at all. The distinction matters: the first is a verdict
        the caller must act on, the second means we simply do not know.
        """
        key = self._resolve_key(api_key)
        url = f"{self._base_url}/key"
        try:
            response = await self._client.get(
                url,
                headers={
                    "Authorization": f"Bearer {key.get_secret_value()}",
                    "Accept": "application/json",
                    "HTTP-Referer": _REFERER,
                    "X-Title": _APP_TITLE,
                },
                timeout=self._verify_timeout(),
            )
        except httpx.TimeoutException as exc:
            raise upstream_timeout("Verifying the key with the provider timed out.") from exc
        except httpx.HTTPError as exc:
            raise upstream_unavailable("Could not reach the provider to verify the key.") from exc

        if response.status_code in (401, 403):
            raise invalid_api_key("The provider rejected this API key.")
        if response.status_code != 200:
            # EXACTLY 200 or we do not have an answer. Note the deliberate `!= 200`
            # rather than `>= 400`: the client does not follow redirects, so a 3xx
            # arrives here as-is, and a body-shaped 302 would otherwise read as a
            # successful verification. Everything that is not the documented success
            # is OUR inability to get an answer, not a verdict on the key — never
            # report an unknown as "invalid". Reuse the codes the chat path already
            # maps so a rate limit or an upstream timeout stays itself.
            raise self._map_verify_error(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise upstream_protocol_error(
                "The provider returned an unreadable key record."
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            # A 200 whose body is not a key record answers nothing. Degrading it to an
            # empty record would surface as `valid: true` — the exact false positive
            # this endpoint exists to prevent — so it is an error, not a default.
            raise upstream_protocol_error("The provider returned no key record.")
        data: dict[str, Any] = payload["data"]
        return data

    def _verify_timeout(self) -> httpx.Timeout:
        """A short read budget for the sign-in probe, structured like ``_timeout``.

        The probe must NOT inherit ``_default_timeout`` (the 14-minute main-model
        budget): a user is watching a sign-in spinner, and a stalled provider would
        otherwise pin a request — and a Cloud Run concurrency slot — for the whole of
        it, on an endpoint any header-safe token can reach. A bare float would also
        collapse the client's connect/write/pool components, which ``_timeout``
        documents at length; only READ is narrowed here.
        """
        base = self._client.timeout
        return httpx.Timeout(
            connect=base.connect, read=_VERIFY_TIMEOUT_SECONDS, write=base.write, pool=base.pool
        )

    def _map_verify_error(self, response: httpx.Response) -> DaintreeError:
        """Non-200 on the probe, mapped like the chat path — minus its 404 prose.

        ``_map_http_error`` is not reused wholesale because its 404 branch names
        `_PROVIDER_ROUTING`, which is chat-completion-specific and would send a reader
        hunting through endpoint-selection policy over a plain introspection call.
        """
        status = response.status_code
        if status == 429:
            return upstream_rate_limited(retry_after=response.headers.get("retry-after"))
        if status in (408, 504):
            return upstream_timeout("Verifying the key with the provider timed out.")
        if status >= 500:
            return upstream_unavailable(
                f"The provider could not verify the key (HTTP {status})."
            )
        return upstream_error(f"The provider could not verify the key (HTTP {status}).")

    def _headers(self, *, stream: bool, api_key: SecretStr) -> dict[str, str]:
        # A fresh dict per call. The shared client's own headers are never mutated:
        # the connection pool keys on host/port/protocol, not header contents, so a
        # per-call Authorization reuses pooled connections safely and concurrent
        # calls carrying different keys cannot bleed into one another.
        return {
            "Authorization": f"Bearer {api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "HTTP-Referer": _REFERER,
            "X-Title": _APP_TITLE,
        }

    def _build_body(self, request: ProviderChatRequest, *, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": _rewrite_messages(request.messages),
        }
        if request.tools:
            body["tools"] = request.tools
        if request.tool_choice is not None:
            body["tool_choice"] = request.tool_choice
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.response_format is not None:
            body["response_format"] = request.response_format
        if request.stop:
            body["stop"] = request.stop
        if self._send_reasoning:
            if request.thinking_disabled:
                # An explicit disable is mandatory: a reasoning model reasons by
                # default when the field is absent, and the non-thinking selector's
                # whole latency argument depends on it actually being off.
                body["reasoning"] = {"enabled": False}
            else:
                # Reasoning mode: the model emits a chain-of-thought (returned as
                # `reasoning`, renamed to reasoning_content on the way back out)
                # before the answer. `effort` tunes the depth; with no tier we send
                # a bare enable and let the endpoint's default apply. `effort` and
                # `max_tokens` are mutually exclusive here — only `effort` is used.
                if request.reasoning_effort:
                    body["reasoning"] = {"effort": request.reasoning_effort}
                else:
                    body["reasoning"] = {"enabled": True}
        # Structured calls route under the tail-bounded policy: `response_format`
        # is exactly the signal that this call sits in the slower JSON-capable pool
        # and cares about time-to-first-token, not tokens/second.
        if request.routing is None:
            # The overwhelmingly common path. Reuse the prebuilt module-level dict
            # rather than re-rendering: this block is serialized into every request
            # body, and the prompt cache keys on a byte-identical prefix.
            body["provider"] = (
                _PROVIDER_ROUTING_FIRST_BYTE if request.latency_critical else _PROVIDER_ROUTING
            )
        else:
            # Server axis FIRST, caller preference on top: `ranked_on_first_byte`
            # replaces the sort key, so applying it last would silently overrule a
            # caller who asked for `price`, and that trade is legitimately theirs.
            # The bound and the privacy floor survive either ordering.
            policy = DEFAULT_POLICY
            if request.latency_critical:
                policy = policy.ranked_on_first_byte()
            body["provider"] = policy.merged_with(request.routing).to_wire()
        # Cache affinity. OpenRouter keeps a sticky record of which endpoint served
        # a cached request and routes the next request with the same key back to
        # it, so a conversation's growing prefix stays on the node that holds it.
        # Left absent, the key defaults to a hash of the first system message and
        # the first non-system message — for this assembler that is the invariant
        # base prompt plus whichever optional row happens to come first (skill
        # bodies, else the integrations block, else startup), so it changes on
        # every selector topic shift and differs per machine on a skill-less turn.
        # Naming the key ourselves replaces that accident with something meant.
        #
        # WHAT THE KEY NAMES varies by call and is chosen by the caller of this
        # adapter, not here: a session for a conversation, the catalog revision for
        # the selector, the task id for a utility task (section 3a).
        #
        # A SIBLING of `provider`, deliberately not part of it: the block above is
        # the endpoint-selection POLICY (filters, sort, fallbacks) and is reused as
        # a prebuilt module-level dict precisely because it must not vary per
        # request. Affinity varies per cohort, so it rides its own top-level field
        # and leaves that dict untouched. It is likewise not `provider.order`, which
        # OpenRouter documents as DISABLING sticky routing — see
        # docs/PROVIDER_POLICY.md section 3.
        if request.cache_session_id:
            body["session_id"] = _cohort_scoped(
                request.cache_session_id, body["provider"], request.routing
            )
        if stream:
            body["stream"] = True
            if request.include_usage:
                body["stream_options"] = {"include_usage": True}
        self._maybe_dump_body(body)
        return body

    def _timeout(self, request: ProviderChatRequest) -> httpx.Timeout:
        """Per-request timeout preserving the client's structured components.

        The per-call budget applies to READ only (for a streaming response httpx
        applies it per chunk gap, and a thinking model can legitimately sit far
        longer between chunks than any sane connect/write/pool bound). A bare
        float here would override ALL components, collapsing connect/write/pool
        to the huge main budget — exactly what the structured client timeout in
        ``main._build_provider`` exists to prevent.
        """
        seconds = request.timeout if request.timeout is not None else self._default_timeout
        base = self._client.timeout
        return httpx.Timeout(connect=base.connect, read=seconds, write=base.write, pool=base.pool)

    def _map_http_error(
        self, status: int, headers: httpx.Headers, detail: str | None = None
    ) -> DaintreeError:
        """The HTTP half of the taxonomy — see :func:`_map_status`.

        A thin wrapper so the streaming in-band path and the HTTP path can never
        drift into two different answers for the same condition.
        """
        return _map_status(
            status, retry_after=headers.get("retry-after"), detail=detail
        )

    def _build_result(self, data: dict[str, Any]) -> ProviderChatResult:
        # `or []` only for a genuinely absent key — a present-but-wrong `choices`
        # (e.g. `{}` or `""`) must fail the guard rather than be coerced to empty.
        raw_choices = data.get("choices")
        choices = [] if raw_choices is None else raw_choices
        _normalize_choices(choices, "message")
        # Structural guard mirroring the STREAM path exactly (shared validator), so the
        # same malformed upstream produces the same 502 on both paths. Consumers index
        # straight into choices[0]["message"] and read content/tool_calls off it, so a
        # bad shape here would otherwise surface as an AttributeError 500.
        try:
            validate_message_shape(choices, data.get("usage"))
        except ChunkShapeError as exc:
            raise upstream_protocol_error(
                "Upstream returned a malformed completion response."
            ) from exc
        finish = choices[0].get("finish_reason") if choices else None
        return ProviderChatResult(
            id=data.get("id", ""),
            created=data.get("created", 0),
            model=data.get("model", ""),
            choices=choices,
            usage=ProviderUsage.from_wire(data.get("usage")),
            finish_reason=finish,
            endpoint=_endpoint_name(data),
            raw=data,
        )

    # ---- ChatProvider ---------------------------------------------------

    async def complete(self, request: ProviderChatRequest) -> ProviderChatResult:
        body = self._build_body(request, stream=False)
        api_key = self._resolve_key(request.api_key)
        try:
            resp = await self._client.post(
                self._url,
                json=body,
                headers=self._headers(stream=False, api_key=api_key),
                timeout=self._timeout(request),
            )
        except httpx.TimeoutException as exc:
            raise upstream_timeout() from exc
        except httpx.TransportError as exc:
            raise upstream_unavailable("Could not reach the upstream provider.") from exc
        if resp.status_code >= 400:
            detail = _extract_upstream_error(resp.content, secret=api_key)
            logger.warning(
                "upstream_http_error",
                phase="complete",
                model=request.model,
                status=resp.status_code,
                detail=detail,
            )
            raise self._map_http_error(resp.status_code, resp.headers, detail)
        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise upstream_protocol_error("Upstream returned a non-JSON response.") from exc
        if not isinstance(data, dict):
            raise upstream_protocol_error("Upstream returned a malformed completion response.")
        return self._build_result(data)

    async def structured_tool_call(
        self,
        request: ProviderChatRequest,
        *,
        function_name: str,
        function_schema: dict[str, Any],
    ) -> ProviderToolCallResult:
        sub = ProviderChatRequest(
            model=request.model,
            messages=request.messages,
            tools=[
                {
                    "type": "function",
                    "function": {"name": function_name, "parameters": function_schema},
                }
            ],
            tool_choice={"type": "function", "function": {"name": function_name}},
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            stream=False,
            # DeepSeek rejects a forced tool_choice under thinking ("Thinking mode
            # does not support this tool_choice"). OpenRouter may route around the
            # endpoint that enforces it, but the constraint is the model's, not the
            # router's, so pin the call non-thinking regardless of what the caller
            # requested rather than gamble the turn on which endpoint answers.
            thinking_disabled=True,
            reasoning_effort=None,
            timeout=request.timeout,
            # Must be forwarded explicitly: this sub-request is rebuilt field by
            # field, so an omission here would silently spend the SERVER's default
            # credential on every forced-tool-call path instead of the caller's.
            api_key=request.api_key,
            # Same reasoning: dropping the caller's routing preference here would
            # silently downgrade a ZDR request to the default privacy filter on
            # the one path that rebuilds its request from scratch.
            routing=request.routing,
            # And again: this sub-request carries the parent's whole prompt, so
            # dropping the affinity key would send the turn's largest call to
            # whatever endpoint the default hash elects — off the node holding the
            # prefix, which is the one thing the key exists to prevent.
            cache_session_id=request.cache_session_id,
            # Same class of omission: this sub-request is the parent's call, so it
            # inherits the parent's cost shape and must be ranked on the same axis.
            latency_critical=request.latency_critical,
        )
        result = await self.complete(sub)
        calls = result.tool_calls
        if not calls:
            # No tool call: surface empty args so the caller's validation path
            # can attempt repair rather than crashing.
            return ProviderToolCallResult(
                name=function_name,
                arguments_json="",
                usage=result.usage,
                endpoint=result.endpoint,
                raw=result.raw,
            )
        fn = calls[0].get("function", {}) or {}
        return ProviderToolCallResult(
            name=fn.get("name", ""),
            arguments_json=fn.get("arguments", "") or "",
            usage=result.usage,
            endpoint=result.endpoint,
            raw=result.raw,
        )

    async def open_stream(self, request: ProviderChatRequest) -> ProviderStream:
        body = self._build_body(request, stream=True)
        api_key = self._resolve_key(request.api_key)
        ctx = self._client.stream(
            "POST",
            self._url,
            json=body,
            headers=self._headers(stream=True, api_key=api_key),
            timeout=self._timeout(request),
        )
        try:
            response = await ctx.__aenter__()
        except httpx.TimeoutException as exc:
            raise upstream_timeout() from exc
        except httpx.TransportError as exc:
            raise upstream_unavailable("Could not reach the upstream provider.") from exc
        if response.status_code >= 400:
            detail: str | None = None
            try:
                # Drain the error body best-effort: a transport failure mid-read
                # must not mask the mapped status error (e.g. a retryable 5xx). When
                # it IS readable, keep its reason — it's the whole diagnostic.
                with contextlib.suppress(httpx.HTTPError):
                    detail = _extract_upstream_error(await response.aread(), secret=api_key)
            finally:
                await ctx.__aexit__(None, None, None)
            logger.warning(
                "upstream_http_error",
                phase="stream_open",
                model=request.model,
                status=response.status_code,
                detail=detail,
            )
            raise self._map_http_error(response.status_code, response.headers, detail)
        return OpenRouterStream(ctx, response, request.model)

    async def aclose(self) -> None:
        await self._client.aclose()
