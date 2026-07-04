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
    daintree_capabilities,
    daintree_respond,
    daintree_skills,
    daintree_tasks,
    health,
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
from .providers.fireworks import FireworksProvider
from .services.chat_orchestrator import ChatOrchestrator
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
        await response(scope, _eat, send)


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

    @property
    def catalog(self) -> Catalog:
        """The current catalog (live: the skill-learning harness may reload it)."""
        return self.catalog_manager.catalog


def _build_provider(settings: Settings) -> ChatProvider:
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.main_timeout_seconds,
            connect=settings.upstream_connect_timeout_seconds,
        ),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    return FireworksProvider(
        client,
        api_key=settings.fireworks_api_key.get_secret_value(),
        base_url=settings.fireworks_base_url,
        default_timeout=settings.main_timeout_seconds,
        send_prompt_cache_key=settings.fireworks_send_prompt_cache_key,
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
        skill_learner = SkillLearner(
            settings=settings,
            provider=chat_provider,
            prompt_loader=prompt_loader,
            catalog_manager=catalog_manager,
        )

    orchestrator = ChatOrchestrator(
        settings=settings,
        catalog_manager=catalog_manager,
        selector=selector,
        state_codec=state_codec,
        assembler=assembler,
        provider=chat_provider,
        learner=skill_learner,
    )
    task_runner = TaskRunner(settings=settings, provider=chat_provider, prompt_loader=prompt_loader)
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
            secret_problems = settings.missing_secrets() + settings.weak_secrets()
            if settings.environment != "production":
                # Local dev: a missing inbound API key just enables unauthenticated
                # access; it must not block the server from booting.
                secret_problems = [p for p in secret_problems if p != "DAINTREE_API_KEY"]
            problems.extend(secret_problems)
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
            with contextlib.suppress(Exception):  # pragma: no cover - best-effort drain
                # Let any in-flight fire-and-forget learning finish (its write+reload
                # is cancellation-shielded) before the provider it uses is torn down.
                await final_container.orchestrator.drain_background_tasks()
            with contextlib.suppress(Exception):  # pragma: no cover - best-effort close
                await final_container.provider.aclose()


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
        request_id = request.headers.get("x-request-id") or new_request_id()
        bind_request_context(request_id=request_id, path=request.url.path, method=request.method)
        try:
            response = await call_next(request)
            response.headers.setdefault("X-Request-Id", request_id)
            return response
        finally:
            clear_request_context()

    # Outermost: count body bytes (handles chunked / missing Content-Length).
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)

    app.include_router(health.router)
    app.include_router(daintree_respond.router)
    app.include_router(daintree_capabilities.router)
    app.include_router(daintree_skills.router)
    app.include_router(daintree_tasks.router)

    return app


app = create_app()
