"""The skill-overlay directory and its live reload endpoint.

Two properties carry this feature, and they pull in opposite directions:

* a developer must be able to edit a skill against a running server and see the
  edit take effect — that is the entire point;
* production must never do this. The route mutates process state and
  ``require_auth`` gates nothing yet, so the protection is that the URL does not
  exist there at all. The tests below assert its ABSENCE as carefully as its
  behaviour.

The third property is what makes it usable rather than merely possible: a skill
saved half-written costs a 409, not the running catalog.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from conftest import scripted_selector
from fastapi.testclient import TestClient

from daintree_assistant_server.config import Settings
from daintree_assistant_server.main import create_app

RELOAD_URL = "/v1/daintree/skills/reload"


def _skill(skill_id: str, summary: str) -> str:
    return (
        f"---\n"
        f"id: {skill_id}\n"
        f"title: Title for {skill_id}\n"
        f"summary: {summary}\n"
        f"whenToUse: Use when testing {skill_id}.\n"
        f"---\n"
        f"Use when: testing.\n\n"
        f"Procedure:\n1. Do the thing.\n\n"
        f"Report back: done.\n"
    )


def _write(directory: Path, skill_id: str, summary: str) -> None:
    (directory / f"{skill_id}.md").write_text(_skill(skill_id, summary), encoding="utf-8")


def _settings(overlay: Path | None, environment: str = "development") -> Settings:
    return Settings(
                STATE_SIGNING_KEY_CURRENT="test-signing-key",
        STATE_SIGNING_KID="k1",
        STATE_SALT="test-salt",
        RETRY_BACKOFF_BASE_SECONDS=0.0,
        ENVIRONMENT=environment,
        DAINTREE_SKILLS_OVERLAY_DIR=str(overlay) if overlay is not None else None,
    )


def _app(settings: Settings, fake_provider: Any) -> Any:
    return create_app(
        settings=settings,
        provider=fake_provider,
        selector=scripted_selector(fake_provider, settings),
    )


def _serve(app: Any) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        # The lifespan builds a real DocsMcpClient pointed at daintree.org; these
        # tests never answer a turn, but detaching keeps the suite offline by
        # construction rather than by luck.
        container = app.state.container
        if container is not None:
            container.orchestrator.docs_client = None
        yield test_client


@pytest.fixture
def overlay(tmp_path: Path) -> Path:
    directory = tmp_path / "overlay-skills"
    directory.mkdir()
    return directory


@pytest.fixture
def overlay_client(overlay: Path, fake_provider: Any) -> Iterator[TestClient]:
    yield from _serve(_app(_settings(overlay), fake_provider))


def _container(client: TestClient) -> Any:
    return client.app.state.container  # type: ignore[union-attr]


# ---- mounting: the route exists only where authoring happens ---------------


def test_the_reload_route_is_absent_without_an_overlay(fake_provider: Any) -> None:
    """Asserted over HTTP, not over ``app.routes``.

    A route-table assertion here would pass whether or not the route existed if
    the introspection were ever wrong, and an absence test that cannot fail is
    worse than no test. 404 is the thing a caller actually observes.
    """
    for client in _serve(_app(_settings(None), fake_provider)):
        assert client.post(RELOAD_URL).status_code == 404


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_the_reload_route_is_absent_in_a_hardened_environment(
    overlay: Path, fake_provider: Any, environment: str
) -> None:
    """Belt AND braces: readiness refuses the variable, and the route is unmounted.

    Either alone would do, but they fail differently — readiness is a deploy-time
    alarm someone has to be watching, while an absent route cannot be called by
    anything at any time.
    """
    settings = _settings(overlay, environment=environment)
    assert any("DAINTREE_SKILLS_OVERLAY_DIR" in p for p in settings.local_only_problems())
    for client in _serve(_app(settings, fake_provider)):
        assert client.post(RELOAD_URL).status_code == 404


def test_the_reload_route_is_mounted_for_local_authoring(
    overlay_client: TestClient, auth: dict[str, str]
) -> None:
    assert overlay_client.post(RELOAD_URL, headers=auth).status_code == 200


# ---- the authoring loop ----------------------------------------------------


def test_a_configured_overlay_is_loaded_at_startup(overlay: Path, fake_provider: Any) -> None:
    _write(overlay, "test.overlay.startup", "Present from the first request.")
    for client in _serve(_app(_settings(overlay), fake_provider)):
        assert "test.overlay.startup" in _container(client).catalog
        assert _container(client).catalog_manager.overlay_dir == overlay


def test_reload_applies_an_overlay_edit_live(
    overlay: Path, overlay_client: TestClient, auth: dict[str, str]
) -> None:
    manager = _container(overlay_client).catalog_manager
    before_revision = manager.catalog.revision
    before_etag = overlay_client.get("/v1/daintree/skills", headers=auth).headers["ETag"]
    assert "test.overlay.live" not in manager.catalog

    _write(overlay, "test.overlay.live", "Added while the server was running.")
    r = overlay_client.post(RELOAD_URL, headers=auth)

    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is True
    assert body["previous_catalog_revision"] == before_revision
    assert body["catalog_revision"] != before_revision
    assert body["skills"] == len(manager.catalog)

    # The reload reaches every consumer because they all read through the
    # manager — the container property and the orchestrator included.
    assert "test.overlay.live" in _container(overlay_client).catalog
    assert "test.overlay.live" in _container(overlay_client).orchestrator.catalog

    assert body["object"] == "skill_catalog_reload"

    listed = overlay_client.get("/v1/daintree/skills", headers=auth)
    assert listed.headers["ETag"] != before_etag
    assert any(s["id"] == "test.overlay.live" for s in listed.json()["data"])

    # The conditional path has to move too. A cache that still answers 304 to the
    # pre-reload ETag would hand a CLI the old catalog for as long as it kept
    # asking politely — the one client most likely to be holding an ETag.
    stale = overlay_client.get(
        "/v1/daintree/skills", headers={**auth, "If-None-Match": before_etag}
    )
    assert stale.status_code == 200
    fresh = overlay_client.get(
        "/v1/daintree/skills", headers={**auth, "If-None-Match": listed.headers["ETag"]}
    )
    assert fresh.status_code == 304


def test_reload_reports_an_unchanged_catalog(
    overlay_client: TestClient, auth: dict[str, str]
) -> None:
    """A no-op reload is a success, not an error — it just says nothing moved."""
    revision = _container(overlay_client).catalog.revision

    r = overlay_client.post(RELOAD_URL, headers=auth)

    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is False
    assert body["previous_catalog_revision"] == revision
    assert body["catalog_revision"] == revision


def test_an_overlay_skill_replaces_the_packaged_one_by_id(
    overlay: Path, overlay_client: TestClient, auth: dict[str, str]
) -> None:
    """The override path, end to end: the id a developer is iterating on."""
    target = next(iter(_container(overlay_client).catalog.known_ids))

    _write(overlay, target, "Overridden live.")
    assert overlay_client.post(RELOAD_URL, headers=auth).status_code == 200

    skill = _container(overlay_client).catalog.get(target)
    assert skill is not None
    assert skill.summary == "Overridden live."


# ---- failing safe ----------------------------------------------------------


def test_a_malformed_overlay_skill_keeps_the_running_catalog(
    overlay: Path, overlay_client: TestClient, auth: dict[str, str]
) -> None:
    manager = _container(overlay_client).catalog_manager
    before = manager.catalog

    (overlay / "test.overlay.broken.md").write_text("not a skill", encoding="utf-8")
    r = overlay_client.post(RELOAD_URL, headers=auth)

    assert r.status_code == 409
    assert r.json()["error"]["code"] == "skill_catalog_reload_failed"
    # Identity, not equality: the very same catalog object is still serving.
    assert manager.catalog is before
    assert overlay_client.get("/readyz").status_code == 200
    assert overlay_client.get("/v1/daintree/skills", headers=auth).status_code == 200


def test_a_fixed_overlay_skill_reloads_after_a_failure(
    overlay: Path, overlay_client: TestClient, auth: dict[str, str]
) -> None:
    """The failure costs a 409, not a restart — that is what makes it a loop."""
    broken = overlay / "test.overlay.fixable.md"
    broken.write_text("not a skill", encoding="utf-8")
    assert overlay_client.post(RELOAD_URL, headers=auth).status_code == 409

    broken.write_text(_skill("test.overlay.fixable", "Fixed."), encoding="utf-8")
    r = overlay_client.post(RELOAD_URL, headers=auth)

    assert r.status_code == 200
    assert r.json()["changed"] is True
    assert "test.overlay.fixable" in _container(overlay_client).catalog


def test_reload_picks_up_an_edit_to_an_existing_overlay_skill(
    overlay: Path, fake_provider: Any, auth: dict[str, str]
) -> None:
    """The literal ask in the issue: EDIT a skill while the server runs.

    Adding a file and editing one are different code paths to anything that
    remembers what it has already read, and editing is the one an author does
    fifty times an afternoon.
    """
    _write(overlay, "test.overlay.edited", "The first draft.")
    for client in _serve(_app(_settings(overlay), fake_provider)):
        before = _container(client).catalog.get("test.overlay.edited")
        assert before is not None
        assert before.summary == "The first draft."

        _write(overlay, "test.overlay.edited", "The second draft.")
        r = client.post(RELOAD_URL, headers=auth)

        assert r.status_code == 200
        assert r.json()["changed"] is True
        after = _container(client).catalog.get("test.overlay.edited")
        assert after is not None
        assert after.summary == "The second draft."


def test_an_overlay_directory_created_after_startup_is_picked_up(
    tmp_path: Path, fake_provider: Any, auth: dict[str, str]
) -> None:
    """The first run: the variable is set before the directory exists.

    Failing readiness here would make the feature hostile to the exact moment it
    is being switched on, so a not-yet-created overlay is simply empty — and the
    route is mounted anyway, ready for the directory to appear.
    """
    overlay = tmp_path / "not-yet"
    for client in _serve(_app(_settings(overlay), fake_provider)):
        assert client.post(RELOAD_URL, headers=auth).json()["changed"] is False

        overlay.mkdir()
        _write(overlay, "test.overlay.late", "Arrived after startup.")
        r = client.post(RELOAD_URL, headers=auth)

        assert r.status_code == 200
        assert "test.overlay.late" in _container(client).catalog


def test_an_overlay_path_that_is_a_file_fails_readiness(
    tmp_path: Path, fake_provider: Any, auth: dict[str, str]
) -> None:
    """A typo'd path must not be indistinguishable from an empty directory.

    Ignoring it silently is the worst outcome available: the author edits skills
    into a path nothing reads and is told, correctly and uselessly, that nothing
    changed.
    """
    not_a_directory = tmp_path / "overlay.md"
    not_a_directory.write_text("this is a file, not a directory", encoding="utf-8")

    for client in _serve(_app(_settings(not_a_directory), fake_provider)):
        assert client.get("/readyz").status_code == 503
        # Mounted (the overlay IS configured) but unready — 503, not 404, which
        # is what tells the absence tests above apart from an unready server.
        assert client.post(RELOAD_URL, headers=auth).status_code == 503


def test_the_reload_route_is_503_not_500_on_an_unready_server(
    overlay: Path, fake_provider: Any, auth: dict[str, str]
) -> None:
    """A malformed skill at STARTUP is still a readiness failure, not a reload.

    There is no container to reload from in that state, so the route has to be
    readiness-gated: without the dependency it would reach `container=None` and
    answer a routine authoring mistake with a 500.
    """
    (overlay / "test.overlay.broken.md").write_text("not a skill", encoding="utf-8")

    for client in _serve(_app(_settings(overlay), fake_provider)):
        assert client.get("/readyz").status_code == 503
        r = client.post(RELOAD_URL, headers=auth)
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "service_unavailable"
        assert r.headers["Retry-After"] == "5"
