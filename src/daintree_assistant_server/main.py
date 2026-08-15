"""FastAPI application factory and lifespan.

Builds the immutable service container (settings, prompts, catalog, provider,
state codec, selector, assembler, orchestrator, task runner), validates
readiness on startup, and tears the provider down on shutdown. A malformed
prompt or skill, or a missing required secret, prevents the app from becoming
ready (``/readyz`` returns 503) without crashing liveness.
"""

from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request

from . import __version__
from .api import (
    daintree_auth,
    daintree_capabilities,
    daintree_doctor,
    daintree_respond,
    daintree_skills,
    daintree_tasks,
    health,
    root,
)
from .config import Settings, get_settings
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
from .providers.openrouter import OpenRouterProvider
from .services.chat_orchestrator import ChatOrchestrator
from .services.docs_mcp import DOCS_LOOKUP_TIMEOUT_SECONDS, DocsMcpClient
from .services.prompt_assembler import PromptAssembler
from .services.skill_learning import SkillLearner
from .services.task_runner import TaskRunner
from .skills.catalog import Catalog, CatalogManager, default_skills_dir, load_catalog
from .skills.selector import Selector
from .state.codec import StateCodec

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
        headers = dict(scope.get("headers") or [])
        try:
            inbound = headers.get(b"x-request-id", b"").decode("ascii")
            authorization = headers.get(b"authorization", b"").decode("ascii")
        except UnicodeDecodeError:  # pragma: no cover - defensive
            inbound, authorization = "", ""
        response.headers.setdefault(
            "X-Request-Id", _safe_request_id(inbound, authorization)
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


def _safe_request_id(inbound: str | None, authorization: str | None) -> str:
    """Adopt the caller's correlation id, or mint one when it isn't safe to.

    Honoring an inbound id is genuinely useful — it is how a CLI ties its own
    logs to ours — but it is also an unvalidated caller string headed straight
    for our log pipeline. Three ways it is refused:

    * **Shape.** Bounded length and a conservative charset, so an id cannot smuggle
      newlines, control characters or a kilobyte of padding into a log line.
    * **Credential overlap.** A bearer token is header-safe ASCII, which means a
      caller's own OpenRouter key is a *structurally valid* request id. Somebody
      scripting against this API can paste one into the wrong variable, and we
      would faithfully log and echo their spend-authorizing credential. Checked
      both directions, since a prefix is as damaging as the whole value.
    * **Emptiness.**

    Refusal is silent and non-fatal: we simply mint our own. Rejecting the request
    would turn a harmless client mistake into an outage, and the caller's id has
    no authority here anyway.
    """
    if not inbound:
        return new_request_id()
    candidate = inbound.strip()
    if not candidate or len(candidate) > _MAX_REQUEST_ID_LENGTH:
        return new_request_id()
    if not all(ch in _REQUEST_ID_SAFE for ch in candidate):
        return new_request_id()
    token = _bearer_token(authorization)
    if token:
        # Containing the whole token is damaging at any length. The reverse — the
        # id being a fragment OF the token — only matters once the fragment is long
        # enough to be credential material rather than a coincidence.
        if token in candidate:
            return new_request_id()
        if len(candidate) >= _MIN_CREDENTIAL_FRAGMENT and candidate in token:
            return new_request_id()
    return candidate


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


@dataclass
class Container:
    """All long-lived service components."""

    settings: Settings
    prompt_loader: PromptLoader
    catalog_manager: CatalogManager
    provider: ChatProvider
    state_codec: StateCodec
    selector: Selector
    assembler: PromptAssembler
    orchestrator: ChatOrchestrator
    task_runner: TaskRunner
    skill_learner: SkillLearner | None = None
    # Its own httpx client, kept here so lifespan can close it. Deliberately not
    # the provider's: the docs server is a different host with a different
    # latency profile, and a documentation search must never queue behind a
    # long-lived streaming completion holding a pooled connection.
    docs_http_client: httpx.AsyncClient | None = None

    @property
    def catalog(self) -> Catalog:
        """The current catalog (live: the skill-learning harness may reload it)."""
        return self.catalog_manager.catalog

    async def aclose(self) -> None:
        """Drain in-flight background work, then close the providers.

        Best-effort teardown usable by any owner (the app lifespan, or a direct
        ``build_container`` caller). There is one provider to close: skill learning
        rides the same OpenRouter client, differing only in the model it names.
        """
        with contextlib.suppress(Exception):  # pragma: no cover - best-effort drain
            # Let any in-flight fire-and-forget learning finish (its write+reload is
            # cancellation-shielded) before the provider it uses is torn down.
            await self.orchestrator.drain_background_tasks()
        with contextlib.suppress(Exception):  # pragma: no cover - best-effort close
            await self.provider.aclose()
        if self.docs_http_client is not None:
            with contextlib.suppress(Exception):  # pragma: no cover - best-effort close
                await self.docs_http_client.aclose()


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


def _build_provider(settings: Settings) -> ChatProvider:
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=settings.upstream_connect_timeout_seconds,
            read=settings.main_timeout_seconds,
            write=_UPSTREAM_WRITE_TIMEOUT_SECONDS,
            pool=_UPSTREAM_POOL_TIMEOUT_SECONDS,
        ),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    return OpenRouterProvider(
        client,
        # NO default credential — the server holds no upstream key. Every provider
        # call must carry the caller's own key (their bearer token, threaded as
        # ProviderChatRequest.api_key); a call that arrives without one fails
        # loudly in _resolve_key instead of quietly billing a shared account.
        # Standalone tooling (benchmarks, live tests) may still construct a
        # provider WITH a default; the serving path deliberately does not.
        api_key=None,
        base_url=settings.openrouter_base_url,
        default_timeout=settings.main_timeout_seconds,
        # send_reasoning stays at its True default: a reasoning model thinks when
        # the field is absent, and the explicit per-call directive is what keeps
        # the non-thinking selector actually non-thinking.
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


def build_container(settings: Settings, provider: ChatProvider | None = None) -> Container:
    """Construct and validate all service components."""
    prompt_loader = PromptLoader()
    prompt_loader.validate()

    # The catalog loads the packaged skills, plus an optional local overlay when the
    # skill-learning harness is configured to write to a separate directory (in the
    # default edit-in-place mode there is no overlay). A CatalogManager holds it so a
    # learning write can reload it in place.
    primary_dir = default_skills_dir()
    learn_dir = Path(settings.skill_learning_dir) if settings.skill_learning_dir else None
    overlay_dir = (
        learn_dir
        if (learn_dir is not None and learn_dir.resolve() != primary_dir.resolve())
        else None
    )
    catalog = load_catalog(primary_dir, overlay_dir)
    catalog_manager = CatalogManager(catalog, primary_dir=primary_dir, overlay_dir=overlay_dir)

    state_codec = _build_state_codec(settings)
    chat_provider = provider if provider is not None else _build_provider(settings)
    selector = Selector(chat_provider, prompt_loader, catalog_manager, settings)
    assembler = PromptAssembler(prompt_loader, settings)

    skill_learner: SkillLearner | None = None
    if settings.skill_learning_enabled:
        # Build/improve run on a stronger model (SKILL_BUILDER_MODEL) than the gate,
        # but on the SAME provider — OpenRouter fronts both, so the split is a model
        # id, not a second client or a second credential.
        skill_learner = SkillLearner(
            settings=settings,
            provider=chat_provider,
            prompt_loader=prompt_loader,
            catalog_manager=catalog_manager,
        )

    docs_http_client, docs_client = _build_docs_client()
    orchestrator = ChatOrchestrator(
        settings=settings,
        catalog_manager=catalog_manager,
        selector=selector,
        state_codec=state_codec,
        assembler=assembler,
        provider=chat_provider,
        learner=skill_learner,
        docs_client=docs_client,
    )
    task_runner = TaskRunner(
        settings=settings,
        provider=chat_provider,
        prompt_loader=prompt_loader,
        catalog_manager=catalog_manager,
    )

    return Container(
        settings=settings,
        prompt_loader=prompt_loader,
        catalog_manager=catalog_manager,
        provider=chat_provider,
        state_codec=state_codec,
        selector=selector,
        assembler=assembler,
        orchestrator=orchestrator,
        task_runner=task_runner,
        skill_learner=skill_learner,
        docs_http_client=docs_http_client,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    settings: Settings = app.state.settings
    injected: ChatProvider | None = getattr(app.state, "injected_provider", None)
    try:
        # Local-development-only features (skill learning) must never be enabled in
        # production; checked always, even with an injected provider.
        problems = list(settings.local_only_problems())
        if injected is None:
            problems.extend(settings.missing_secrets() + settings.weak_secrets())
        if problems:
            raise RuntimeError(f"configuration problems: {', '.join(problems)}")
        container = build_container(settings, injected)
        app.state.container = container
        app.state.ready = True
        logger.info(
            "startup_ready",
            environment=settings.environment,
            catalog_revision=container.catalog.revision,
            skills=len(container.catalog),
            tasks=container.task_runner.task_ids(),
            skill_learning_mode=settings.skill_learning_mode,
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


def create_app(
    *, settings: Settings | None = None, provider: ChatProvider | None = None
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
    app.state.ready = False
    app.state.ready_error = None

    register_exception_handlers(app)

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = _safe_request_id(
            request.headers.get("x-request-id"), request.headers.get("authorization")
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
    # Deliberately NOT behind require_ready: a doctor that goes down with the
    # patient cannot diagnose the outage a tester is calling about.
    app.include_router(daintree_doctor.router)
    app.include_router(daintree_skills.router)
    app.include_router(daintree_tasks.router)

    return app


app = create_app()
