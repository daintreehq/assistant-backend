"""Shared pytest fixtures."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest
from fakes import FakeProvider, ScriptedSelector
from fastapi.testclient import TestClient

from daintree_assistant_server import PROTOCOL_VERSION
from daintree_assistant_server.config import Settings
from daintree_assistant_server.main import build_container, create_app
from daintree_assistant_server.prompts.loader import PromptLoader
from daintree_assistant_server.runbooks.catalog import (
    CatalogManager,
    default_runbooks_dir,
    load_catalog,
)
from daintree_assistant_server.services.chat_orchestrator import ChatOrchestrator
from daintree_assistant_server.services.prompt_assembler import PromptAssembler
from daintree_assistant_server.services.task_runner import TaskRunner
from daintree_assistant_server.state.codec import StateCodec

API_KEY = "test-api-key"


@pytest.fixture(autouse=True, scope="session")
def _isolate_from_dotenv() -> Iterator[None]:
    """Tests must not read a developer's local ``.env``: it would override any
    Settings field not explicitly passed and break test isolation.

    The real process environment is the same hazard by a shorter route, and the
    local-development-only paths are the ones a developer actually exports —
    ``DAINTREE_RUNBOOKS_OVERLAY_DIR`` is set for as long as they are authoring a
    runbook. A test asserting a field is unset by default must not fail because
    the person running it happened to be using the feature.
    """
    previous = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    exported = {
        name: os.environ.pop(name)
        for name in ("DAINTREE_RUNBOOKS_OVERLAY_DIR", "DAINTREE_DUMP_UPSTREAM_DIR")
        if name in os.environ
    }
    yield
    Settings.model_config["env_file"] = previous
    os.environ.update(exported)


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    return Settings(
        # The provider credential, EXPLICITLY. Leaving it out meant it fell
        # through to whatever `.env` held — so a developer with a real
        # TOGETHER_API_KEY ran the redaction tests against their live secret and
        # CI, having no `.env`, ran them against the empty string, where
        # `assert key not in body` is unsatisfiable rather than merely weak.
        # A sentinel here makes both environments run the same test.
        TOGETHER_API_KEY="tgp_v1_test-key",
        # Required for readiness since the fast-response stage lost its off
        # switch — the same reasoning as the executor key above, and a sentinel
        # rather than a real key for the same redaction reason.
        CEREBRAS_API_KEY="csk-test-key",
        STATE_SIGNING_KEY_CURRENT="test-signing-key",
        STATE_SIGNING_KID="k1",
        STATE_SALT="test-salt",
        RETRY_BACKOFF_BASE_SECONDS=0.0,
    )


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture
def fake_provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def prompt_loader() -> PromptLoader:
    loader = PromptLoader()
    loader.validate()
    return loader


@pytest.fixture
def catalog() -> Any:
    return load_catalog()


@pytest.fixture
def catalog_manager() -> CatalogManager:
    primary = default_runbooks_dir()
    return CatalogManager(load_catalog(primary), primary_dir=primary)


@pytest.fixture
def state_codec(settings: Settings) -> StateCodec:
    return StateCodec(
        current_key=settings.state_signing_key_current.get_secret_value().encode(),
        current_kid=settings.state_signing_kid,
        salt=settings.state_salt.get_secret_value().encode(),
        ttl_seconds=settings.state_token_ttl_seconds,
    )


@pytest.fixture
def selector(
    fake_provider: FakeProvider,
    catalog_manager: CatalogManager,
    settings: Settings,
) -> ScriptedSelector:
    """The scriptable seam, over the test catalog fixture.

    Distinct from ``scripted_selector`` below only in which catalog it reads:
    this one takes the test's ``catalog_manager`` fixture, that one builds its
    own from the packaged runbooks for the paths that go through
    ``build_container``.
    """
    return ScriptedSelector(fake_provider, catalog_manager, settings)


@pytest.fixture
def assembler(prompt_loader: PromptLoader, settings: Settings) -> PromptAssembler:
    return PromptAssembler(prompt_loader, settings)


@pytest.fixture
def orchestrator(
    settings: Settings,
    catalog_manager: CatalogManager,
    selector: ScriptedSelector,
    state_codec: StateCodec,
    assembler: PromptAssembler,
    fake_provider: FakeProvider,
) -> ChatOrchestrator:
    return ChatOrchestrator(
        settings=settings,
        catalog_manager=catalog_manager,
        selector=selector,
        state_codec=state_codec,
        assembler=assembler,
        provider=fake_provider,
    )


@pytest.fixture
def task_runner(
    settings: Settings,
    fake_provider: FakeProvider,
    prompt_loader: PromptLoader,
    catalog_manager: CatalogManager,
) -> TaskRunner:
    return TaskRunner(
        settings=settings,
        provider=fake_provider,
        prompt_loader=prompt_loader,
        catalog_manager=catalog_manager,
    )


def scripted_selector(fake_provider: FakeProvider, settings: Settings) -> ScriptedSelector:
    """The scripted selection seam, injected at CONSTRUCTION time.

    Production selection is a local two-stage ONNX selector — it decides for
    real, which is exactly wrong for orchestration tests that need to SCRIPT the
    selection ("returns these ids", "fails open", "caps at three") to drive a
    specific orchestrator branch. `ScriptedSelector` (tests/fakes.py) reads
    ``FakeProvider``'s ``selection_result``/``selection_error`` and records
    ``selection_requests``, which is the interface ninety-odd assertions in this
    suite are written against.

    It used to be the prompt-based `Selector` from the shipped package, kept
    alive there for this fixture alone. That class is gone — an LLM selector is
    slower and more expensive than the local one on every axis measured, so
    there was no product left for it to be a seam FOR, and a billed-model-call
    implementation living in `src/` to serve a test fixture is the wrong trade.

    Passed INTO build_container/create_app (never swapped in afterwards) so the
    fixtures skip loading the ONNX graphs entirely — a fresh clone without LFS
    content runs the suite, and only the explicitly artifact-marked tests skip.
    The LocalSelector's own contract is tested in tests/unit/test_local_selection.py.
    """
    return ScriptedSelector(
        fake_provider,
        CatalogManager(load_catalog(default_runbooks_dir()), primary_dir=default_runbooks_dir()),
        settings,
    )


def detach_docs_client(container: Any) -> None:
    """Cut a container's link to the LIVE documentation server.

    `build_container` wires a real `DocsMcpClient` pointed at daintree.org, so a
    test whose selector stub happens to return a `docs_query` would make a real
    outbound call from the suite. Nothing today does, which is exactly why it
    needs a guard rather than vigilance. Tests that exercise the lookup inject
    their own client over a MockTransport (see tests/unit/test_docs_lookup.py).

    Detaching also means the client is never used, so there is no pool to close.
    """
    container.orchestrator.docs_client = None


@pytest.fixture
def app(settings: Settings, fake_provider: FakeProvider) -> Any:
    """A FastAPI app with an injected fake provider, marked ready (no lifespan needed)."""
    seam = scripted_selector(fake_provider, settings)
    application = create_app(settings=settings, provider=fake_provider, selector=seam)
    container = build_container(settings, fake_provider, seam)
    detach_docs_client(container)
    application.state.container = container
    application.state.ready = True
    return application


@pytest.fixture
def client(app: Any) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        # Entering TestClient runs the app lifespan, which builds its OWN
        # container and replaces the one the `app` fixture installed — so the
        # detachment has to be reapplied to the container that actually serves
        # these requests, not the one that was discarded. (The scripted selector
        # needs no such reapplication — the lifespan container receives it via
        # app.state.injected_selector.)
        detach_docs_client(app.state.container)
        yield test_client


def respond_body(
    messages: list[dict[str, Any]],
    *,
    session_id: str = "s",
    turn_id: str = "t",
    instruction_revision: int = 0,
    round: int = 0,
    state: str | None = None,
    selection: dict[str, Any] | None = None,
    generation: dict[str, Any] | None = None,
    startup: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
    turn: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    protocol_version: int = PROTOCOL_VERSION,
) -> dict[str, Any]:
    """Build a native /v1/daintree/respond request body."""
    session: dict[str, Any] = {"id": session_id, "turn_id": turn_id}
    if instruction_revision:
        session["instruction_revision"] = instruction_revision
    if round:
        session["round"] = round
    inp: dict[str, Any] = {"messages": messages}
    if tools is not None:
        inp["tools"] = tools
    if tool_choice is not None:
        inp["tool_choice"] = tool_choice
    body: dict[str, Any] = {
        "protocol_version": protocol_version,
        "session": session,
        "input": inp,
        "startup": startup
        if startup is not None
        else {
            "project": {
                "id": "project-test",
                "name": "Test Project",
                "path": "/project",
                "status": "active",
                "daintree_config_present": True,
                "in_repo_settings": True,
            },
            "agent_roster": {
                "agents": [
                    {
                        "id": "codex",
                        "display_name": "Codex",
                        "source": "built-in",
                        "availability": "ready",
                        "installed": True,
                        "launchable": True,
                        "pinned": True,
                        "toolbar_visible": True,
                    }
                ],
                "complete": True,
                "availability_complete": True,
                "total_count": 1,
            },
            "project_instructions": "Follow the project's checked-in conventions.",
        },
    }
    if state is not None:
        body["state"] = state
    if selection is not None:
        body["selection"] = selection
    if generation is not None:
        body["generation"] = generation
    if runtime is not None:
        body["runtime"] = runtime
    if turn is not None:
        body["turn"] = turn
    return body


def meta_of(resp_json: dict[str, Any]) -> dict[str, Any]:
    """Adapt a native respond body (or stream meta event) to a flat runbook view."""
    runbooks = resp_json.get("runbooks", {}) or {}
    return {
        "active_runbooks": runbooks.get("active", []),
        "newly_loaded_runbooks": runbooks.get("newly_loaded", []),
        "selector": runbooks.get("selector", {}),
        "prelude": runbooks.get("prelude", {"tool_executions": []}),
        "state": resp_json.get("state"),
        "catalog_revision": resp_json.get("catalog_revision"),
        "warnings": resp_json.get("warnings", []),
    }


def parse_sse(text: str) -> list[tuple[str | None, dict[str, Any]]]:
    """Parse a named-event SSE stream into a list of (event, data) tuples."""
    events: list[tuple[str | None, dict[str, Any]]] = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        event: str | None = None
        data: dict[str, Any] = {}
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        events.append((event, data))
    return events
