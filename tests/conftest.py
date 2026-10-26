"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
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


@pytest.fixture(scope="session")
def shipped_selector_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The shipped selector, with its table already synced to the current catalog.

    The shipped ``vectors.npz`` predates the encoder fingerprint, so no classifier
    adopts it, and each new one embeds the whole catalog, one document at a time, on
    its first ``sync``: about 20s, paid again by every test that built its own. This
    embeds it once per session and saves the table, fingerprinted, beside symlinks to
    the rest of the artifact, so a classifier built here is still fresh but adopts
    the table and syncs on the fast path. Tests whose subject is embedding itself
    keep the real artifact directory.
    """
    from daintree_assistant_server.runbooks.local.paths_local import selector_artifact_dir
    from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier, save_table

    shipped = selector_artifact_dir()
    synced = tmp_path_factory.mktemp("selector")
    for part in shipped.iterdir():
        if part.name != "vectors.npz":
            (synced / part.name).symlink_to(part)
    save_table(synced / "vectors.npz", SetwiseClassifier(shipped).sync(load_catalog()))
    return synced
