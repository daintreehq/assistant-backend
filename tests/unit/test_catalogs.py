"""The single current catalog and per-request instruction bundles."""

from pathlib import Path

import pytest

from daintree_assistant_server.runbooks.catalog import (
    HOSTS_FOUNDATION_ID,
    MAX_SUPPORTING_RUNBOOKS,
    Catalog,
    default_runbooks_dir,
    load_catalog,
    view_for,
)
from daintree_assistant_server.runbooks.host_state import HostState
from daintree_assistant_server.runbooks.loader import (
    RunbookLoadError,
    load_runbooks_dir,
    parse_runbook,
)
from daintree_assistant_server.runbooks.models import Runbook


def _text(
    runbook_id: str,
    *,
    body: str = "body",
    foundation: bool = False,
    requires: tuple[str, ...] = (),
    eligibility: str | None = None,
) -> str:
    lines = ["---", f"id: {runbook_id}", "title: T", "summary: s", "whenToUse: w"]
    if foundation:
        lines.append("foundation: true")
    if requires:
        lines.append("requires:")
        lines += [f"  - {ref}" for ref in requires]
    if eligibility is None and runbook_id.startswith("daintree.hosts."):
        eligibility = "any"
    if eligibility is not None:
        lines.append(f"hostEligibility: {eligibility}")
    lines += ["risk: read", "---", body, ""]
    return "\n".join(lines)


def _runbook(runbook_id: str, **kwargs: object) -> Runbook:
    return parse_runbook(_text(runbook_id, **kwargs), expected_id=runbook_id)  # type: ignore[arg-type]


def _write(directory: Path, runbook_id: str, **kwargs: object) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{runbook_id}.md").write_text(_text(runbook_id, **kwargs))  # type: ignore[arg-type]


def test_packaged_catalog_has_one_file_per_id_in_its_domain() -> None:
    catalog = load_catalog()
    paths = list(default_runbooks_dir().rglob("*.md"))
    assert len(catalog) == len(paths)
    for path in paths:
        parts = path.stem.split(".")
        domain = parts[1] if len(parts) > 2 else "core"
        assert path.parent.name == domain
    assert HOSTS_FOUNDATION_ID in catalog
    assert "daintree.hosts.handle-hub-message" not in catalog
    assert not (default_runbooks_dir().parent / "inbox").exists()


def test_nested_catalogs_are_loaded_and_duplicate_ids_fail(tmp_path: Path) -> None:
    _write(tmp_path / "one", "daintree.foundation", foundation=True)
    _write(tmp_path / "two", "daintree.terminals.rename")
    assert len(load_catalog(tmp_path)) == 2
    _write(tmp_path / "three", "daintree.terminals.rename")
    with pytest.raises(RunbookLoadError, match="duplicate runbook id"):
        load_catalog(tmp_path)


def test_empty_catalog_is_a_startup_failure(tmp_path: Path) -> None:
    with pytest.raises(RunbookLoadError, match="empty"):
        load_catalog(tmp_path)


def test_attached_foundation_counts_against_the_dependency_bound(tmp_path: Path) -> None:
    _write(tmp_path, "daintree.foundation", foundation=True)
    _write(tmp_path, HOSTS_FOUNDATION_ID, foundation=True, requires=("daintree.foundation",))
    chain = [f"daintree.git.step-{i}" for i in range(MAX_SUPPORTING_RUNBOOKS)]
    for i, rid in enumerate(chain):
        _write(tmp_path, rid, requires=tuple(chain[i + 1 :]))
    assert (
        len(Catalog(load_runbooks_dir(tmp_path)).supporting_for([chain[0]]))
        == MAX_SUPPORTING_RUNBOOKS
    )
    with pytest.raises(RunbookLoadError, match="hosts foundation attached"):
        load_catalog(tmp_path)


def test_connected_view_adds_one_hosts_foundation_to_every_bundle() -> None:
    catalog = load_catalog()
    view = view_for(catalog, HostState("connected"))
    assert view.attach == (HOSTS_FOUNDATION_ID,)
    for ids in (
        ["daintree.terminals.rename"],
        ["daintree.hosts.survey-hosts"],
        ["daintree.terminals.rename", "daintree.agents.ask-and-relay"],
    ):
        assert [r.id for r in catalog.supporting_for(ids, attach=view.attach)] == [
            "daintree.foundation",
            HOSTS_FOUNDATION_ID,
        ]
    assert catalog.supporting_for([], attach=view.attach) == []


@pytest.mark.parametrize("state", [None, HostState("none"), HostState("configured")])
def test_other_views_attach_nothing(state: HostState | None) -> None:
    catalog = load_catalog()
    view = view_for(catalog, state)
    assert view.catalog is catalog
    assert view.attach == ()
    assert view.exclude == catalog.excluded_for(state)
