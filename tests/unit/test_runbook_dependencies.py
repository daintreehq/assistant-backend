"""Instruction dependencies: `optionalTools`, `requires`, and the supporting set.

A runbook names the instructions that must be in hand before it is followed —
its nearest domain foundation implicitly, other runbooks through `requires` — and
both tools deliver those once, beside the procedure. A dependency that cannot be
resolved, is never served, or loops is a malformed catalog: it fails readiness,
exactly like a runbook with bad frontmatter.
"""

from __future__ import annotations

import pytest

from daintree_assistant_server.runbooks.catalog import (
    DOCS_RUNBOOK_ID,
    MAX_SUPPORTING_RUNBOOKS,
    Catalog,
    load_catalog,
)
from daintree_assistant_server.runbooks.loader import RunbookLoadError, parse_runbook
from daintree_assistant_server.runbooks.models import Runbook


def _runbook(
    runbook_id: str,
    *,
    foundation: bool = False,
    requires: list[str] | None = None,
    required_tools: list[str] | None = None,
    optional_tools: list[str] | None = None,
    body: str = "body",
) -> Runbook:
    lines = ["---", f"id: {runbook_id}", "title: T", "summary: s", "whenToUse: w"]
    if foundation:
        lines.append("foundation: true")
    for key, values in (
        ("requires", requires),
        ("requiredTools", required_tools),
        ("optionalTools", optional_tools),
    ):
        if values is not None:
            lines.append(f"{key}:")
            lines += [f"  - {v}" for v in values]
    lines += ["---", body, ""]
    return parse_runbook("\n".join(lines), expected_id=runbook_id)


# ---- frontmatter ---------------------------------------------------------------


def test_optional_tools_and_requires_parse() -> None:
    runbook = _runbook(
        "x.a",
        requires=["x.b"],
        required_tools=["agent.launch"],
        optional_tools=["terminal.notifyWhenIdle"],
    )
    assert runbook.meta.requires == ["x.b"]
    assert runbook.meta.optional_tools == ["terminal.notifyWhenIdle"]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"required_tools": ["agent.launch", "agent.launch"]}, "duplicate"),
        ({"optional_tools": ["a.b", "a.b"]}, "duplicate"),
        ({"required_tools": ["agent.launch"], "optional_tools": ["agent.launch"]}, "both"),
        ({"optional_tools": ["not a tool"]}, "invalid"),
        ({"requires": ["x.a"]}, "requires itself"),
        ({"requires": ["x.b", "x.b"]}, "duplicate"),
        ({"requires": ["x..b"]}, "empty dotted segment"),
    ],
)
def test_malformed_dependency_metadata_is_a_load_error(
    kwargs: dict[str, list[str]], message: str
) -> None:
    with pytest.raises(RunbookLoadError, match=message):
        _runbook("x.a", **kwargs)  # type: ignore[arg-type]


def test_new_metadata_changes_the_content_hash() -> None:
    plain = _runbook("x.a")
    assert _runbook("x.a", requires=["x.b"]).content_hash != plain.content_hash
    assert _runbook("x.a", optional_tools=["a.b"]).content_hash != plain.content_hash


# ---- the graph -------------------------------------------------------------------


def test_supporting_set_is_foundation_first_then_requires_each_once() -> None:
    catalog = Catalog(
        [
            _runbook("d.foundation", foundation=True),
            _runbook("d.shared"),
            _runbook("d.helper", requires=["d.shared"]),
            _runbook("d.task", requires=["d.helper", "d.shared"]),
        ]
    )
    assert [r.id for r in catalog.supporting_for(["d.task"])] == [
        "d.foundation",
        "d.shared",
        "d.helper",
    ]


def test_supporting_set_omits_what_was_already_served() -> None:
    catalog = Catalog(
        [
            _runbook("d.foundation", foundation=True),
            _runbook("d.one"),
            _runbook("d.two", requires=["d.one"]),
        ]
    )
    assert [r.id for r in catalog.supporting_for(["d.two", "d.one"])] == ["d.foundation"]
    assert catalog.supporting_for(["d.foundation"]) == []
    assert [r.id for r in catalog.supporting_for(["d.one", "d.two"])] == ["d.foundation"]


def test_a_nested_foundation_does_not_pull_its_ancestor_implicitly() -> None:
    """Nearest-foundation pairing names ONE foundation; an ancestor's judgment
    reaches a nested domain only when the nested foundation requires it."""
    runbooks = [
        _runbook("d.foundation", foundation=True),
        _runbook("d.sub.foundation", foundation=True),
        _runbook("d.sub.task"),
    ]
    assert [r.id for r in Catalog(runbooks).supporting_for(["d.sub.task"])] == [
        "d.sub.foundation"
    ]
    runbooks[1] = _runbook("d.sub.foundation", foundation=True, requires=["d.foundation"])
    assert [r.id for r in Catalog(runbooks).supporting_for(["d.sub.task"])] == [
        "d.foundation",
        "d.sub.foundation",
    ]


def test_an_unknown_requirement_fails_the_catalog() -> None:
    with pytest.raises(RunbookLoadError, match="unknown runbook 'd.ghost'"):
        Catalog([_runbook("d.task", requires=["d.ghost"])])


def test_requiring_the_hidden_runbook_fails_the_catalog() -> None:
    with pytest.raises(RunbookLoadError, match="never served"):
        Catalog([_runbook(DOCS_RUNBOOK_ID), _runbook("d.task", requires=[DOCS_RUNBOOK_ID])])


def test_an_explicit_cycle_fails_the_catalog() -> None:
    with pytest.raises(RunbookLoadError, match="cycle"):
        Catalog([_runbook("d.a", requires=["d.b"]), _runbook("d.b", requires=["d.a"])])


def test_a_foundation_requiring_its_own_specific_is_a_cycle() -> None:
    """The implicit specific -> foundation edge closes the loop."""
    with pytest.raises(RunbookLoadError, match="cycle"):
        Catalog([_runbook("d.foundation", foundation=True, requires=["d.task"]), _runbook("d.task")])


def test_a_closure_over_the_bound_fails_the_catalog() -> None:
    def chain(length: int) -> list[Runbook]:
        links = [_runbook(f"d.r{i}", requires=[f"d.r{i + 1}"]) for i in range(length)]
        return [*links, _runbook(f"d.r{length}")]

    Catalog(chain(MAX_SUPPORTING_RUNBOOKS))  # exactly at the bound: fine
    with pytest.raises(RunbookLoadError, match="over the bound"):
        Catalog(chain(MAX_SUPPORTING_RUNBOOKS + 1))


def test_get_public_never_returns_the_hidden_runbook() -> None:
    catalog = Catalog([_runbook(DOCS_RUNBOOK_ID), _runbook("d.task")])
    assert catalog.get(DOCS_RUNBOOK_ID) is not None
    assert catalog.get_public(DOCS_RUNBOOK_ID) is None
    assert catalog.get_public("d.task") is not None
    assert catalog.get_public("d.nope") is None


# ---- the packaged catalog --------------------------------------------------------


def test_every_served_runbook_brings_the_general_foundation() -> None:
    catalog = load_catalog()
    for runbook in catalog.runbooks:
        if runbook.id not in catalog.selectable_ids or runbook.meta.foundation:
            continue
        supporting = [r.id for r in catalog.supporting_for([runbook.id])]
        assert supporting and supporting[0] == "daintree.foundation", runbook.id
