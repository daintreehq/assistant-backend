"""Shared pytest fixtures."""

from __future__ import annotations

import base64
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
from daintree_assistant_server.skills.catalog import load_catalog
from daintree_assistant_server.skills.selector import Selector
from daintree_assistant_server.state.codec import StateCodec

API_KEY = "test-api-key"


@pytest.fixture
def settings() -> Settings:
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
def state_codec(settings: Settings) -> StateCodec:
    return StateCodec(
        current_key=settings.state_signing_key_current.get_secret_value().encode(),
        current_kid=settings.state_signing_kid,
        salt=settings.state_salt.get_secret_value().encode(),
        ttl_seconds=settings.state_token_ttl_seconds,
    )


@pytest.fixture
def selector(fake_provider: FakeProvider, prompt_loader: PromptLoader, catalog: Any, settings: Settings) -> Selector:
    return Selector(fake_provider, prompt_loader, catalog, settings)


@pytest.fixture
def assembler(prompt_loader: PromptLoader, settings: Settings) -> PromptAssembler:
    return PromptAssembler(prompt_loader, settings)


@pytest.fixture
def orchestrator(
    settings: Settings,
    catalog: Any,
    selector: Selector,
    state_codec: StateCodec,
    assembler: PromptAssembler,
    fake_provider: FakeProvider,
) -> ChatOrchestrator:
    return ChatOrchestrator(
        settings=settings,
        catalog=catalog,
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


def decode_meta(headers: Any) -> dict[str, Any]:
    """Decode the X-Daintree-Meta header into a dict."""
    raw = headers["x-daintree-meta"]
    pad = "=" * (-len(raw) % 4)
    return json.loads(base64.urlsafe_b64decode(raw + pad))
