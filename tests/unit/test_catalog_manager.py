"""Overlay loading and in-place reload for the skill catalog."""

from __future__ import annotations

from pathlib import Path

import pytest

from daintree_assistant_server.skills.catalog import CatalogManager, load_catalog


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
    new = manager.reload_sync()
    assert "test.delta" in new
    assert manager.catalog is new
    assert new.revision != before


@pytest.mark.asyncio
async def test_reload_async(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    _write(primary, "test.alpha", "Alpha.")
    manager = CatalogManager(load_catalog(primary), primary_dir=primary)
    _write(primary, "test.epsilon", "Epsilon.")
    new = await manager.reload()
    assert "test.epsilon" in new
    assert manager.catalog is new
