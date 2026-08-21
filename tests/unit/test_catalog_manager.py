"""Overlay loading and in-place reload for the skill catalog."""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from daintree_assistant_server.skills import catalog as catalog_module
from daintree_assistant_server.skills.catalog import CatalogManager, load_catalog
from daintree_assistant_server.skills.loader import SkillLoadError


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
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{skill_id}.md").write_text(_skill(skill_id, summary), encoding="utf-8")


def test_load_primary_only(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    _write(primary, "test.alpha", "Alpha.")
    _write(primary, "test.beta", "Beta.")
    catalog = load_catalog(primary)
    assert set(catalog.known_ids) == {"test.alpha", "test.beta"}


def test_overlay_adds_new_id(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    overlay = tmp_path / "overlay"
    _write(primary, "test.alpha", "Alpha.")
    _write(overlay, "test.gamma", "Gamma from overlay.")
    catalog = load_catalog(primary, overlay)
    assert set(catalog.known_ids) == {"test.alpha", "test.gamma"}


def test_overlay_overrides_primary_by_id(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    overlay = tmp_path / "overlay"
    _write(primary, "test.alpha", "Original summary.")
    _write(overlay, "test.alpha", "Overridden summary.")
    catalog = load_catalog(primary, overlay)
    skill = catalog.get("test.alpha")
    assert skill is not None
    assert skill.summary == "Overridden summary."


def test_overlay_equal_to_primary_is_ignored(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    _write(primary, "test.alpha", "Alpha.")
    # overlay_dir pointing at the same directory must not raise a duplicate-id error.
    catalog = load_catalog(primary, primary)
    assert set(catalog.known_ids) == {"test.alpha"}


def test_missing_overlay_dir_is_ignored(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    _write(primary, "test.alpha", "Alpha.")
    catalog = load_catalog(primary, tmp_path / "does-not-exist")
    assert set(catalog.known_ids) == {"test.alpha"}


def test_reload_picks_up_new_file(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    _write(primary, "test.alpha", "Alpha.")
    manager = CatalogManager(load_catalog(primary), primary_dir=primary)
    before = manager.catalog.revision
    assert "test.delta" not in manager.catalog

    _write(primary, "test.delta", "Delta.")
    _, new = manager.reload_sync()
    assert "test.delta" in new
    assert manager.catalog is new
    assert new.revision != before


@pytest.mark.asyncio
async def test_reload_async(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    _write(primary, "test.alpha", "Alpha.")
    manager = CatalogManager(load_catalog(primary), primary_dir=primary)
    _write(primary, "test.epsilon", "Epsilon.")
    _, new = await manager.reload()
    assert "test.epsilon" in new
    assert manager.catalog is new


def test_reload_uses_the_overlay_dir(tmp_path: Path) -> None:
    """The manager keeps the overlay path, so a reload re-reads BOTH directories.

    A reload that only re-read the primary directory would be the one bug that
    makes this whole feature useless: the overlay would work at startup and then
    silently stop mattering the moment a developer edited a file.
    """
    primary = tmp_path / "primary"
    overlay = tmp_path / "overlay"
    _write(primary, "test.alpha", "Alpha.")
    overlay.mkdir()
    manager = CatalogManager(
        load_catalog(primary, overlay), primary_dir=primary, overlay_dir=overlay
    )
    assert manager.overlay_dir == overlay

    _write(overlay, "test.alpha", "Overridden after startup.")
    _write(overlay, "test.gamma", "Gamma from overlay.")
    _, new = manager.reload_sync()

    alpha = new.get("test.alpha")
    assert alpha is not None
    assert alpha.summary == "Overridden after startup."
    assert "test.gamma" in new


@pytest.mark.asyncio
async def test_reload_async_uses_the_overlay_dir(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    overlay = tmp_path / "overlay"
    _write(primary, "test.alpha", "Alpha.")
    overlay.mkdir()
    manager = CatalogManager(
        load_catalog(primary, overlay), primary_dir=primary, overlay_dir=overlay
    )
    _write(overlay, "test.gamma", "Gamma from overlay.")
    _, new = await manager.reload()
    assert "test.gamma" in new


def test_a_failed_reload_keeps_the_previous_catalog(tmp_path: Path) -> None:
    """Fail SAFE: a half-written skill must not take the running catalog down.

    The load happens in full before the rebind, so the identity check is the
    assertion that matters — not merely an equal revision, but the very same
    object still serving.
    """
    primary = tmp_path / "primary"
    overlay = tmp_path / "overlay"
    _write(primary, "test.alpha", "Alpha.")
    overlay.mkdir()
    manager = CatalogManager(
        load_catalog(primary, overlay), primary_dir=primary, overlay_dir=overlay
    )
    before = manager.catalog

    (overlay / "test.broken.md").write_text("no frontmatter here", encoding="utf-8")
    with pytest.raises(SkillLoadError):
        manager.reload_sync()

    assert manager.catalog is before


@pytest.mark.asyncio
async def test_a_failed_async_reload_keeps_the_previous_catalog(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    overlay = tmp_path / "overlay"
    _write(primary, "test.alpha", "Alpha.")
    overlay.mkdir()
    manager = CatalogManager(
        load_catalog(primary, overlay), primary_dir=primary, overlay_dir=overlay
    )
    before = manager.catalog

    (overlay / "test.broken.md").write_text("no frontmatter here", encoding="utf-8")
    with pytest.raises(SkillLoadError):
        await manager.reload()

    assert manager.catalog is before


def test_an_overlay_path_that_is_a_file_is_rejected(tmp_path: Path) -> None:
    """Distinct from a missing directory, and deliberately so.

    A path that has not been created yet is a normal first-run state. A path
    that exists and is not a directory is a typo, and silently ignoring it is
    the failure mode that wastes an afternoon.
    """
    primary = tmp_path / "primary"
    _write(primary, "test.alpha", "Alpha.")
    not_a_directory = tmp_path / "overlay.md"
    not_a_directory.write_text("a file", encoding="utf-8")

    with pytest.raises(SkillLoadError, match="not a directory"):
        load_catalog(primary, not_a_directory)


def test_reload_reports_the_revision_it_replaced(tmp_path: Path) -> None:
    """The pair is the contract: what was bound before, and what is bound now.

    Reading the old revision separately, before calling, is what made this racy
    in the first place — so the manager reports it from inside the same critical
    section that performed the swap.
    """
    primary = tmp_path / "primary"
    _write(primary, "test.alpha", "Alpha.")
    manager = CatalogManager(load_catalog(primary), primary_dir=primary)
    before = manager.catalog.revision

    _write(primary, "test.delta", "Delta.")
    previous, new = manager.reload_sync()

    assert previous == before
    assert new.revision != previous
    assert manager.catalog is new


@pytest.mark.asyncio
async def test_concurrent_reloads_cannot_overlap(tmp_path: Path, monkeypatch) -> None:
    """Two reloads must not be in flight at once.

    Without the lock both coroutines dispatch their blocking load to the thread
    pool before either finishes, so the slower one rebinds LAST and the newer
    catalog is lost — an author's edit reverting itself. The assertion is on the
    overlap itself rather than on which revision won, because "which won" is
    exactly the thing that is nondeterministic when the bug is present.
    """
    primary = tmp_path / "primary"
    _write(primary, "test.alpha", "Alpha.")
    manager = CatalogManager(load_catalog(primary), primary_dir=primary)

    real = catalog_module.load_catalog
    counter = threading.Lock()
    in_flight = 0
    peak = 0

    def counting(*args: object, **kwargs: object) -> object:
        nonlocal in_flight, peak
        with counter:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            time.sleep(0.05)
            return real(*args, **kwargs)  # type: ignore[arg-type]
        finally:
            with counter:
                in_flight -= 1

    monkeypatch.setattr(catalog_module, "load_catalog", counting)
    await asyncio.gather(manager.reload(), manager.reload())

    assert peak == 1
