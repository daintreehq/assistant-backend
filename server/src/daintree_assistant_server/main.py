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
from typing import Any

import httpx
from fastapi import FastAPI, Request

from . import __version__
from .api import (
    daintree_capabilities,
    daintree_skills,
    daintree_tasks,
    health,
    openai_chat,
    openai_models,
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
from .providers.deepseek import DeepSeekProvider
from .services.chat_orchestrator import ChatOrchestrator
from .services.prompt_assembler import PromptAssembler
from .services.task_runner import TaskRunner
from .skills.catalog import load_catalog
from .skills.selector import Selector
from .state.codec import StateCodec

logger = get_logger("daintree.main")


@dataclass
class Container:
    """All long-lived service components."""

    settings: Settings
    prompt_loader: PromptLoader
    catalog: Any
    provider: ChatProvider
    state_codec: StateCodec
    selector: Selector
    assembler: PromptAssembler
    orchestrator: ChatOrchestrator
    task_runner: TaskRunner


def _build_provider(settings: Settings) -> ChatProvider:
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            settings.main_timeout_seconds,
            connect=settings.upstream_connect_timeout_seconds,
        ),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    return DeepSeekProvider(
        client,
        api_key=settings.deepseek_api_key.get_secret_value(),
        base_url=settings.deepseek_base_url,
        default_timeout=settings.main_timeout_seconds,
        send_prompt_cache_key=settings.deepseek_send_prompt_cache_key,
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
    catalog = load_catalog()
    state_codec = _build_state_codec(settings)
    chat_provider = provider if provider is not None else _build_provider(settings)
    selector = Selector(chat_provider, prompt_loader, catalog, settings)
    assembler = PromptAssembler(prompt_loader, settings)
    orchestrator = ChatOrchestrator(
        settings=settings,
        catalog=catalog,
        selector=selector,
        state_codec=state_codec,
        assembler=assembler,
        provider=chat_provider,
    )
    task_runner = TaskRunner(settings=settings, provider=chat_provider, prompt_loader=prompt_loader)
    return Container(
        settings=settings,
        prompt_loader=prompt_loader,
        catalog=catalog,
        provider=chat_provider,
        state_codec=state_codec,
        selector=selector,
        assembler=assembler,
        orchestrator=orchestrator,
        task_runner=task_runner,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    settings: Settings = app.state.settings
    injected: ChatProvider | None = getattr(app.state, "injected_provider", None)
    try:
        if injected is None:
            missing = settings.missing_secrets()
            if missing:
                raise RuntimeError(f"missing required secrets: {', '.join(missing)}")
        container = build_container(settings, injected)
        app.state.container = container
        app.state.ready = True
        logger.info(
            "startup_ready",
            environment=settings.environment,
            catalog_revision=container.catalog.revision,
            skills=len(container.catalog),
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
            # Reject oversized bodies up front via Content-Length when available.
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    if int(content_length) > settings.max_request_bytes:
                        return request_too_large(
                            "Request body exceeds the configured limit."
                        ).to_response()
                except ValueError:
                    pass
            response = await call_next(request)
            response.headers.setdefault("X-Request-Id", request_id)
            return response
        finally:
            clear_request_context()

    app.include_router(health.router)
    app.include_router(openai_models.router)
    app.include_router(openai_chat.router)
    app.include_router(daintree_capabilities.router)
    app.include_router(daintree_skills.router)
    app.include_router(daintree_tasks.router)

    return app


app = create_app()
