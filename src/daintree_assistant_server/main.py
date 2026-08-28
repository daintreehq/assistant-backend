"""FastAPI application factory and lifespan.

Builds the immutable service container (settings, prompts, catalog, provider,
state codec, selector, assembler, orchestrator, task runner), validates
readiness on startup, and tears the provider down on shutdown. A malformed
prompt or runbook, or a missing required secret, prevents the app from becoming
ready (``/readyz`` returns 503) without crashing liveness.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from http.cookiejar import DefaultCookiePolicy
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request

from . import __version__
from .api import (
    daintree_account,
    daintree_auth,
    daintree_capabilities,
    daintree_doctor,
    daintree_respond,
    daintree_runbooks,
    daintree_tasks,
    health,
    root,
)
from .config import EXECUTOR_REASONING_EFFORT, Settings, get_settings
from .errors import register_exception_handlers, request_too_large
from .observability import (
    bind_request_context,
    clear_request_context,
    configure_logging,
    get_logger,
    new_request_id,
)
from .prompts.loader import PromptLoader
from .providers.base import ChatProvider
from .providers.baseten import BasetenProvider
from .providers.cerebras import CerebrasProvider
from .providers.openrouter import OpenRouterProvider
from .runbooks.catalog import Catalog, CatalogManager, default_runbooks_dir, load_catalog
from .runbooks.local.paths_local import selector_artifact_dir
from .runbooks.local.runtime import InferencePool
from .runbooks.local.selector import LocalSelector, RunbookSelector
from .runbooks.local.setwise import SetwiseClassifier
from .services.bearer import authorization_values, credential_candidates
from .services.chat_orchestrator import ChatOrchestrator
from .services.compaction import Compactor, NullCompactor, default_compactor
from .services.docs_mcp import DOCS_LOOKUP_TIMEOUT_SECONDS, DocsMcpClient
from .services.entitlement import (
    ENTITLEMENT_CONNECT_TIMEOUT_SECONDS,
    ENTITLEMENT_TIMEOUT_SECONDS,
    EntitlementService,
)
from .services.fast_response import FastResponseService
from .services.prompt_assembler import PromptAssembler
from .services.supabase_auth import JwksCache, SupabaseTokenVerifier
from .services.task_runner import TaskRunner
from .state.codec import StateCodec
from .tracing import (
    DefaultTraceRecorder,
    FilesystemTraceSink,
    NullTraceRecorder,
    NullTraceSink,
    TraceRecorder,
    TraceSink,
    decide,
)
from .tracing.sinks.gcs import GcsTraceSink

logger = get_logger("daintree.main")


class BodySizeLimitMiddleware:
    """Reject oversized request bodies by counting bytes as they arrive.

    A ``Content-Length`` header is honored up front, but the limit is *also*
    enforced by reading the body in the middleware and bailing the moment it
    exceeds the cap — so a chunked request (or one that omits ``Content-Length``)
    cannot push an unbounded body into the JSON parser. The buffered body is
    then replayed to the application unchanged. The decision is made *before*
    the app runs, so the framework cannot reinterpret the abort as a 400.
    """

    def __init__(self, app: Any, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > self.max_bytes:
                    await self._reject(scope, send)
                    return
                break

        body = bytearray()
        more_body = True
        while more_body:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") != "http.request":
                continue
            body += message.get("body", b"")
            more_body = bool(message.get("more_body", False))
            if len(body) > self.max_bytes:
                await self._reject(scope, send)
                return

        replayed = False

        async def replay_receive() -> Any:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _reject(self, scope: Any, send: Any) -> None:
        async def _eat() -> Any:  # pragma: no cover - response sends no body reads
            return {"type": "http.request", "body": b"", "more_body": False}

        response = request_too_large("Request body exceeds the configured limit.").to_response()
        # This middleware is OUTERMOST, so it answers before the request-context
        # middleware that normally mints and attaches the id. Without this, a 413
        # is the one response a tester cannot quote a request id for — precisely
        # the failure they are most likely to be reporting. Honor an inbound id
        # when the client sent one, exactly as the inner middleware does.
        raw_headers = list(scope.get("headers") or [])
        # `dict(raw_headers)` would keep only the LAST value for a repeated
        # header, which is precisely how a second `Authorization` would become
        # invisible to the overlap check below.
        supplied = [v for k, v in raw_headers if k.lower() == b"x-request-id"]
        try:
            # Exactly one, or none of them. Two inbound ids are an ambiguity this
            # server cannot resolve — picking either means our logs may name a
            # different id than the intermediary's do, which is worse than a
            # minted id that at least matches everything downstream of here.
            inbound = supplied[0].decode("ascii") if len(supplied) == 1 else ""
        except UnicodeDecodeError:  # pragma: no cover - defensive
            inbound = ""
        response.headers.setdefault(
            "X-Request-Id", _safe_request_id(inbound, authorization_values(raw_headers))
        )
        await response(scope, _eat, send)


# Bounds on an inbound X-Request-Id we are willing to adopt. A request id is a
# correlation handle: it lands in every structured log line for the request and is
# echoed in the response header, so it is the one caller-controlled string that
# gets copied verbatim into our logs.
_MAX_REQUEST_ID_LENGTH = 128
_REQUEST_ID_SAFE = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
)
# Shortest id that is treated as a possible credential FRAGMENT. Below this, an
# id being a substring of the bearer means nothing — "a" is a substring of almost
# every key — and rejecting on it would discard legitimate short ids. Above it,
# the overlap is long enough to be real credential material worth withholding.
_MIN_CREDENTIAL_FRAGMENT = 8


def _safe_request_id(inbound: str | None, authorizations: Sequence[str] | str | None) -> str:
    """Adopt the caller's correlation id, or mint one when it isn't safe to.

    Honoring an inbound id is genuinely useful — it is how a CLI ties its own
    logs to ours — but it is also an unvalidated caller string headed straight
    for our log pipeline. Three ways it is refused:

    * **Shape.** Bounded length and a conservative charset, so an id cannot smuggle
      newlines, control characters or a kilobyte of padding into a log line.
    * **Credential overlap.** A bearer token is header-safe ASCII, which means a
      caller's own API key is a *structurally valid* request id. Somebody
      scripting against this API can paste one into the wrong variable, and we
      would faithfully log and echo their spend-authorizing credential. Checked
      both directions, since a prefix is as damaging as the whole value — and
      against EVERY ``Authorization`` header rather than one of them. Starlette's
      ``Headers.get`` returns the first and a ``dict`` of the raw pairs keeps the
      last, so with two such headers either accessor silently drops one, and a
      token in the dropped one would reach the log pipeline untouched. The
      candidates come from ``services.bearer``, whose extractor is deliberately
      lenient: a header the strict parser refuses is *more* likely to hold a
      mis-pasted credential, not less.
    * **Emptiness.**

    Refusal is silent and non-fatal: we simply mint our own. Rejecting the request
    would turn a harmless client mistake into an outage, and the caller's id has
    no authority here anyway.
    """
    # A bare `str` is itself a `Sequence[str]`, so passing one where a list was
    # meant type-checks perfectly and then iterates CHARACTERS — every id becomes
    # a "substring of the credential" and is thrown away, silently, on a path
    # whose whole purpose is not to be silent. Normalised rather than trusted,
    # because the type system cannot object and the failure is invisible.
    if authorizations is None:
        authorizations = ()
    elif isinstance(authorizations, str):
        authorizations = (authorizations,)

    if not inbound:
        return new_request_id()
    candidate = inbound.strip()
    if not candidate or len(candidate) > _MAX_REQUEST_ID_LENGTH:
        return new_request_id()
    if not all(ch in _REQUEST_ID_SAFE for ch in candidate):
        return new_request_id()
    for secret in credential_candidates(authorizations):
        # Containing the whole token is damaging at any length. The reverse — the
        # id being a fragment OF the token — only matters once the fragment is long
        # enough to be credential material rather than a coincidence.
        if secret in candidate:
            return new_request_id()
        if len(candidate) >= _MIN_CREDENTIAL_FRAGMENT and candidate in secret:
            return new_request_id()
    return candidate


@dataclass
class Container:
    """All long-lived service components."""

    settings: Settings
    prompt_loader: PromptLoader
    catalog_manager: CatalogManager
    provider: ChatProvider
    state_codec: StateCodec
    selector: RunbookSelector
    assembler: PromptAssembler
    orchestrator: ChatOrchestrator
    task_runner: TaskRunner
    # The second adapter, or None when OPENROUTER_API_KEY is unset. `None` is a
    # NORMAL configuration, not a degraded one — the credential is optional and
    # ungated, so a deployment without it is complete and only the one utility
    # task that routes there is unavailable (it 503s saying so).
    openrouter_provider: ChatProvider | None = None
    # Its own httpx client, kept here so lifespan can close it. Deliberately not
    # the provider's: the docs server is a different host with a different
    # latency profile, and a documentation search must never queue behind a
    # long-lived streaming completion holding a pooled connection.
    # The context compactor. `NullCompactor` unless a producer is injected —
    # which is every deployment today, because issue #60 (the second provider and
    # the reconciliation task) has not landed. Held here so `/capabilities` can
    # report honestly whether the feature can actually do anything.
    compactor: Compactor = field(default_factory=NullCompactor)
    # The training-trace plane. `NullTraceRecorder` unless an operator configured
    # capture AND the configuration is valid — see `_build_trace_recorder`. Held
    # here so lifespan can drain its uploads before the clients they ride on close.
    trace_recorder: TraceRecorder = field(default_factory=NullTraceRecorder)
    # The second internal provider ROLE — not a second product. `None` unless the
    # fast stage is configured, which is every deployment today.
    fast_response_provider: ChatProvider | None = None
    docs_http_client: httpx.AsyncClient | None = None
    # The local selector's worker pool — owned here so lifespan teardown joins
    # its threads. None when a selector override was injected (tests/benchmarks).
    inference_pool: InferencePool | None = None

    @property
    def catalog(self) -> Catalog:
        """The current catalog, read live — CatalogManager supports reload."""
        return self.catalog_manager.catalog

    async def aclose(self) -> None:
        """Drain in-flight background work, then close the providers.

        Best-effort teardown usable by any owner (the app lifespan, or a direct
        ``build_container`` caller).
        """
        with contextlib.suppress(Exception):  # pragma: no cover - best-effort drain
            # Detached tasks own live upstream calls; drain them (bounded) before
            # the provider they are calling through is torn down.
            await self.orchestrator.drain_background_tasks()
        # Trace uploads BEFORE the clients they ride on. The GCS sink borrows the
        # shared httpx client, so closing that first would turn every in-flight
        # trace write into a failure at exactly the moment there is no longer any
        # turn to protect.
        with contextlib.suppress(Exception):  # pragma: no cover - best-effort drain
            await self.trace_recorder.aclose()
        with contextlib.suppress(Exception):  # pragma: no cover - best-effort close
            await self.provider.aclose()
        if self.fast_response_provider is not None:
            with contextlib.suppress(Exception):  # pragma: no cover - best-effort close
                await self.fast_response_provider.aclose()
        if self.openrouter_provider is not None:
            with contextlib.suppress(Exception):  # pragma: no cover - best-effort close
                await self.openrouter_provider.aclose()
        if self.docs_http_client is not None:
            with contextlib.suppress(Exception):  # pragma: no cover - best-effort close
                await self.docs_http_client.aclose()
        if self.inference_pool is not None:
            with contextlib.suppress(Exception):  # pragma: no cover - best-effort close
                self.inference_pool.close()


# Structured upstream-timeout components, hardcoded (no env toggles — one
# optimum, like the model ids). READ deliberately stays at the big
# main_timeout_seconds: on a streaming response httpx applies it to the gap
# between chunks, and a thinking model can legitimately sit minutes inside a
# reasoning block before its next delta — a tight read bound would kill healthy
# turns. Sending our request (write) and waiting for a pooled connection never
# legitimately take that long, so they get tight bounds of their own.
_UPSTREAM_WRITE_TIMEOUT_SECONDS = 30.0
_UPSTREAM_POOL_TIMEOUT_SECONDS = 10.0


def _build_docs_client() -> tuple[httpx.AsyncClient, DocsMcpClient]:
    """The documentation MCP client and the connection pool it owns.

    Small and short-timeout by design, and separate from the provider pool. Every
    per-call timeout is passed explicitly by the caller from what remains of the
    turn's preparation budget; the values here are the ceiling that applies when
    nothing tighter is in force. Connection limits are modest because a docs
    lookup happens on a small fraction of turns — the search only runs when the
    selector classified the turn as a question about Daintree itself.
    """
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(DOCS_LOOKUP_TIMEOUT_SECONDS, connect=2.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
    )
    return client, DocsMcpClient(client)


def _build_fast_response_provider(settings: Settings) -> ChatProvider | None:
    """The Cerebras adapter, or None when there is no credential.

    None only ever means "no credential", and readiness refuses that outright —
    the stage has no off switch. The None path stays because `build_container` is
    also called directly by tests and benchmarks, which never probe readiness.

    Its own httpx client, not the executor's. The two hosts have opposite latency
    profiles and opposite lifetimes — a three-second preamble call must never
    queue behind a long-lived streaming completion holding a pooled connection,
    which is the same reasoning that gives the docs MCP its own pool.
    """
    if not settings.cerebras_api_key.get_secret_value():
        # Readiness already refuses this (`Settings.fast_response_problems`), but
        # `build_container` is also called directly by tests and benchmarks, which
        # never probe readiness. Failing to None here keeps those paths honest.
        return None
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.fast_response_timeout_seconds,
            connect=2.0,
            write=_UPSTREAM_WRITE_TIMEOUT_SECONDS,
            pool=_UPSTREAM_POOL_TIMEOUT_SECONDS,
        ),
        limits=httpx.Limits(max_connections=40, max_keepalive_connections=10),
    )
    return CerebrasProvider(
        client,
        api_key=settings.cerebras_api_key,
        base_url=settings.cerebras_base_url or None,
        default_timeout=settings.fast_response_timeout_seconds,
        send_prompt_cache_key=settings.cerebras_prompt_cache_enabled,
    )


def _build_trace_recorder(
    settings: Settings, http_client: httpx.AsyncClient | None
) -> TraceRecorder:
    """The training-trace recorder, or the null one — which is every deployment.

    Returns ``NullTraceRecorder`` unless an operator has deliberately set a
    capture mode AND the configuration passes ``Settings.trace_problems()``. The
    second half matters: readiness already refuses a bad trace configuration, but
    ``build_container`` is also called directly by tests and benchmarks, and a
    recorder that captured content because nobody happened to call ``/readyz``
    would be the whole failure this gate exists to prevent.
    """
    if settings.trace_capture_mode == "off" or settings.trace_problems():
        return NullTraceRecorder()

    eligibility = decide(
        mode=settings.trace_capture_mode, is_hardened=settings.is_hardened
    )
    if not eligibility.enabled:
        return NullTraceRecorder()

    sink: TraceSink
    if settings.trace_sink == "filesystem":
        sink = FilesystemTraceSink(Path(settings.trace_filesystem_dir))
    elif settings.trace_sink == "gcs" and http_client is not None:
        sink = GcsTraceSink(
            bucket=settings.trace_gcs_bucket,
            prefix=settings.trace_gcs_prefix,
            client=http_client,
        )
    else:
        sink = NullTraceSink()

    return DefaultTraceRecorder(
        sink=sink,
        identity_key=settings.trace_identity_key.get_secret_value().encode("utf-8"),
        eligibility=eligibility,
        max_event_bytes=settings.trace_max_event_bytes,
        max_pending_bytes=settings.trace_max_pending_bytes,
        flush_grace_seconds=settings.trace_final_flush_grace_seconds,
    )


def _build_provider(settings: Settings) -> ChatProvider:
    """The serving adapter: Baseten, holding the server's own credential.

    The OpenAI-shape adapter, pointed at Baseten's Model APIs. Same SSE, same
    `max_tokens` spelling, same tool-call chunks as the OpenRouter parent — the
    vendor difference is the key-probe shape and the THINKING POSTURE, which is
    the one place this host is not interchangeable: GLM-5.3-Flash cannot be run
    non-thinking, so the adapter sends `reasoning_effort` where the parent sends
    a `reasoning` object. See providers/baseten.py.
    """
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=settings.upstream_connect_timeout_seconds,
            read=settings.main_timeout_seconds,
            write=_UPSTREAM_WRITE_TIMEOUT_SECONDS,
            pool=_UPSTREAM_POOL_TIMEOUT_SECONDS,
        ),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    return BasetenProvider(
        client,
        # THE server credential. Daintree funds every turn from this key: callers
        # supply none of their own (see auth.py), so a provider built without one
        # cannot answer at all — which is why `missing_secrets` gates readiness on
        # it rather than letting the first turn discover the gap.
        api_key=settings.serving_api_key,
        base_url=settings.serving_base_url,
        # The floor this model exposes; there is no "off" on these weights. The
        # decision lives beside the model id in `config` and is injected here so
        # the adapter does not reach back into settings for it.
        reasoning_effort=EXECUTOR_REASONING_EFFORT,
        default_timeout=settings.main_timeout_seconds,
        dump_dir=settings.dump_upstream_dir,
    )


def _build_openrouter_provider(settings: Settings) -> ChatProvider | None:
    """The second adapter, or ``None`` when its credential is not configured.

    NOT CONSTRUCTING is the whole design. A keyless provider would start fine and
    then fail on its first call with an upstream 401 — an error that reads like a
    bad key rather than an absent one, and that arrives at the worst possible
    moment to diagnose it. Returning ``None`` instead makes "not configured" a
    state the task runner can see and report as itself
    (``provider_not_configured``, naming the provider).

    It gets its OWN client rather than sharing the serving one — for isolation,
    which is the same argument the docs client already makes here: a compaction
    call must never queue behind a live turn holding a pooled connection, and a
    misbehaving second vendor must not be able to exhaust the pool the serving
    path depends on. (Sharing would be technically possible — neither client is
    given a ``base_url`` and the adapters build absolute URLs — so this is a
    deliberate choice, not a constraint.) The pool is small because compaction is
    an occasional background task rather than the serving path, against the ~100
    connections that one sizes for; it is not a concurrency limit, and nothing
    gates how many compactions may run at once.
    """
    if not settings.openrouter_api_key.get_secret_value():
        return None
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=settings.upstream_connect_timeout_seconds,
            # The heavy-task budget, not the main model's: nothing streams here,
            # so there is no long inter-chunk gap to accommodate.
            read=settings.utility_timeout_seconds,
            write=_UPSTREAM_WRITE_TIMEOUT_SECONDS,
            pool=_UPSTREAM_POOL_TIMEOUT_SECONDS,
        ),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
    )
    return OpenRouterProvider(
        client,
        api_key=settings.openrouter_api_key,
        default_timeout=settings.utility_timeout_seconds,
        dump_dir=settings.dump_upstream_dir,
    )


def _build_state_codec(settings: Settings) -> StateCodec:
    previous: dict[str, bytes] = {}
    prev_key = settings.state_signing_key_previous.get_secret_value()
    if prev_key and settings.state_signing_kid_previous:
        previous[settings.state_signing_kid_previous] = prev_key.encode("utf-8")
    return StateCodec(
        current_key=settings.state_signing_key_current.get_secret_value().encode("utf-8"),
        current_kid=settings.state_signing_kid,
        salt=settings.state_salt.get_secret_value().encode("utf-8"),
        ttl_seconds=settings.state_token_ttl_seconds,
        previous_keys=previous,
    )


def build_container(
    settings: Settings,
    provider: ChatProvider | None = None,
    selector: RunbookSelector | None = None,
    compactor: Compactor | None = None,
) -> Container:
    """Construct and validate all service components.

    ``selector`` override: tests and deterministic benchmarks inject the
    scriptable prompt-based selector here so the container skips loading the
    ONNX artifact entirely — production always leaves it None and gets the
    local classifier.
    """
    prompt_loader = PromptLoader()
    prompt_loader.validate()

    # The packaged runbooks are the catalog. `DAINTREE_RUNBOOKS_OVERLAY_DIR` (local
    # development only, refused in production by `local_only_problems`) merges a
    # scratch directory on top, so a runbook can be authored against a running
    # server and picked up by POST /v1/daintree/runbooks/reload without a restart.
    # The path is fixed here for the process; only its contents are re-read.
    primary_dir = default_runbooks_dir()
    overlay_dir = Path(settings.runbooks_overlay_dir) if settings.runbooks_overlay_dir else None
    catalog = load_catalog(primary_dir, overlay_dir)
    catalog_manager = CatalogManager(catalog, primary_dir=primary_dir, overlay_dir=overlay_dir)

    state_codec = _build_state_codec(settings)
    # The selector is built BEFORE the provider: loading it can raise, and
    # raising after the provider exists would leave an HTTP client open in a
    # process that deliberately stays alive (unready, not dead).
    inference_pool: InferencePool | None = None
    if selector is None:
        # Runbook selection is LOCAL: a cosine shortlist plus one set-wise pass,
        # in process, replacing the upstream model call that used to sit on the
        # turn's first-byte path (p50 2.0s). Constructed eagerly so a missing or
        # LFS-pointer artifact fails container validation — readiness, not a
        # request — exactly like a malformed prompt or runbook would.
        classifier = SetwiseClassifier(selector_artifact_dir())
        # Embed the live catalog now rather than on the first turn: it validates
        # the artifact against a real catalog at startup, and it means the first
        # request does not pay for whatever drifted since the image was built.
        # There is deliberately no staleness GATE — the head has no per-runbook
        # weights, so a catalog the artifact never saw is answered normally
        # instead of degrading every turn open until someone retrains.
        classifier.sync(catalog_manager.catalog)
        inference_pool = InferencePool()
        selector = LocalSelector(classifier, inference_pool, catalog_manager, settings)
    chat_provider = provider if provider is not None else _build_provider(settings)
    # Never substituted by the `provider` override: that argument exists so tests
    # and deterministic benchmarks can replace the SERVING provider, and silently
    # pointing the compaction task at the same fake would hide the fact that it
    # runs somewhere else entirely.
    openrouter_provider = _build_openrouter_provider(settings)
    docs_http_client, docs_client = _build_docs_client()
    # No producer exists yet (issue #60), so this is the null one on every real
    # deployment: nothing is started, nothing is awaited, and no `compaction`
    # event is ever emitted.
    chat_compactor: Compactor = compactor if compactor is not None else default_compactor()
    # ...which is also why the base prompt does not carry the declaration rule
    # here. The control line exists to let compaction start while the reply is
    # still being written; with no consumer it is bytes on every request and a
    # generated line on every reply, buying one log field. The assembler is built
    # AFTER the compactor for this reason alone — the flag has to come from it.
    #
    # Read ONCE, at startup, unlike `/capabilities` and the orchestrator which
    # re-read `available` per operation. That asymmetry is deliberate: a prompt
    # that changed mid-process would split the prefix cache between the requests
    # either side of the flip, and the flip cannot help the requests already in
    # flight anyway — a model can only declare what it was asked to declare. A
    # compactor whose availability genuinely varies at runtime therefore needs a
    # container rebuild to change the prompt, not just a property returning True.
    fast_response_provider = _build_fast_response_provider(settings)
    fast_response = FastResponseService(
        settings=settings,
        prompt_loader=prompt_loader,
        provider=fast_response_provider,
    )
    assembler = PromptAssembler(
        prompt_loader,
        settings,
        response_declaration=chat_compactor.available,
    )
    # Shares the docs pool rather than opening a third: a trace upload is a small
    # request to a different host on the same latency profile, and a pool per
    # concern costs connections nothing here is short of.
    trace_recorder = _build_trace_recorder(settings, docs_http_client)
    orchestrator = ChatOrchestrator(
        settings=settings,
        catalog_manager=catalog_manager,
        selector=selector,
        state_codec=state_codec,
        assembler=assembler,
        provider=chat_provider,
        docs_client=docs_client,
        compactor=chat_compactor,
        trace_recorder=trace_recorder,
        fast_response=fast_response,
    )
    task_runner = TaskRunner(
        settings=settings,
        provider=chat_provider,
        prompt_loader=prompt_loader,
        catalog_manager=catalog_manager,
        openrouter_provider=openrouter_provider,
    )

    return Container(
        settings=settings,
        prompt_loader=prompt_loader,
        catalog_manager=catalog_manager,
        provider=chat_provider,
        openrouter_provider=openrouter_provider,
        state_codec=state_codec,
        selector=selector,
        assembler=assembler,
        orchestrator=orchestrator,
        task_runner=task_runner,
        compactor=chat_compactor,
        trace_recorder=trace_recorder,
        fast_response_provider=fast_response_provider,
        docs_http_client=docs_http_client,
        inference_pool=inference_pool,
    )


async def _build_token_verifier(
    settings: Settings,
) -> tuple[Any, httpx.AsyncClient | None]:
    """The account-token verifier, or ``None`` when this deployment has no OAuth.

    Built OUTSIDE the container, and that separation is load-bearing rather than
    tidy. ``/v1/daintree/doctor`` deliberately answers when the container failed to
    build — a doctor that goes down with the patient cannot diagnose the outage a
    tester is calling about — and it is an authenticated route. A verifier living
    on the container would therefore be unavailable in exactly the state the
    diagnostic exists for, so an operator investigating a bad upstream key would
    find they could not authenticate to ask about it.
    """
    if settings.auth_mode == "open" or not settings.auth_is_configured:
        return None, None
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(5.0, connect=2.0),
        # Never follow a redirect to another host for signing keys. Set here as
        # well as at the call site: two locks on a door that opens onto key
        # material.
        follow_redirects=False,
    )
    try:
        verifier = SupabaseTokenVerifier(
            issuer=settings.supabase_auth_issuer,
            allowed_client_ids=frozenset({settings.supabase_oauth_client_id}),
            jwks=JwksCache(url=settings.supabase_jwks_url, client=client),
        )
    except Exception:
        # The client exists and nothing else holds a reference to it yet, so an
        # exception from here on would strand an open connection pool for the life
        # of the process. Closed on the way out rather than left to the caller,
        # which has no way to know one was created.
        await client.aclose()
        raise
    return verifier, client


def _refuse_cookies(client: httpx.AsyncClient) -> None:
    """Make ``client`` store no cookies and send none.

    Applied to the client's OWN jar after construction, which is the only place it
    works: ``AsyncClient(cookies=...)`` builds a fresh ``Cookies`` and copies the
    values across, so a policy set on a jar passed in is silently discarded.

    ``httpx.Cookies`` wraps a stdlib ``CookieJar``, and a policy allowing no domain
    at all makes both ``set_ok`` and ``return_ok`` false for everything — which is
    the smallest honest way to say "this client has no cookie behaviour".
    """
    client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))


async def _build_entitlement_service(settings: Settings) -> EntitlementService | None:
    """The billing-authority client, or ``None`` when no URL is configured.

    Built beside the token verifier and OUTSIDE the container, for the same reason
    it is: both answer questions about the CALLER, and both are needed by routes
    that must keep working when the model-serving half of the process did not
    build. ``GET /v1/daintree/account`` is exactly such a route — "what does my
    account own?" is a question worth answering during an upstream outage, not one
    to take down with it.

    ``None`` is a NORMAL configuration rather than a degraded one: it is every
    deployment running the identity-only rollout, and it is what makes the account
    endpoint report ``access: unverified`` instead of failing.
    """
    if not settings.entitlement_is_configured:
        return None
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            ENTITLEMENT_TIMEOUT_SECONDS, connect=ENTITLEMENT_CONNECT_TIMEOUT_SECONDS
        ),
        # Two locks on the door, matching `_build_token_verifier`: the call site
        # sets this too, and this call forwards the CALLER's account credential.
        follow_redirects=False,
        # No ambient proxy. The URL is pinned to a Daintree host in configuration,
        # and an environment variable that could route the caller's bearer through
        # a third party would undo that pin without changing the configuration
        # anybody reads. It also keeps a loopback development URL from being sent
        # to a corporate proxy that cannot reach it. (It also disables ambient
        # `SSL_CERT_FILE`/`SSL_CERT_DIR`; a custom CA here would need an explicit
        # SSL context rather than re-enabling environment trust.)
        trust_env=False,
    )
    # No cookies, ever. This client is LONG-LIVED and shared by every caller, so a
    # `Set-Cookie` from the website while checking one account would be retained
    # and sent alongside the NEXT caller's bearer — one person's session state
    # travelling with another person's credential. Clearing the jar per request
    # would race under concurrency; refusing to store is not a race at all.
    _refuse_cookies(client)
    try:
        return EntitlementService(client, url=settings.daintree_entitlement_url)
    except Exception:
        # The client exists and nothing else holds a reference to it yet, so an
        # exception from here would strand an open connection pool for the life of
        # the process. Same ownership rule as `_build_token_verifier` above.
        await client.aclose()
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    settings: Settings = app.state.settings
    injected: ChatProvider | None = getattr(app.state, "injected_provider", None)
    injected_selector: RunbookSelector | None = getattr(app.state, "injected_selector", None)
    injected_compactor: Compactor | None = getattr(app.state, "injected_compactor", None)
    # Before the container, and outside its try: a container that fails to build
    # must not take authentication down with it (see `_build_token_verifier`).
    #
    # Guarded by a try of its OWN rather than sharing the container's, because
    # this runs before the `yield` and an escape here would fail the whole
    # lifespan — turning a configuration problem into a dead process, which is
    # precisely the posture every other startup failure in this file avoids
    # ("readiness must not crash liveness"). Degrading to no verifier is safe and
    # already exercised: under `enforce` every request then answers
    # `auth_dependency_unavailable`, which is the honest verdict for a door that
    # could not be built.
    try:
        verifier, jwks_client = await _build_token_verifier(settings)
    except Exception as exc:  # noqa: BLE001 - see above
        logger.error("auth_verifier_build_failed", error=str(exc))
        verifier, jwks_client = None, None
    app.state.token_verifier = verifier
    app.state.jwks_client = jwks_client
    # Guarded like the verifier above and for the same reason: a configuration
    # problem here must not take the process down. Absent is a NORMAL state while
    # entitlement is off — it is every deployment running the identity-only
    # rollout, and it is what makes the account endpoint report `unverified`.
    try:
        app.state.entitlement_service = await _build_entitlement_service(settings)
    except Exception as exc:  # noqa: BLE001 - see above
        logger.error("entitlement_service_build_failed", error=str(exc))
        app.state.entitlement_service = None
    try:
        # Local-development-only features (runbook learning) must never be enabled in
        # production; checked always, even with an injected provider.
        problems = list(settings.local_only_problems())
        if injected is None:
            problems.extend(settings.missing_secrets() + settings.weak_secrets())
        if settings.entitlement_mode != "off" and app.state.entitlement_service is None:
            # A binding mode with no client is refused HERE rather than left to
            # answer `entitlement_unavailable` on every paid request. The
            # per-request refusal is correct and fails closed, but a revision that
            # reports itself ready while refusing every turn is the shape of
            # outage that takes longest to diagnose — `/readyz` is the one place
            # that can say "do not send me traffic". Static validation already
            # refuses a bad URL (`entitlement_problems`); this covers the runtime
            # half, where the values were fine and the client still did not build.
            problems.append(
                f"ENTITLEMENT_MODE={settings.entitlement_mode} requires a usable "
                "DAINTREE_ENTITLEMENT_URL (no entitlement client could be built)"
            )
        if problems:
            raise RuntimeError(f"configuration problems: {', '.join(problems)}")
        container = build_container(
            settings, injected, injected_selector, injected_compactor
        )
        app.state.container = container
        app.state.ready = True
        logger.info(
            "startup_ready",
            environment=settings.environment,
            catalog_revision=container.catalog.revision,
            runbooks=len(container.catalog),
            tasks=container.task_runner.task_ids(),
        )
    except Exception as exc:  # noqa: BLE001 - readiness must not crash liveness
        app.state.container = None
        app.state.ready = False
        app.state.ready_error = str(exc)
        logger.error("startup_failed", error=str(exc))
    try:
        yield
    finally:
        final_container: Container | None = getattr(app.state, "container", None)
        if final_container is not None:
            await final_container.aclose()
        final_jwks_client: httpx.AsyncClient | None = getattr(app.state, "jwks_client", None)
        if final_jwks_client is not None:
            await final_jwks_client.aclose()
        final_entitlement: EntitlementService | None = getattr(
            app.state, "entitlement_service", None
        )
        if final_entitlement is not None:
            with contextlib.suppress(Exception):  # pragma: no cover - best-effort close
                await final_entitlement.aclose()


def create_app(
    *,
    settings: Settings | None = None,
    provider: ChatProvider | None = None,
    selector: RunbookSelector | None = None,
    compactor: Compactor | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Daintree Assistant Server",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.injected_provider = provider
    app.state.injected_selector = selector
    app.state.injected_compactor = compactor
    app.state.ready = False
    app.state.ready_error = None
    # Defined here as well as in lifespan so a request served by an app that never
    # entered one (the tests do this deliberately) reads `None` rather than raising
    # AttributeError on the auth path.
    app.state.token_verifier = None
    app.state.jwks_client = None
    app.state.entitlement_service = None

    register_exception_handlers(app)

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        supplied_ids = request.headers.getlist("x-request-id")
        request_id = _safe_request_id(
            # Exactly one, or mint. See BodySizeLimitMiddleware._reject: two
            # inbound ids are an ambiguity, and resolving it by picking one would
            # let our logs disagree with an intermediary's about which id names
            # this request.
            supplied_ids[0] if len(supplied_ids) == 1 else None,
            # `getlist`, not `get`: the latter returns only the first of two
            # `Authorization` headers, and a token in the second would then be
            # invisible to the credential-overlap check.
            request.headers.getlist("authorization"),
        )
        # One request identity for the whole turn: handlers read it back through
        # api.deps.request_id() so logs, response headers, SSE meta and SSE error
        # never diverge. It rides request.state (i.e. the ASGI scope), which
        # outlives this middleware's own frame — the outer 500 handler reads it
        # from there after this one has unwound.
        request.state.request_id = request_id
        bind_request_context(request_id=request_id, path=request.url.path, method=request.method)
        try:
            response = await call_next(request)
            # Assigned, NOT setdefault. This middleware OWNS the header; a route
            # that sets its own would win a setdefault and hand the caller an id
            # absent from every log line, which is exactly the bug /tasks shipped.
            # Making the assignment unconditional turns that from a convention
            # route authors must remember into something they cannot get wrong.
            response.headers["X-Request-Id"] = request_id
            return response
        finally:
            clear_request_context()

    # Outermost: count body bytes (handles chunked / missing Content-Length).
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)

    app.include_router(root.router)
    app.include_router(health.router)
    app.include_router(daintree_respond.router)
    app.include_router(daintree_capabilities.router)
    app.include_router(daintree_auth.router)
    app.include_router(daintree_account.router)
    # Deliberately NOT behind require_ready: a doctor that goes down with the
    # patient cannot diagnose the outage a tester is calling about.
    app.include_router(daintree_doctor.router)
    app.include_router(daintree_runbooks.router)
    # The overlay reload trigger is mounted ONLY for local runbook authoring. It
    # mutates process state, and `require_auth` is not the boundary that stops it:
    # under the shipped `open` posture every caller is the anonymous principal
    # holding every permission, and `runbooks:write` deliberately does not exist.
    # So rather than ship a route production has to be trusted not to call,
    # production simply does not have it — with no overlay configured, or in a hardened
    # environment, the URL 404s. (A hardened deploy that sets the variable at
    # all also fails readiness; this is the second lock on the same door.)
    if settings.runbooks_overlay_dir and not settings.is_hardened:
        app.include_router(daintree_runbooks.reload_router)
    app.include_router(daintree_tasks.router)

    return app


app = create_app()
