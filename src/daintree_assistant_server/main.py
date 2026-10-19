"""FastAPI application factory and lifespan.

Builds the catalog and the local selector on startup. A malformed runbook or a
missing selector artifact prevents readiness (``/readyz`` returns 503) without
crashing liveness, so Cloud Run holds traffic off a broken revision instead of
restart-looping it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from . import __version__
from .api import health, mcp, root
from .config import Settings, get_settings
from .mcp_sessions import SessionMemory
from .observability import (
    bind_request_context,
    clear_request_context,
    configure_logging,
    get_logger,
    new_request_id,
)
from .runbooks.catalog import Catalogs, load_catalogs
from .runbooks.host_state import HostModel
from .runbooks.local.paths_local import selector_artifact_dir
from .runbooks.local.runtime import InferencePool
from .runbooks.local.selector import LocalSelector, RunbookSelector
from .runbooks.local.setwise import SetwiseClassifier

logger = get_logger("daintree.main")


class BodySizeLimitMiddleware:
    """Reject oversized request bodies by counting bytes as they arrive.

    ``Content-Length`` is honoured up front, and the limit is also enforced while
    reading, so a chunked request (or one that omits the header) cannot push an
    unbounded body into the JSON parser. The buffered body is then replayed to
    the application unchanged.
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
                    await self._reject(scope, receive, send)
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
                await self._reject(scope, receive, send)
                return

        replayed = False

        async def replay_receive() -> Any:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _reject(self, scope: Any, receive: Any, send: Any) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Request body is too large."},
            },
        )
        await response(scope, receive, send)


# An inbound X-Request-Id is adopted only when it is short and plain: it is a
# caller-controlled string copied into every log line for the request.
_MAX_REQUEST_ID_LENGTH = 128
_REQUEST_ID_SAFE = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
)


def _safe_request_id(inbound: str | None) -> str:
    candidate = (inbound or "").strip()
    if (
        not candidate
        or len(candidate) > _MAX_REQUEST_ID_LENGTH
        or not all(ch in _REQUEST_ID_SAFE for ch in candidate)
    ):
        return new_request_id()
    return candidate


@dataclass
class Container:
    """The long-lived service components."""

    settings: Settings
    catalogs: Catalogs
    #: One search per host model's catalog, over one classifier and pool.
    selectors: dict[HostModel, RunbookSelector]
    pool: InferencePool | None = None
    sessions: SessionMemory = field(default_factory=SessionMemory)

    def selector_for(self, model: HostModel) -> RunbookSelector:
        return self.selectors[model]

    def close(self) -> None:
        if self.pool is not None:
            self.pool.close()


def build_container(settings: Settings, selector: RunbookSelector | None = None) -> Container:
    """Load both catalogs and the selector.

    ``selector`` is the test seam: a scripted selector skips loading the ONNX
    artifact entirely, and answers for both catalogs. Production leaves it None and
    gets the local classifier, shared by a search bound to each catalog.
    """
    catalogs = load_catalogs()
    pool: InferencePool | None = None
    if selector is not None:
        return Container(
            settings=settings,
            catalogs=catalogs,
            selectors={"control": selector, "inbox": selector},
        )
    # Constructed eagerly so a missing or LFS-pointer artifact fails readiness
    # rather than the first request.
    classifier = SetwiseClassifier(selector_artifact_dir())
    # Embed both catalogs now: it validates the artifact against real catalogs at
    # startup, and no caller's first search pays for the embedding.
    classifier.sync(catalogs.inbox)
    classifier.sync(catalogs.control)
    pool = InferencePool()
    selectors: dict[HostModel, RunbookSelector] = {
        "control": LocalSelector(classifier, pool, catalogs.control),
        "inbox": LocalSelector(classifier, pool, catalogs.inbox),
    }
    return Container(settings=settings, catalogs=catalogs, selectors=selectors, pool=pool)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    injected: RunbookSelector | None = app.state.injected_selector
    try:
        container = build_container(settings, injected)
        app.state.container = container
        app.state.ready = True
        logger.info(
            "startup_ready",
            environment=settings.environment,
            catalog_revision=container.catalogs.control.revision,
            runbooks=len(container.catalogs.control),
            inbox_catalog_revision=container.catalogs.inbox.revision,
            inbox_runbooks=len(container.catalogs.inbox),
        )
    except Exception as exc:  # noqa: BLE001 - readiness must not crash liveness
        app.state.container = None
        app.state.ready = False
        app.state.ready_error = str(exc)
        logger.error("startup_failed", error=str(exc))
    try:
        yield
    finally:
        final: Container | None = getattr(app.state, "container", None)
        if final is not None:
            final.close()


def create_app(
    *,
    settings: Settings | None = None,
    selector: RunbookSelector | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title="Daintree Runbooks",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.injected_selector = selector
    app.state.container = None
    app.state.ready = False
    app.state.ready_error = None

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        supplied = request.headers.getlist("x-request-id")
        # Exactly one, or mint: two inbound ids are an ambiguity, and picking one
        # would let our logs disagree with an intermediary's.
        request_id = _safe_request_id(supplied[0] if len(supplied) == 1 else None)
        request.state.request_id = request_id
        bind_request_context(request_id=request_id, path=request.url.path, method=request.method)
        try:
            response = await call_next(request)
            response.headers["X-Request-Id"] = request_id
            return response
        finally:
            clear_request_context()

    # Outermost: count body bytes (handles chunked / missing Content-Length).
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)

    app.include_router(root.router)
    app.include_router(health.router)
    app.include_router(mcp.router)
    return app


app = create_app()
