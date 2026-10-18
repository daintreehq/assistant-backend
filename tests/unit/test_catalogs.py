"""One catalog per host model (issue #159): the model header, how the inbox catalog
is composed from the control one, and what each caller is served.

Synthetic runbooks for the mechanics, so they do not move with the catalog; the
packaged pair is checked for what must stay true of it whatever its content.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from daintree_assistant_server.runbooks.catalog import (
    HOSTS_FOUNDATION_ID,
    MAX_SUPPORTING_RUNBOOKS,
    Catalog,
    Catalogs,
    compose_inbox,
    default_inbox_dir,
    default_runbooks_dir,
    load_catalog,
    load_catalogs,
)
from daintree_assistant_server.runbooks.host_state import (
    CallerState,
    HostState,
    parse_caller_state,
    parse_host_model,
)
from daintree_assistant_server.runbooks.loader import RunbookLoadError, parse_runbook
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


def _shared() -> list[Runbook]:
    return [
        _runbook("daintree.foundation", foundation=True),
        _runbook("daintree.terminals.rename"),
        _runbook("daintree.release.deploy", body="control deploy"),
        _runbook(
            HOSTS_FOUNDATION_ID, foundation=True, requires=("daintree.foundation",)
        ),
        _runbook("daintree.hosts.drive", eligibility="connected"),
        _runbook("daintree.hosts.connect", eligibility="any"),
    ]


# ---- the model header ------------------------------------------------------


@pytest.mark.parametrize(("values", "model"), [(["1"], "control"), ([" 1 "], "control")])
def test_exactly_one_is_the_host_control_model(values: list[str], model: str) -> None:
    assert parse_host_model(values) == model


@pytest.mark.parametrize(
    "values", [[], [""], ["0"], ["2"], ["01"], ["true"], ["1, 1"], ["1", "1"], ["control"]]
)
def test_anything_else_is_the_inbox_model(values: list[str]) -> None:
    """Fails closed, unlike the hosts header: an unknown value must never show a v0.44
    client a tool it does not have."""
    assert parse_host_model(values) == "inbox"


def test_the_model_does_not_depend_on_the_host_state() -> None:
    for hosts in (None, "none", "configured", "connected", "bogus"):
        assert parse_caller_state(["1"], hosts, None).model == "control"
        assert parse_caller_state([], hosts, None).model == "inbox"


def test_an_inbox_caller_keeps_the_v0_44_lane_contract() -> None:
    assert parse_caller_state([], "none", "reachable") == CallerState(
        "inbox", HostState("none", lane_reachable=True)
    )
    assert parse_caller_state([], "connected", "maybe") == CallerState("inbox", None)
    assert parse_caller_state([], None, None) == CallerState("inbox", None)


def test_a_control_caller_has_no_lane_and_ignores_the_header() -> None:
    for lane in (None, "reachable", "maybe"):
        assert parse_caller_state(["1"], "connected", lane) == CallerState(
            "control", HostState("connected")
        )
    assert parse_caller_state(["1"], "bogus", "reachable") == CallerState("control", None)


# ---- composing the inbox catalog ------------------------------------------


def test_the_inbox_replaces_the_hosts_namespace_and_what_it_overrides() -> None:
    frozen = [
        _runbook(HOSTS_FOUNDATION_ID, foundation=True, requires=("daintree.foundation",)),
        _runbook("daintree.hosts.handle-hub-message", eligibility="lane"),
        _runbook("daintree.release.deploy", body="inbox deploy"),
    ]
    inbox = Catalog(compose_inbox(_shared(), frozen))
    assert set(inbox.known_ids) == {
        "daintree.foundation",
        "daintree.terminals.rename",
        "daintree.release.deploy",
        HOSTS_FOUNDATION_ID,
        "daintree.hosts.handle-hub-message",
    }
    # A control-only hosts runbook never reaches the inbox, and an override wins.
    assert "daintree.hosts.drive" not in inbox
    deploy = inbox.get("daintree.release.deploy")
    assert deploy is not None and deploy.body == "inbox deploy"


def test_an_inbox_runbook_must_override_something() -> None:
    with pytest.raises(RunbookLoadError, match="overrides no shared runbook"):
        compose_inbox(_shared(), [_runbook("daintree.release.only-in-inbox")])


def test_the_control_catalog_has_no_lanes(tmp_path: Path) -> None:
    _write(tmp_path / "files", "daintree.foundation", foundation=True)
    _write(tmp_path / "files", "daintree.hosts.lane-thing", eligibility="lane")
    _write(tmp_path / "inbox", "daintree.hosts.lane-thing", eligibility="lane")
    with pytest.raises(RunbookLoadError, match="control catalog has no lanes"):
        load_catalogs(tmp_path / "files", tmp_path / "inbox")


def test_the_attached_foundation_counts_against_the_supporting_bound(tmp_path: Path) -> None:
    files = tmp_path / "files"
    _write(files, "daintree.foundation", foundation=True)
    _write(files, HOSTS_FOUNDATION_ID, foundation=True, requires=("daintree.foundation",))
    chain = [f"daintree.git.step-{i}" for i in range(MAX_SUPPORTING_RUNBOOKS)]
    for i, runbook_id in enumerate(chain):
        _write(files, runbook_id, requires=tuple(chain[i + 1 :]))
    _write(tmp_path / "inbox", HOSTS_FOUNDATION_ID, foundation=True)
    # At the bound alone (the foundation and every later step), over it with the hosts
    # foundation attached.
    assert len(Catalog(_load(files)).supporting_for([chain[0]])) == MAX_SUPPORTING_RUNBOOKS
    with pytest.raises(RunbookLoadError, match="hosts foundation attached"):
        load_catalogs(files, tmp_path / "inbox")


def _load(directory: Path) -> list[Runbook]:
    from daintree_assistant_server.runbooks.loader import load_runbooks_dir

    return load_runbooks_dir(directory)


# ---- what a caller is served -------------------------------------------------


def _catalogs() -> Catalogs:
    frozen = [
        _runbook(HOSTS_FOUNDATION_ID, foundation=True, requires=("daintree.foundation",)),
        _runbook("daintree.hosts.handle-hub-message", eligibility="lane"),
    ]
    return Catalogs(control=Catalog(_shared()), inbox=Catalog(compose_inbox(_shared(), frozen)))


def _ids(runbooks: list[Runbook]) -> list[str]:
    return [r.id for r in runbooks]


def test_a_connected_control_caller_gets_the_hosts_foundation_with_everything() -> None:
    view = _catalogs().view_for(CallerState("control", HostState("connected")))
    assert view.attach == (HOSTS_FOUNDATION_ID,)
    supporting = view.catalog.supporting_for(["daintree.terminals.rename"], attach=view.attach)
    # The general foundation still leads.
    assert _ids(supporting) == ["daintree.foundation", HOSTS_FOUNDATION_ID]
    # A hosts runbook's own foundation edge is not doubled.
    hosted = view.catalog.supporting_for(["daintree.hosts.drive"], attach=view.attach)
    assert _ids(hosted) == ["daintree.foundation", HOSTS_FOUNDATION_ID]
    # Several runbooks share one copy; the foundations themselves are not resent.
    both = view.catalog.supporting_for(
        ["daintree.terminals.rename", "daintree.release.deploy"], attach=view.attach
    )
    assert _ids(both) == ["daintree.foundation", HOSTS_FOUNDATION_ID]
    assert _ids(view.catalog.supporting_for([HOSTS_FOUNDATION_ID], attach=view.attach)) == [
        "daintree.foundation"
    ]
    assert _ids(view.catalog.supporting_for(["daintree.foundation"], attach=view.attach)) == [
        HOSTS_FOUNDATION_ID
    ]
    # Nothing delivered in full, nothing attached.
    assert view.catalog.supporting_for([], attach=view.attach) == []


@pytest.mark.parametrize(
    "state",
    [
        CallerState("control", None),
        CallerState("control", HostState("none")),
        CallerState("control", HostState("configured")),
        CallerState("inbox", HostState("connected")),
        CallerState("inbox", HostState("connected", lane_reachable=True)),
        CallerState("inbox", None),
    ],
)
def test_no_other_caller_has_anything_attached(state: CallerState) -> None:
    view = _catalogs().view_for(state)
    assert view.attach == ()
    assert _ids(view.catalog.supporting_for(["daintree.terminals.rename"])) == [
        "daintree.foundation"
    ]


def test_each_model_is_served_from_its_own_catalog() -> None:
    catalogs = _catalogs()
    control = catalogs.view_for(CallerState("control", None)).catalog
    inbox = catalogs.view_for(CallerState("inbox", None)).catalog
    assert control is catalogs.control and inbox is catalogs.inbox
    assert control.revision != inbox.revision
    assert "daintree.hosts.handle-hub-message" in inbox
    assert "daintree.hosts.handle-hub-message" not in control
    assert "daintree.hosts.drive" not in inbox


def test_host_state_narrows_the_search_within_the_callers_catalog() -> None:
    catalogs = _catalogs()
    control = catalogs.view_for(CallerState("control", HostState("none")))
    assert control.exclude == frozenset({"daintree.hosts.drive"})
    inbox = catalogs.view_for(CallerState("inbox", HostState("connected")))
    assert inbox.exclude == frozenset({"daintree.hosts.handle-hub-message"})


# ---- the packaged pair ---------------------------------------------------------

#: sha256 over the inbox directory's files, in name order, as ``name\0bytes``. The
#: inbox catalog is what every v0.44 client is served, frozen at the post-#161
#: catalog (revision ``sha256:f863c7a9…`` when it was split off). Change a file
#: there only to fix something wrong for v0.44 clients, and update this with it.
INBOX_DIGEST = "sha256:b9eba7fd8cd72c0fe0bd94c29dfde2c832e17ff036413d0cb0f28b31a79432a7"


def _inbox_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.md")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def test_the_inbox_overlay_is_frozen() -> None:
    assert _inbox_digest(default_inbox_dir()) == INBOX_DIGEST


def test_the_packaged_catalogs_load() -> None:
    catalogs = load_catalogs()
    assert catalogs.control.revision == load_catalog().revision
    assert len(catalogs.control) == len(list(default_runbooks_dir().glob("*.md")))
    assert HOSTS_FOUNDATION_ID in catalogs.control and HOSTS_FOUNDATION_ID in catalogs.inbox


def test_every_inbox_override_differs_from_the_control_runbook() -> None:
    """An identical copy would freeze a runbook for v0.44 clients for no reason: a
    shared runbook keeps improving for both models."""
    catalogs = load_catalogs()
    for path in sorted(default_inbox_dir().glob("*.md")):
        frozen = catalogs.inbox.get(path.stem)
        current = catalogs.control.get(path.stem)
        assert frozen is not None
        if current is not None:
            assert frozen.content_hash != current.content_hash, (
                f"{path.stem} is identical in both catalogs; delete its inbox copy"
            )


# ---- one classifier, a table per catalog -------------------------------------

_ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / "src/daintree_assistant_server/runbooks/local/artifacts/selector"
)


def _artifact_present() -> bool:
    head = _ARTIFACT / "head.onnx"
    return head.exists() and head.stat().st_size > 1024


@pytest.mark.skipif(not _artifact_present(), reason="selector model not fetched (training/fetch_selector.sh)")
def test_alternating_catalogs_never_rebuild_a_table() -> None:
    """The server alternates between the two models' catalogs request by request; one
    cached slot would rebuild the table on every alternation."""
    from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier

    catalogs = load_catalogs()
    clf = SetwiseClassifier(_ARTIFACT)
    control = clf.sync(catalogs.control)
    inbox = clf.sync(catalogs.inbox)
    for _ in range(3):
        assert clf.sync(catalogs.control) is control
        assert clf.sync(catalogs.inbox) is inbox
    assert set(control.ids) == catalogs.control.selectable_ids
    assert set(inbox.ids) == catalogs.inbox.selectable_ids
    # A runbook both catalogs share is embedded once.
    shared = "daintree.terminals.rename"
    assert (
        control.retrieval[control.ids.index(shared)]
        == inbox.retrieval[inbox.ids.index(shared)]
    ).all()
    # A third catalog evicts the oldest; the newest two stay.
    narrowed = Catalog([r for r in catalogs.control.runbooks if r.id != shared])
    clf.sync(narrowed)
    assert clf.sync(catalogs.inbox) is inbox


def test_every_control_hosts_runbook_is_classified() -> None:
    """The control catalog has no lanes. Adding the first host and the domain's base
    judgment fit a caller with none; what is wanted right after hosts drop, and the
    read-only survey that diagnoses why one cannot be reached, stays offered while they
    are down."""
    control = load_catalogs().control
    hosts = {
        r.id: r.meta.host_eligibility for r in control.runbooks if r.id.startswith("daintree.hosts.")
    }
    assert hosts and all(v is not None for v in hosts.values())
    assert "lane" not in hosts.values()
    assert {rid for rid, v in hosts.items() if v == "any"} == {
        "daintree.hosts.connect-host",
        HOSTS_FOUNDATION_ID,
    }
    for leaf in (
        "recover-and-clean-up",
        "park-swarm",
        "undo-swarm-changes",
        "survey-hosts",
        "copy-portable-settings",
        "open-project-on-host",
        "supervise-swarm",
    ):
        assert hosts[f"daintree.hosts.{leaf}"] == "configured", leaf
    offered = control.selectable_ids - control.excluded_for(HostState("none"))
    assert {rid for rid in offered if rid.startswith("daintree.hosts.")} == {
        "daintree.hosts.connect-host",
        HOSTS_FOUNDATION_ID,
    }
