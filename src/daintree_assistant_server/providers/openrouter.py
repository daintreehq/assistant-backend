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
import itertools
import json
import os
import time
from typing import Any

import httpx
from pydantic import SecretStr

from ..errors import DaintreeError, upstream_error, upstream_rate_limited, upstream_timeout
from ..observability import get_logger
from .base import (
    ProviderChatRequest,
    ProviderChatResult,
    ProviderStream,
    ProviderToolCallResult,
    ProviderUsage,
)
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

# Attribution headers. OpenRouter uses them for its public model/app rankings;
# they carry no auth weight and are safe to send on every call.
_REFERER = "https://daintree.dev"
_APP_TITLE = "Daintree Assistant"

# Endpoint-selection policy, sent on EVERY request. Hardcoded like the model ids
# and the thinking posture (config.py): there is one optimum and the server ships
# it. Two product decisions are encoded here.
#
# THROUGHPUT FIRST. ``sort: "throughput"`` ranks the eligible endpoints by
# measured tokens/second and takes the fastest — explicitly at the cost of price,
# and it disables OpenRouter's default price-weighted load balancing. This is the
# right trade for an interactive assistant: the turn's wall-clock is dominated by
# generation, and this model is cheap enough that a 40% price delta is noise next
# to a 2x speed one. Selection is automatic and re-evaluated per request, so a
# newly-faster endpoint is picked up with no code change.
#
# PRIVACY FLOOR. ``data_collection: "deny"`` restricts routing to endpoints that
# do not store or train on request data — the pool is filtered FIRST, then sorted,
# so throughput never buys its speed with user prompts. (The stricter
# ``"zdr": True`` — zero on-disk retention, not merely no training — is a
# one-key escalation here if it is ever wanted; it narrows the pool further, so
# it is off until measured.)
#
# ``require_parameters`` drops endpoints that would silently ignore something we
# sent. It matters most for the json_object calls (selector, utility tasks):
# several otherwise-fast endpoints for this model do not implement
# ``response_format``, and without this they would answer with unconstrained prose
# that fails our parse. Per-request, so a plain chat turn still reaches them.
#
# ``allow_fallbacks`` keeps the next-fastest compliant endpoint available when the
# winner is down. With filters this strict, turning it off would make any single
# outage a hard 404/503 ("No allowed providers are available").
_PROVIDER_ROUTING: dict[str, Any] = {
    "sort": "throughput",
    "data_collection": "deny",
    "require_parameters": True,
    "allow_fallbacks": True,
}

# Routing for STRUCTURED calls — anything carrying ``response_format`` (the skill
# selector, the utility tasks, the skill-learning gate). Same policy plus a tail
# bound, because these calls have the opposite cost shape to a chat turn.
#
# WHY A SEPARATE POLICY. ``require_parameters`` drops every endpoint that does not
# implement ``response_format``, and for this model that includes the fastest one
# (CoreWeave — measured p50 361ms TTFT, 101 tok/s, and the endpoint a plain chat
# turn actually lands on). Pinning it here is not an option: CoreWeave plus
# ``response_format`` is a hard ``404 No endpoints found``. So a structured call is
# always routed within a DIFFERENT, slower pool, and ``sort: "throughput"`` then
# ranks that pool by tokens/second — the wrong axis entirely for a call that emits
# a ~40-token JSON object. It elected endpoints whose medians are fine and whose
# TAILS are catastrophic: wafer p90 5.4s / p99 12.6s, parasail p99 13.9s, novita
# p99 5.4s. That is not theoretical — a selector call on the turn's first-byte path
# was measured at 10.75s, three quarters of a 14.4s time-to-first-token.
#
# ``preferred_max_latency`` DEPRIORITIZES (never excludes) endpoints whose measured
# TTFT percentiles exceed the bound, so it cannot empty the pool the way a filter
# could — the tail-happy endpoints stay reachable, they just stop winning. The
# numbers are set to clear the observed tail cluster while leaving several
# endpoints qualifying, not to name a favourite. Measured over 10 selector-shaped
# calls: TTFT median 1222ms -> 635ms, worst 1811ms -> 920ms.
#
# Throughput stays the sort key underneath: among endpoints that answer promptly we
# still want the fastest generator, which matters for the longer utility tasks
# (terminal summarize/extract) that share this policy with the tiny selector call.
_PROVIDER_ROUTING_STRUCTURED: dict[str, Any] = {
    **_PROVIDER_ROUTING,
    "preferred_max_latency": {"p90": 2.0, "p99": 5.0},
}


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


def _looks_rate_limited(err: dict[str, Any]) -> bool:
    blob = " ".join(str(err.get(k, "")) for k in ("code", "type", "message")).lower()
    return any(
        token in blob
        for token in ("rate limit", "rate_limit", "429", "quota", "overloaded", "too many requests")
    )


def map_stream_error(err: dict[str, Any]) -> DaintreeError:
    if _looks_rate_limited(err):
        return upstream_rate_limited()
    return upstream_error("The upstream model stream reported an error.")


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
                raise upstream_error("Upstream stream ended before completion.") from exc
            return None
        except httpx.TimeoutException as exc:
            self._done = True
            raise upstream_timeout() from exc
        except httpx.TransportError as exc:
            self._done = True
            # A dropped connection is transient. Retrying is only acted on during
            # open/prefetch, before any model delta is exposed; the mid-stream
            # consumer ignores the flag and surfaces a terminal error instead.
            raise upstream_error(
                "The upstream model stream was interrupted.", retryable=True
            ) from exc
        except SseProtocolError as exc:
            self._done = True
            # Framing violation (oversized line, invalid UTF-8). Surface it as an
            # upstream error like every other malformed-stream case — letting it
            # escape raw would render an internal-error envelope instead of a 502.
            raise upstream_error("Malformed SSE stream from upstream.") from exc

        try:
            chunk = parse_chunk(data)
        except json.JSONDecodeError as exc:
            self._done = True
            raise upstream_error("Malformed SSE payload from upstream.") from exc
        except ChunkShapeError as exc:
            # Valid JSON, unusable structure (list root, non-dict choice/delta, …):
            # an upstream protocol violation, surfaced here as a DaintreeError so
            # the consumer never crashes indexing into the chunk.
            self._done = True
            raise upstream_error(f"Malformed stream chunk from upstream ({exc}).") from exc

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
        body["provider"] = (
            _PROVIDER_ROUTING_STRUCTURED
            if request.response_format is not None
            else _PROVIDER_ROUTING
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
        if status == 429:
            return upstream_rate_limited(retry_after=headers.get("retry-after"))
        if status in (408, 504):
            return upstream_timeout()
        if status >= 500:
            # Transient upstream fault — retry while no model output is exposed.
            return upstream_error(retryable=True)
        # An auth/billing rejection concerns the CALLER's upstream account (their
        # own key rode this request), not the shape of their request. The 502
        # upstream_error code keeps it distinguishable from our own 401
        # invalid_api_key (missing/malformed at our door); upstream prose stays in
        # the log (the upstream_http_error entry already carries the full body)
        # rather than the response, so account/quota detail is never echoed.
        if status in (401, 402, 403):
            return upstream_error("The upstream provider rejected the supplied credentials.")
        # OpenRouter-specific: 404 on a chat completion is routing, not a bad URL —
        # no endpoint satisfied `_PROVIDER_ROUTING` (privacy filter plus
        # require_parameters can empty the pool for an unusual parameter mix). Name
        # it, because the generic "rejected the request" reads as a malformed body
        # and sends the reader hunting in entirely the wrong place.
        if status == 404:
            return upstream_error(
                "No upstream endpoint matched this server's provider-routing policy."
            )
        # Any other 4xx means the upstream rejected OUR request as malformed: permanent
        # (never retried) and almost always a server-side bug in what we sent. Carry
        # the upstream's own reason through so the failure is diagnosable instead of
        # an opaque "status 400" — the CLI and logs then name the offending field.
        suffix = f" {detail}" if detail else ""
        return upstream_error(f"Upstream rejected the request (HTTP {status}).{suffix}")

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
            raise upstream_error("Upstream returned a malformed completion response.") from exc
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
            raise upstream_error("Could not reach the upstream provider.", retryable=True) from exc
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
            raise upstream_error("Upstream returned a non-JSON response.") from exc
        if not isinstance(data, dict):
            raise upstream_error("Upstream returned a malformed completion response.")
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
            raise upstream_error("Could not reach the upstream provider.", retryable=True) from exc
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
