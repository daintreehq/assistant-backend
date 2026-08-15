"""No skill may instruct a raw MCP action that has acquired a typed wrapper.

The CLI keeps a DENYLIST: raw MCP action name -> the typed wrapper that replaced
it. Calling a denied action through `daintree.call` does not degrade — it returns
`USE_TYPED_WRAPPER` and does nothing. So a skill still naming one is a runbook
that asks the user to approve a system-tier call which cannot execute.

This has already happened. `forge.getChecks` was added to the CLI and
`forge.getCIStatus` denied; four skills went on instructing the raw call across a
submodule bump, and nothing noticed — `test_skill_catalog_hygiene` checks
`requiredTools` against the tool-name snapshot, and these were never tools. They
were MCP actions reached through the generic `daintree.call`, which is itself a
perfectly valid tool. The gap was structural, not an oversight.

Read from the vendored CLI source rather than a copy kept here, so the two cannot
disagree: the deny-list IS the CLI's, and moving the submodule pin moves this
check with it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "src" / "daintree_assistant_server" / "skills" / "files"
DISCOVERY_GO = REPO_ROOT / "cli" / "internal" / "tools" / "mcpx" / "discovery.go"

# Skills that NAME a denied action while telling the model not to call it. A
# warning is the opposite of an instruction and is worth keeping — the learned
# worktree skill earns its place by explaining exactly which raw call breaks and
# how. Listed explicitly so the exemption is a decision someone made, not a
# pattern that quietly swallows real drift.
REVIEWED_WARNINGS: set[tuple[str, str]] = set()


# Entries that must survive any parse, spanning the block from first to last. A
# truncated parse silently drops actions from the END, and each dropped action is
# one whose check quietly stops running — the failure mode this whole file exists
# to prevent, one level up. Anchors at both ends catch a truncation anywhere.
_DENYLIST_ANCHORS = ("agent.launch", "terminal.sendCommand", "forge.getCIStatus")


def _go_map_body(source: str, decl: str) -> str:
    """The body of a Go map literal, found by BRACE BALANCE, not by a regex.

    Every regex terminator tried here was defeated by the map's own contents: the
    wrapper prose describes JSON, so it contains braces, and a Go RAW string (
    backticks) may hold a real newline followed by a column-zero `}` — gofmt does
    not indent raw-string contents. A newline-brace or column-zero terminator ends the block
    there and silently drops every entry after it, which disables one action's
    check per dropped entry.

    So this walks the literal, tracking whether it is inside an interpreted
    string, a raw string, or a comment. It is a few lines more than a regex and it
    cannot be fooled by the values it is skipping over.
    """
    head = source.index(decl) + len(decl)
    depth, i, n = 1, head, len(source)
    while i < n and depth:
        ch = source[i]
        if ch == '"':                                   # interpreted string
            i += 1
            while i < n and source[i] != '"':
                i += 2 if source[i] == "\\" else 1
        elif ch == "`":                                 # raw string, may span lines
            i = source.index("`", i + 1)
        elif source.startswith("//", i):
            i = source.index("\n", i)
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if not depth:
                return source[head:i]
        i += 1
    raise AssertionError(f"unbalanced braces after {decl!r}")


# Entries that must survive any parse, spanning the block from first to last. A
# truncated parse silently drops actions from the END, and each dropped action is
# one whose check quietly stops running — the failure mode this whole file exists
# to prevent, one level up. Anchors at both ends catch a truncation anywhere.
_DENYLIST_ANCHORS = ("agent.launch", "terminal.sendCommand", "forge.getCIStatus")

_MAP_DECL = "var wrappedMCPTools = map[string]string{"


def _denied_actions() -> dict[str, str]:
    """The CLI's `wrappedMCPTools` map: raw action -> replacement wrapper."""
    source = DISCOVERY_GO.read_text(encoding="utf-8")
    assert _MAP_DECL in source, (
        f"cannot find `{_MAP_DECL}` in the vendored "
        f"{DISCOVERY_GO.relative_to(REPO_ROOT)}. The CLI changed how it declares the "
        "typed-wrapper denylist, so this gate can no longer read it — which means "
        "skills naming a superseded raw action would stop being caught."
    )
    block = _go_map_body(source, _MAP_DECL)
    # Keys only. The value is prose naming the replacement — one entry names four
    # alternative wrappers — so treating it as structured metadata would add a
    # second silent-drift boundary for no gain.
    pairs = re.findall(r'^\s*"([\w.\-/]+)":\s*(.+?),?\s*$', block, re.M)
    return {name: replacement.strip().strip('"`') for name, replacement in pairs}


def _skill_files() -> list[Path]:
    return sorted(SKILLS_DIR.glob("*.md"))


# How far after a `daintree.call` the action name can sit and still belong to that
# invocation. Wide enough for `{"arguments": {...}, "name": "x"}` — the catalog
# writes the payload both ways round — and far short of the next paragraph.
_CALL_SPAN_CHARS = 240


def _normalize_action(raw: str) -> str:
    """The CLI's `normalizeMCPName`: strip whitespace, lowercase.

    Mirrored rather than approximated. A checker stricter than the runtime it
    models reports a clean catalog for a body the runtime would refuse, which is
    the one result worse than no checker.
    """
    return "".join(raw.split()).lower()


def _instructs(body: str, action: str) -> bool:
    """Does this body carry a `daintree.call` INVOCATION naming ``action``?

    Two syntaxes, because the catalog uses both and roughly evenly — 10 shorthand
    against 9 JSON at the time of writing. A checker that knew only the JSON form
    would have covered about half the real usage while reading like a gate:

        daintree.call {"name": "forge.getCIStatus", "arguments": {…}}
        daintree.call forge.getCIStatus({…})

    Bound to a SPAN after each `daintree.call`, not to the file. Searching the
    whole body for the two halves independently flags a skill that mentions the
    escape hatch in one paragraph and quotes an unrelated `"name"` field in
    another — and a catalog full of false positives gets the gate deleted.
    """
    target = _normalize_action(action)
    for match in re.finditer(r"daintree[._]call", body, re.I):
        span = body[match.end() : match.end() + _CALL_SPAN_CHARS]
        # JSON payload: "name": "<action>", in any quoting the runbooks use.
        for candidate in re.findall(r"""["'`]name["'`]\s*:\s*["'`]([^"'`]+)["'`]""", span):
            if _normalize_action(candidate) == target:
                return True
        # Shorthand: `daintree.call <action>` — the next dotted token.
        shorthand = re.match(r"""[\s`'"(]*([A-Za-z][\w]*\.[\w.]+)""", span)
        if shorthand and _normalize_action(shorthand.group(1)) == target:
            return True
    return False


@pytest.fixture(scope="module")
def denied() -> dict[str, str]:
    if not DISCOVERY_GO.exists():
        pytest.skip("cli/ submodule is not populated (git submodule update --init cli)")
    actions = _denied_actions()
    missing = [a for a in _DENYLIST_ANCHORS if a not in actions]
    assert not missing, (
        f"the denylist parsed as {len(actions)} entries but is missing {missing} — "
        f"the parse truncated, and every action after the cut has silently stopped "
        f"being checked"
    )
    return actions


def test_the_denylist_parses_and_names_the_known_case(denied: dict[str, str]) -> None:
    """Guards the parser itself. A regex that silently matched nothing would make
    every test below vacuous, and the failure mode of this whole file is a gate
    that quietly stops gating."""
    assert "forge.getCIStatus" in denied
    assert "forge.getChecks" in denied["forge.getCIStatus"]


def test_no_skill_instructs_a_raw_action_that_has_a_typed_wrapper(
    denied: dict[str, str],
) -> None:
    """The gate.

    Matches the CALL SHAPE — a `"name": "<action>"` payload — rather than a bare
    mention, because a skill saying "do NOT send this through daintree.call" is
    doing the right thing and must not be punished for naming what it forbids.
    """
    offenders: list[str] = []
    for path in _skill_files():
        body = path.read_text(encoding="utf-8")
        if "daintree.call" not in body:
            continue
        for action, wrapper in denied.items():
            if not _instructs(body, action):
                continue
            if (path.stem, action) in REVIEWED_WARNINGS:
                continue
            offenders.append(f"{path.stem} calls {action} — use {wrapper}")

    assert not offenders, (
        "these skills instruct a raw MCP action the CLI has superseded with a typed "
        "wrapper. `daintree.call` returns USE_TYPED_WRAPPER for them and does "
        "nothing, so the runbook asks the user to approve a call that cannot run:\n  "
        + "\n  ".join(offenders)
    )


def test_the_gate_would_catch_the_drift_it_was_written_for(denied: dict[str, str]) -> None:
    """A synthetic body in the exact shape four real skills carried, so the check
    is shown to fire rather than assumed to."""
    body = (
        "1. READ THE STATE. For a PR, CI comes through the raw escape hatch:\n"
        '`daintree.call {"name":"forge.getCIStatus","arguments":{"prNumber":<int>}}`\n'
    )
    hits = [a for a in denied if "daintree.call" in body and _instructs(body, a)]
    assert hits == ["forge.getCIStatus"]


def test_a_warning_against_a_denied_action_is_not_flagged(denied: dict[str, str]) -> None:
    """The negative control, and the reason the check matches a call shape rather
    than a mention: the skill that documents the trap is the most useful one in
    the catalog on this subject."""
    body = (
        "Do NOT send this action through `daintree.call`, and do not improvise "
        "`git checkout`. Use forge.getChecks instead of forge.getCIStatus.\n"
    )
    hits = [a for a in denied if _instructs(body, a)]
    assert hits == []


@pytest.mark.parametrize(
    "payload",
    [
        '`daintree.call {"name":"forge.getCIStatus","arguments":{}}`',
        "daintree.call({'name': 'forge.getCIStatus', 'arguments': {}})",
        'daintree.call {"name": "Forge.GetCIStatus"}',
        'daintree.call {"name" : " forge.getCIStatus "}',
        'daintree.call {"arguments":{"prNumber":1},"name":"forge.getCIStatus"}',
        # The SHORTHAND, which the catalog uses slightly more than the JSON form.
        # A checker that knew only the JSON shape covered about half the real
        # usage while reading like a gate.
        "use `daintree.call forge.getCIStatus` for the check state",
        "daintree.call forge.getCIStatus({ prNumber })",
        "daintree.call Forge.GetCIStatus",
    ],
    ids=["backticked", "single-quoted", "case-shifted", "padded", "name-last",
         "shorthand", "shorthand-called", "shorthand-case-shifted"],
)
def test_the_detection_matches_the_spellings_a_runbook_actually_uses(payload: str) -> None:
    """The runtime lowercases and strips before its denylist lookup, so every one
    of these is denied when it runs. A checker stricter than the thing it models
    reports a clean catalog and lets the drift through.

    Deliberately does NOT take the `denied` fixture: these exercise the matcher,
    not the CLI source, so they must keep running where the submodule is absent.
    """
    assert _instructs(payload, "forge.getCIStatus"), f"missed: {payload}"


@pytest.mark.parametrize(
    "body",
    [
        # The escape hatch named in one paragraph, an unrelated `name` field in
        # another. Searching the whole file for the two halves independently
        # flags this, and a gate that cries wolf on the catalog gets deleted.
        'Prefer a typed read over a raw daintree.call.\n\n'
        + "x" * 400
        + '\n\nThe roster row is `{"name": "forge.getCIStatus"}`.',
        # A different action entirely, close by.
        'daintree.call {"name": "git.commit", "arguments": {}}',
        # Shorthand for a different action.
        "daintree.call worktree.compareDiff",
    ],
    ids=["far-apart", "different-action-json", "different-action-shorthand"],
)
def test_the_detection_does_not_fire_on_unrelated_text(body: str) -> None:
    assert not _instructs(body, "forge.getCIStatus")


def test_a_truncated_denylist_parse_is_caught_not_silently_narrowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `}` at line start inside a wrapper's prose used to end the block early,
    dropping every entry after it — and each dropped entry is one action whose
    check quietly stops running. The anchors span the block so a cut anywhere
    fails loudly."""
    import test_wrapper_drift as module

    truncated = tmp_path / "discovery.go"
    # A Go RAW string containing a real newline and a column-zero brace. gofmt
    # does not indent raw-string contents, so this is the shape that actually
    # defeats a `^\}` terminator — the earlier version of this test used `\\n}`,
    # a source escape, and so never exercised the case it claimed to.
    truncated.write_text(
        "var wrappedMCPTools = map[string]string{\n"
        '\t"agent.launch": "agentTask.spawnForEdits",\n'
        "\t\"terminal.sendCommand\": `see the shape:\n}\n`,\n"
        '\t"forge.getCIStatus": "forge.getChecks",\n'
        "}\n"
    )
    monkeypatch.setattr(module, "DISCOVERY_GO", truncated)
    actions = module._denied_actions()
    assert set(_DENYLIST_ANCHORS) <= set(actions), (
        "the column-zero terminator did not survive a brace inside a value"
    )
