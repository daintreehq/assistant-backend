"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fakes import ScriptedSelector
from fastapi.testclient import TestClient

from daintree_assistant_server.config import Settings
from daintree_assistant_server.main import create_app
from daintree_assistant_server.runbooks.catalog import Catalog, default_runbooks_dir, load_catalog


@pytest.fixture(autouse=True, scope="session")
def _isolate_from_dotenv() -> Iterator[None]:
    """Tests must not read a developer's local ``.env``: it would override any
    Settings field not explicitly passed."""
    previous = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    yield
    Settings.model_config["env_file"] = previous


@pytest.fixture
def settings() -> Settings:
    return Settings(ENVIRONMENT="test")


@pytest.fixture
def catalog() -> Catalog:
    return load_catalog(default_runbooks_dir())


@pytest.fixture
def app(settings: Settings, catalog: Catalog) -> Any:
    """The app over a scripted selector; the lifespan builds the real catalog."""
    return create_app(settings=settings, selector=ScriptedSelector(catalog))


@pytest.fixture
def client(app: Any) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
