"""Shared pytest fixtures."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fakes import FakeProvider
from fastapi.testclient import TestClient

from daintree_assistant_server.config import Settings
from daintree_assistant_server.main import build_container, create_app
from daintree_assistant_server.prompts.loader import PromptLoader
from daintree_assistant_server.services.chat_orchestrator import ChatOrchestrator
from daintree_assistant_server.services.prompt_assembler import PromptAssembler
from daintree_assistant_server.services.task_runner import TaskRunner
from daintree_assistant_server.skills.catalog import (
    CatalogManager,
    default_skills_dir,
    load_catalog,
)
from daintree_assistant_server.skills.selector import Selector
from daintree_assistant_server.state.codec import StateCodec

API_KEY = "test-api-key"


@pytest.fixture(autouse=True, scope="session")
def _isolate_from_dotenv() -> Iterator[None]:
    """Tests must not read a developer's local ``.env``: it would override any
    Settings field not explicitly passed and break test isolation."""
    previous = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    yield
    Settings.model_config["env_file"] = previous


@pytest.fixture
def settings(tmp_path: Any) -> Settings:
    return Settings(
        DAINTREE_API_KEY=API_KEY,
        DEEPSEEK_API_KEY="test-deepseek-key",
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
    primary = default_skills_dir()
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
    prompt_loader: PromptLoader,
    catalog_manager: CatalogManager,
    settings: Settings,
) -> Selector:
    return Selector(fake_provider, prompt_loader, catalog_manager, settings)


@pytest.fixture
def assembler(prompt_loader: PromptLoader, settings: Settings) -> PromptAssembler:
    return PromptAssembler(prompt_loader, settings)


@pytest.fixture
def orchestrator(
    settings: Settings,
    catalog_manager: CatalogManager,
    selector: Selector,
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
def task_runner(settings: Settings, fake_provider: FakeProvider, prompt_loader: PromptLoader) -> TaskRunner:
    return TaskRunner(settings=settings, provider=fake_provider, prompt_loader=prompt_loader)


@pytest.fixture
def app(settings: Settings, fake_provider: FakeProvider) -> Any:
    """A FastAPI app with an injected fake provider, marked ready (no lifespan needed)."""
    application = create_app(settings=settings, provider=fake_provider)
    application.state.container = build_container(settings, fake_provider)
    application.state.ready = True
    return application


@pytest.fixture
def client(app: Any) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
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
    runtime: dict[str, Any] | None = None,
    turn: dict[str, Any] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    protocol_version: int = 1,
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
    """Adapt a native respond body (or stream meta event) to a flat skill view."""
    skills = resp_json.get("skills", {}) or {}
    return {
        "active_skills": skills.get("active", []),
        "newly_loaded_skills": skills.get("newly_loaded", []),
        "selector": skills.get("selector", {}),
        "prelude": skills.get("prelude", {"tool_executions": []}),
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
