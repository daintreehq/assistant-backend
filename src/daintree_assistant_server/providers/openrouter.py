"""The OpenRouter provider adapter (OpenAI-compatible wire format).

Translates the normalized :class:`ProviderChatRequest` to OpenRouter's
``/chat/completions`` API and maps upstream failures to Daintree errors. A
single process-wide ``httpx.AsyncClient`` is injected; a client is never built
per request.

**This is a SECOND provider for one named task, not a return to routing.** The
old OpenRouter adapter this one descends from (git history has it) grew a whole
endpoint-selection apparatus — a ``provider`` block on every request, a privacy
filter, a fallback pool, an output-quality denylist, a cohort-scoped
sticky-affinity key, and a caller-facing routing preference. All of it was
deleted in the move to Cerebras and **none of it comes back here**. This adapter
sends no ``provider`` block at all, which leaves OpenRouter's own default
price/throughput balancing in charge — the right answer for a single model
pinned to a single internal task, and the reason there is no policy module
beside this one.

Where it differs from the Cerebras adapter it sits next to, each difference is
load-bearing and each is the INVERSE of that one:

* **``max_tokens``, the OpenAI-standard spelling.** Cerebras insists on
  ``max_completion_tokens`` and 400s when both arrive; OpenRouter takes the
  standard name. One spelling per adapter, resolved at the boundary.
* **The ``reasoning`` object is sent EXPLICITLY, and disabled.** DeepSeek V4
  Flash is a reasoning model and reasons by DEFAULT when the field is absent, so
  ``{"enabled": false}`` is what actually keeps a compaction call non-thinking.
  Cerebras is the mirror image: it rejects this object outright and its serving
  model cannot reason, so omission is its non-thinking posture. Sending nothing
  here would silently buy reasoning tokens and latency on every call.
* **Error bodies are NESTED.** OpenRouter returns OpenAI's ``{"error": {...}}``
  envelope, so ``_upstream_error_detail`` reads nested FIRST here and flat first
  there. Both read both shapes; only the priority differs.
* **No cache-affinity field.** Cerebras takes ``prompt_cache_key``; OpenRouter's
  own spelling was ``session_id``, and it is deliberately NOT sent. That field
  was only ever meaningful as a way to steer a sticky record across the pool of
  endpoints the routing block selected from, and that block is gone. Prefix
  caching at the serving endpoint is automatic, so it would buy nothing, and an
  unadvertised parameter is a needless 400 risk.

And one thing it DOES report that Cerebras does not: **cost**. ``usage.cost``
arrives on every response — automatically, since the old ``usage: {"include":
true}`` opt-in is deprecated and ignored — so ``ProviderUsage.cost`` is a real
number from here. Nothing above the provider boundary needed changing for that:
``from_wire`` already parsed the field defensively, and the accounting chain was
built to carry a reported cost rather than assume zero.
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

from ..errors import (
    DaintreeError,
    invalid_api_key,
    provider_insufficient_credits,
    provider_invalid_api_key,
    provider_key_forbidden,
    upstream_error,
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
from .sse import (
    ChunkShapeError,
    SseProtocolError,
    iter_sse_data,
    iter_sse_lines,
    parse_chunk,
    validate_message_shape,
)

logger = get_logger("daintree.openrouter")

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

_CHAT_PATH = "/chat/completions"
# OpenRouter's own key-introspection endpoint, and the `verify_key` probe.
# Unlike Cerebras's `/models` — a model listing that happens to be
# authenticated — this one answers about the CREDENTIAL and nothing else.
_KEY_PATH = "/key"

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

# Read budget for the key-verification probe. Hardcoded, not a setting: it is a
# single tokenless introspection GET behind a sign-in spinner, so it has nothing in
# common with the main model's budget and there is no deployment that wants it long.
_VERIFY_TIMEOUT_SECONDS = 10.0

# Cap on the logged serving-endpoint name ("DeepSeek", "Fireworks", …).
_ENDPOINT_NAME_CAP = 64

# Attribution headers. OpenRouter uses them for its public model/app rankings;
# they carry no auth weight and are safe to send on every call.
_REFERER = "https://daintree.dev"
_APP_TITLE = "Daintree Assistant"


def _extract_upstream_error(
    body: bytes | str | None, *, secret: SecretStr | None = None
) -> str | None:
    """Pull a short human-readable reason out of an upstream error body.

    OpenRouter returns OpenAI's ``{"error": {"message", "type", "code"}}`` envelope
    on a 4xx/5xx. That message is the single most useful diagnostic for a malformed
    request, so we surface it rather than discard it. Best-effort: any parse failure
    falls back to the raw (capped) text, and an empty body yields ``None``.

    ``secret`` is the credential this call used, scrubbed from the text if the
    upstream echoed it back. That reflection is the one way the server's key could
    escape: this return value goes BOTH to the structured log and — for a non-auth
    4xx — into the client-facing error envelope via :meth:`_map_http_error`.

    Order matters twice, and both orderings are deliberate:

    * Scrub AFTER extraction, not on the raw body. JSON can encode an ordinary
      key as ``\\u0063\\u0073\\u006b…``, which no literal match on the undecoded
      bytes would find but ``json.loads`` faithfully turns back into the
      plaintext. ``_upstream_error_detail`` therefore hands back decoded text on
      every branch. (It also keeps the parse honest: replacing inside the raw
      document could make it invalid JSON and silently demote a structured error
      to raw text.)
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

    BOTH error shapes are read, NESTED first — the mirror of the Cerebras
    adapter, which reads flat first because that is where its vendor puts the
    reason. OpenRouter wraps it in OpenAI's ``{"error": {...}}`` envelope; a
    flat body can still arrive from an intermediary (a proxy, a mock, a future
    gateway). Checking only one shape would throw away the entire diagnostic
    from the other, which is precisely the failure this function exists to
    prevent. Only the PRIORITY differs between the two adapters, so a body
    carrying just one shape reads identically in both.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            nested = _reason_from(err)
            if nested:
                return nested
        elif isinstance(err, str) and err:
            return err
        flat = _reason_from(data)
        if flat:
            return flat
    # No single reason to quote: hand back the whole document, decoded.
    return json.dumps(data, ensure_ascii=False)


def _reason_from(payload: dict[str, Any]) -> str | None:
    """The most specific human-readable reason in an error object, if any."""
    for key in ("message", "code", "type"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


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

    Observability ONLY, and reading it is not routing: nothing branches on the
    answer and no request ever asks for a particular one. It is populated here
    precisely because ``ProviderChatResult.endpoint`` was kept for a router —
    against a single-host provider a slow call has one possible culprit, but a
    slow compaction call through OpenRouter is undiagnosable without knowing
    which endpoint served it.

    Best-effort and metadata-only: a non-string value is discarded rather than
    coerced, and the name is capped because it is upstream-supplied text landing
    in our logs.
    """
    name = payload.get("provider")
    return name[:_ENDPOINT_NAME_CAP] if isinstance(name, str) and name else None


def _rewrite_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rename outbound ``reasoning_content`` to the wire's ``reasoning``.

    The assembler replays an assistant tool-call turn's chain-of-thought so the
    model can continue the reasoning that produced those calls. The wire name for
    it is ``reasoning``; an unrecognized ``reasoning_content`` would simply be
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
    speaks ``reasoning_content``, so the upstream spelling stops here. A
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
        if isinstance(value, str) and part.get("reasoning_content") is None:
            part["reasoning_content"] = value


def _looks_rate_limited(err: dict[str, Any]) -> bool:
    """Whether an in-band stream error is a rate limit.

    Deliberately does NOT match "quota". A quota message is far more often an
    exhausted *balance* — a permanent, operator-fixable condition — than a rate
    limit, and calling it a rate limit makes it retryable, so the turn replays a
    doomed request and reports the wrong fix. Insufficient credit is matched
    explicitly in ``map_stream_error`` instead.
    """
    blob = " ".join(str(err.get(k, "")) for k in ("code", "type", "message")).lower()
    return any(
        token in blob
        for token in ("rate limit", "rate_limit", "429", "overloaded", "too many requests")
    )


def _stream_error_status(err: dict[str, Any]) -> int | None:
    """The HTTP-ish status an in-band stream error is reporting, if it says.

    When a numeric ``code`` is there it is a far better signal than prose — so the
    streaming path can reuse the exact same taxonomy as the HTTP path rather than
    inventing a second, weaker one. OpenRouter usually DOES send a numeric code
    here, so this is the branch that normally wins.
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

# Symbolic codes/phrases OpenRouter uses for a rejected credential, where no
# numeric status rides the in-band error. Narrow on purpose: mistaking anything
# else for an auth failure marks a transient condition permanent and kills a
# retry that would have succeeded.
_INVALID_KEY_MARKERS = (
    "invalid_api_key",
    "invalid api key",
    "no auth credentials",
    "user not found",
)


def map_stream_error(err: dict[str, Any]) -> DaintreeError:
    """Map an in-band SSE error onto the SAME taxonomy as an HTTP failure.

    Without this, the streaming path had its own two-outcome world — rate limited
    or generic ``upstream_error`` — which recreated the exact bug the taxonomy
    exists to fix: an invalid key and a spent balance both arrived as one
    retryable-looking code, and the CLI replayed each of them.

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
    if any(marker in blob for marker in _INVALID_KEY_MARKERS):
        return provider_invalid_api_key()
    if _looks_rate_limited(err):
        return upstream_rate_limited()
    return upstream_error("The upstream model stream reported an error.")


def _map_status(status: int, *, retry_after: str | None, detail: str | None) -> DaintreeError:
    """Status -> Daintree error. Shared by the HTTP and in-band stream paths.

    The organizing question is **whose problem is this, and what do they do about
    it?** These outcomes used to collapse into one 502 ``upstream_error``, which
    told an operator with an exhausted balance that their credentials were
    rejected. The CLI branches on ``error.code``, so only distinct codes can
    produce distinct advice.

    Retryability is set per branch and is load-bearing: an account failure is
    deterministic, and replaying it burns the CLI's retry budget to arrive at the
    same answer more slowly.

    Note what is NOT here: the 404-means-routing-dead-end branch the old adapter
    carried. OpenRouter genuinely IS a router, so that branch was once meaningful
    — but it only ever fired because this server sent a routing policy whose
    filters could empty the eligible pool. Nothing sends one now (no ``provider``
    block, no caller-facing preference), so a 404 can no longer mean "the filters
    emptied the pool". What remains — a wrong model id, a misconfigured base URL,
    an intermediary that does not have the route — is in every case a server-side
    bug in what we sent rather than anything a caller can act on, which is
    exactly ``upstream_request_rejected``, carrying the upstream's own reason.
    Reviving the branch would mean reviving the policy that made it necessary.
    """
    if status == 429:
        return upstream_rate_limited(retry_after=retry_after)
    if status in (408, 504):
        return upstream_timeout()

    # ---- The provider account ----------------------------------------
    # Each of these is a different fix, and upstream prose is deliberately NOT
    # echoed: the log already carries it, and account/quota detail is not ours to
    # relay.
    if status == 401:
        return provider_invalid_api_key()
    if status == 402:
        return provider_insufficient_credits()
    if status == 403:
        return provider_key_forbidden()

    if status >= 500:
        # Genuine upstream fault or capacity shortfall. Transient, so retry while
        # no model output is exposed.
        return upstream_unavailable()

    # Any other 4xx (400, 404, 422, …) means the upstream rejected OUR request as
    # malformed: permanent, and almost always a server-side bug in what we sent.
    # Carry the upstream's own reason through so the failure is diagnosable
    # instead of an opaque "status 400" — the CLI and logs then name the
    # offending field.
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
        # OpenRouter names the endpoint that served the stream on its chunks, so
        # unlike the single-host adapter this is genuinely reportable. Set once,
        # from the first chunk that carries a name (see `_read_one`).
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
    """A :class:`ChatProvider` backed by the OpenRouter API.

    Built only when ``OPENROUTER_API_KEY`` is configured (see
    ``main._build_openrouter_provider``). It is not on the serving path: today
    exactly one utility task routes here, and a deployment without the key
    simply has no instance of this class at all.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: SecretStr | None = None,
        base_url: str = DEFAULT_BASE_URL,
        default_timeout: float = 60.0,
        dump_dir: str | None = None,
    ) -> None:
        self._client = client
        # The SERVER's own upstream credential, and on the serving path the only
        # one there is: Daintree funds every turn from this key rather than from a
        # caller's. A request may still carry its own ``api_key`` (benchmarks and
        # live tests do), and it overrides this. Held as a SecretStr and unwrapped
        # solely where a header or a redaction pattern is built, never stored as
        # plaintext on the instance.
        self._default_api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_timeout = default_timeout
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
        """The credential for one call: the request's own, else the server's.

        Deliberately an ``is not None`` test rather than truthiness. A credential
        supplied explicitly by a caller is never silently replaced by the server's
        own — even when it is empty or malformed, which is rejected below rather
        than papered over.

        Single source of truth for the fallback, because two paths need the same
        answer: the ``Authorization`` header and the redaction of an echoed key out
        of upstream error text. If they ever disagreed, the redaction would miss.

        A credential that cannot become a header value is rejected HERE rather than
        handed to httpx. h11 refuses an empty, whitespace-padded, or control-char
        value with ``LocalProtocolError`` — and that exception embeds the offending
        header value verbatim ("Illegal header value b'Bearer sk-or-v1-…'"), which
        httpcore logs at DEBUG before this adapter ever sees it. So the permissive
        path leaks the key into logs, exactly what this must not do. Failing fast
        also turns a confusing retryable transport error into an honest permanent
        one, and the message deliberately carries no part of the value.
        """
        key = request_api_key if request_api_key is not None else self._default_api_key
        if key is None:
            # No per-call credential and no configured default. Readiness does
            # NOT gate OPENROUTER_API_KEY — a missing key must degrade the one
            # task that routes here, never fail /readyz — so the guard against
            # reaching this point is that the provider is not CONSTRUCTED without
            # a key at all (main._build_openrouter_provider returns None, and the
            # task runner raises `provider_not_configured` instead). This branch
            # is the defensive floor under a directly-constructed instance: fail
            # loudly rather than send an unauthenticated request and read the 401
            # back as the caller's fault.
            raise upstream_error("No upstream credential was supplied for this call.")
        plain = key.get_secret_value()
        if not plain or not all(_HEADER_SAFE_MIN <= ord(c) <= _HEADER_SAFE_MAX for c in plain):
            raise upstream_error("The upstream credential is empty or malformed.")
        return key

    async def verify_key(self, api_key: SecretStr | None = None) -> dict[str, Any]:
        """Ask the provider whether a credential is actually usable.

        ``GET /key`` is the right probe: it is OpenRouter's own key-introspection
        endpoint, costs no tokens, and answers for the credential rather than for
        a generation. Cerebras's ``/models`` listing is the wrong shape here —
        OpenRouter serves its model catalogue unauthenticated, so a listing would
        come back 200 for a key that is not usable at all.

        Raises ``invalid_api_key`` when the provider rejects the credential (a
        401/403 — a definite "this key is not usable"), and ``upstream_error`` when
        the probe could not be completed at all. The distinction matters: the first
        is a verdict to act on, the second means we simply do not know.
        """
        key = self._resolve_key(api_key)
        url = f"{self._base_url}{_KEY_PATH}"
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
            # A 200 whose body is not a key record answers nothing. Degrading it to
            # an empty record would surface as `valid: true` — the exact false
            # positive this endpoint exists to prevent — so it is an error, not a
            # default.
            raise upstream_protocol_error("The provider returned no key record.")
        # Pass the record through rather than projecting fields out of it: this
        # is a diagnostic, and usage/limit/free-tier detail is the useful part of
        # an answer to "is my key usable?".
        #
        # Nothing calls this today — the doctor endpoint probes
        # ``container.provider``, which is the SERVING adapter — so the record
        # reaches no response body. It is implemented because `verify_key` is
        # part of the protocol and a half-built one would fail at the moment
        # someone finally needed it. A future caller that DOES surface this must
        # allowlist fields on the way out, the way the existing diagnostic
        # endpoints already do, rather than forwarding an account record whole.
        record: dict[str, Any] = payload["data"]
        return record

    def _verify_timeout(self) -> httpx.Timeout:
        """A short read budget for the sign-in probe, structured like ``_timeout``.

        The probe must NOT inherit ``_default_timeout`` (the main-model budget): a
        user is watching a spinner, and a stalled provider would otherwise pin a
        request — and a Cloud Run concurrency slot — for the whole of it. A bare
        float would also collapse the client's connect/write/pool components, which
        ``_timeout`` documents at length; only READ is narrowed here.
        """
        base = self._client.timeout
        return httpx.Timeout(
            connect=base.connect, read=_VERIFY_TIMEOUT_SECONDS, write=base.write, pool=base.pool
        )

    def _map_verify_error(self, response: httpx.Response) -> DaintreeError:
        """Non-200 on the probe, mapped like the chat path."""
        status = response.status_code
        if status == 429:
            return upstream_rate_limited(retry_after=response.headers.get("retry-after"))
        if status in (408, 504):
            return upstream_timeout("Verifying the key with the provider timed out.")
        if status >= 500:
            return upstream_unavailable(f"The provider could not verify the key (HTTP {status}).")
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
            # Attribution only — OpenRouter uses these for its public model/app
            # rankings. They carry no auth weight.
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
            # The OpenAI-standard spelling, and the INVERSE of the Cerebras
            # adapter's choice. OpenRouter accepts `max_completion_tokens` too, so
            # the rule that matters is that exactly one of them is ever sent —
            # each adapter resolves the normalized field to its own single name.
            body["max_tokens"] = request.max_tokens
        if request.response_format is not None:
            body["response_format"] = request.response_format
        if request.stop:
            body["stop"] = request.stop
        # Reasoning is a unified OBJECT here, and the disable is MANDATORY rather
        # than implied. The model this adapter exists to reach (DeepSeek V4 Flash)
        # reasons by DEFAULT when the field is absent, so an omitted `reasoning`
        # does not mean "non-thinking" — it means "think, at the endpoint's
        # default depth", silently buying tokens and latency on a call whose whole
        # purpose is to be cheap. This is the exact inverse of the Cerebras
        # adapter, where the wire has no field that could express the disable and
        # omission IS the posture.
        if request.thinking_disabled:
            body["reasoning"] = {"enabled": False}
        elif request.reasoning_effort:
            body["reasoning"] = {"effort": request.reasoning_effort}
        else:
            body["reasoning"] = {"enabled": True}
        # NO cache-affinity field, deliberately — see the module docstring. The
        # normalized `cache_session_id` is accepted and IGNORED here rather than
        # translated: OpenRouter's spelling for it (`session_id`) only ever meant
        # anything as a way to pin a sticky record across the endpoint pool the
        # routing block selected from, and that block is gone. Prefix caching at
        # whichever endpoint serves the call is automatic, so the field would buy
        # nothing and an unadvertised parameter is a needless 400 risk.
        #
        # NOT `prompt_cache_key` either: that is the Cerebras adapter's spelling,
        # and it is not a parameter OpenRouter advertises.
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
        return _map_status(status, retry_after=headers.get("retry-after"), detail=detail)

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
            # A forced tool_choice and a chain-of-thought are an awkward pair on
            # every model this server has run against, and the caller wants one
            # object back rather than reasoning about it. Pin it non-thinking
            # regardless of what the caller requested.
            thinking_disabled=True,
            reasoning_effort=None,
            timeout=request.timeout,
            # Must be forwarded explicitly: this sub-request is rebuilt field by
            # field, so an omission here would silently fall back to the server's
            # default credential on every forced-tool-call path instead of using
            # the one the caller pinned.
            api_key=request.api_key,
            # Forwarded to keep the rebuilt sub-request a faithful copy of its
            # parent, though `_build_body` sends nothing for it here — see the
            # module docstring on why this adapter has no cache-affinity field.
            cache_session_id=request.cache_session_id,
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
