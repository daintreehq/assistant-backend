"""Every runbook says what its steps do when the work's machine is a Remote Host.

The Shell's assistant can follow any runbook against a Remote Host, through direct host
control (the hosts foundation: the work's machine, `host.call`, `project.runCheck` or
`project.runCommand` there, else an agent there). A step written for the caller's own
shell (`git -C <path>`, `lsof`, a test run, a transcript under `~/.claude`) names a path
or a process that, on a host, exists only there. So every runbook carries one top-level
`On a host:` line ahead of its procedure, and a body that acts on a machine names the
routes its steps take there:

- checkout and machine work (git, tests, processes, files): run there, per the hosts
  foundation, or by name (`host.call`, `project.runCheck`, `project.runCommand`, an agent
  there). A shared runbook is also served to v0.44, which has no `host.call` or
  `project.runCommand`, so its line refers to the hosts foundation instead;
- the forge (`gh`): your own shell, since pushed state is the same from every machine.

"Delegate" is not a route: under direct control it means handing work to the host's
assistant, which is not the default.

Nothing here changes a caller with no hosts: the line is conditional. The check is a
lint, claimed narrowly. It proves the line is there with the routes the body needs, not
that every step is placed correctly, that a route word is not negated, or that a model
follows it. Only a live trial against a host shows that.

The hosts runbooks themselves (`daintree.hosts.*`) are written for direct host control,
and foundations are delivered with them, so neither carries the line.
"""

from __future__ import annotations

import re

from daintree_assistant_server.runbooks.catalog import DOCS_RUNBOOK_ID, load_catalog

# A command is its first word at the start of a code span, a fenced line or a pipeline
# stage, so prose that only mentions a git worktree is not one.
_COMMAND_START = r"(?:^[ \t]*|(?<=`)|(?<=\| )|(?<=&& )|(?<=; )|(?<=\$\())"

#: Commands that act on the machine they run on: its checkouts, processes, files,
#: toolchains and credentials.
_MACHINE_COMMAND = re.compile(
    _COMMAND_START + r"(?:git|lsof|ss|ps|curl|kill|pkill|killall|readlink|shasum|sha256sum"
    r"|mktemp|tar|unzip|grep|rg|du|df|uptime|vm_stat|memory_pressure|sysctl|free|docker"
    r"|kubectl|gcloud|npm|npx|pnpm|yarn|claude|codex|cd|ls|cat|find|jq|sed|awk|python3?"
    r"|pytest|make|bash|sh|node)(?=[ `])",
    re.M,
)
#: Git named in prose ("run git status in the worktree"); `git worktree` is left out
#: because prose says it of the concept.
_PROSE_GIT = re.compile(
    r"\bgit (?:-C\b|status|diff|log|show|fetch|push|pull|rev-parse|rev-list|merge-base"
    r"|ls-remote|reflog|stash|reset|checkout|switch|commit|add|rebase|merge|cherry-pick"
    r"|bisect|clean|restore)\b"
)
_MACHINE_PHRASE = re.compile(
    r"\b(?:own file tools|natively|native git|worktree(?:'s)? path|on disk)\b", re.I
)
#: "Your own shell" is the work's machine, except in a sentence about the forge, where
#: it is the forge route itself.
_SHELL_PHRASE = re.compile(r"\b(?:own shell|your shell)\b", re.I)
_MACHINE_PATH = re.compile(
    r"~/\.claude\b|~/\.codex\b|\$\{?(?:CLAUDE_CONFIG_DIR|CODEX_HOME"
    r"|DAINTREE_ASSISTANT_SCRATCH_DIR|TMPDIR)\b|(?<![\w.])/tmp\b|<[\w ]*path>/"
)
#: The forge CLI reads and writes pushed state, the same from every machine.
_FORGE_COMMAND = re.compile(_COMMAND_START + r"gh(?=[ `])", re.M)
_FORGE_WORDS = re.compile(r"`gh\b|\bforge\b")

_HOST_LINE = re.compile(r"^On a host: \S[^\n]*$", re.M)
_MACHINE_ROUTE = re.compile(
    r"\bhosts foundation\b|`host\.call`|`project\.run(?:Check|Command)`|\ban agent there\b", re.I
)
_FORGE_ROUTE = re.compile(r"\bown shell\b", re.I)
# CommonMark fences: at most three spaces of indent; a backtick fence's info string
# holds no backtick; the closing run is the same character, at least as long.
_FENCE_OPEN = re.compile(r" {0,3}(`{3,}(?=[^`]*$)|~{3,})")
_FENCE_CLOSE = re.compile(r" {0,3}(`{3,}|~{3,})[ \t]*$")
_FIRST_STEP = re.compile(r"^1\. ", re.M)


def _blank(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def _outside_fences(body: str) -> str:
    """The body with every fenced block blanked, offsets kept. An unclosed fence runs to
    the end, as Markdown renders it."""
    out: list[str] = []
    fence = ""
    for line in body.splitlines(keepends=True):
        if fence:
            closing = _FENCE_CLOSE.match(line)
            if closing and closing.group(1)[0] == fence[0] and len(closing.group(1)) >= len(fence):
                fence = ""
            out.append(_blank(line))
        elif opened := _FENCE_OPEN.match(line):
            fence = opened.group(1)
            out.append(_blank(line))
        else:
            out.append(line)
    return "".join(out)


def _sentence(text: str, match: re.Match[str]) -> str:
    start = max(text.rfind(". ", 0, match.start()), text.rfind("\n", 0, match.start())) + 1
    ends = [i for i in (text.find(". ", match.end()), text.find("\n", match.end())) if i >= 0]
    return text[start : min(ends, default=len(text))]


def _machine_hit(text: str) -> re.Match[str] | None:
    """The first sign that a body acts on a machine, if it does."""
    hits = [
        m
        for m in (
            _MACHINE_COMMAND.search(text),
            _PROSE_GIT.search(text),
            _MACHINE_PATH.search(text),
            _MACHINE_PHRASE.search(text),
            next(
                (
                    m
                    for m in _SHELL_PHRASE.finditer(text)
                    if not _FORGE_WORDS.search(_sentence(text, m))
                ),
                None,
            ),
        )
        if m
    ]
    return min(hits, key=lambda m: m.start(), default=None)


def host_problems(body: str) -> list[str]:
    """What a body lacks to say how it runs on a host; empty when it says so."""
    prose = _outside_fences(body)
    lines = list(_HOST_LINE.finditer(prose))
    if not lines:
        return ["no top-level `On a host:` line"]
    if len(lines) > 1:
        return [f"{len(lines)} top-level `On a host:` lines; keep one"]
    # Briefs and examples count: a command an agent is told to run is still run on a
    # machine. The line itself does not.
    scanned = _HOST_LINE.sub(lambda m: _blank(m.group(0)), body)
    machine = _machine_hit(scanned)
    forge = _FORGE_COMMAND.search(scanned)
    # The line comes before the procedure: its first numbered step, or, in a body
    # without one, its first step on a machine.
    first_step = _FIRST_STEP.search(prose)
    starts = [m.start() for m in (machine, forge) if m]
    if lines[0].start() > (first_step.start() if first_step else min(starts, default=len(body))):
        return ["its `On a host:` line comes after the procedure starts"]
    route = lines[0].group(0)
    problems: list[str] = []
    if machine and not _MACHINE_ROUTE.search(route):
        problems.append(
            f"acts on a machine ({machine.group(0).strip()!r}) but its `On a host:` line "
            "names no route there (the hosts foundation, `host.call`, `project.runCheck`, "
            "`project.runCommand` or an agent there)"
        )
    if forge and not _FORGE_ROUTE.search(route):
        problems.append("runs `gh` but its `On a host:` line does not keep it in your own shell")
    return problems


def test_every_runbook_says_what_happens_on_a_host() -> None:
    offenders: dict[str, list[str]] = {}
    for runbook in load_catalog().runbooks:
        if runbook.id.startswith("daintree.hosts.") or runbook.meta.foundation:
            continue
        if runbook.id == DOCS_RUNBOOK_ID:
            continue
        problems = host_problems(runbook.body)
        if problems:
            offenders[runbook.id] = problems
    assert not offenders, (
        "runbooks that do not say what their steps do when the work's machine is a host "
        "(one top-level `On a host:` line before the procedure; docs/RUNBOOKS.md): "
        f"{offenders}"
    )


def test_the_scanner_catches_what_it_exists_for() -> None:
    """Guard the guard against a pattern that silently matches nothing."""
    runner = "On a host: shell steps run there, per the hosts foundation.\n\n"
    forge = "On a host: own shell: `gh -R <repo>`.\n\n"
    both = "On a host: shell steps run there, per the hosts foundation; own shell: `gh`.\n\n"
    tools = "On a host: call these tools on the work's machine.\n\n"
    named = (
        "On a host: each step through `host.call`.\n\n",
        "On a host: checks by its `project.runCheck`.\n\n",
        "On a host: shell steps by `project.runCommand` (Full).\n\n",
        "On a host: call these tools there; an agent there runs any shell step.\n\n",
    )
    # Routes the direct-control model does not have: delegating is handing work to the
    # host's assistant, and "command runner" names no tool.
    retired = "On a host: shell steps via its command runner, or delegate.\n\n"

    # Every way a body acts on a machine needs a machine route.
    for step in (
        "1. Read `git -C <path> status`.\n",
        "1. Run git status in the worktree.\n",
        "1. Run:\n\n```sh\ngit -C <path> status\n```\n",
        "1. `cd <path>; make test`.\n",
        "1. `python -m pytest` there.\n",
        "1. Read `<path>/package.json`.\n",
        "1. Read the transcript under `~/.codex/sessions`.\n",
        "1. Save it under `$TMPDIR`.\n",
        "1. Check the tree in your own shell.\n",
        '1. Brief: "run `npm test`".\n',
    ):
        assert host_problems(runner + step) == [], step
        for line in named:
            assert host_problems(line + step) == [], (line, step)
        assert host_problems(tools + step), step
        assert host_problems(retired + step), step
    # `gh` needs the forge route wherever it is written.
    for step in ("1. Read `gh pr view <n>`.\n", "1. Run:\n\n```sh\ngh pr view 123\n```\n"):
        assert host_problems(forge + step) == [], step
        assert host_problems(runner + step), step
    assert host_problems(both + "1. Read `git status` and `gh pr view <n>`.\n") == []
    # Prose about a git worktree is not a command; a forge step in your own shell is the
    # forge route, not a step on the work's machine.
    assert host_problems(tools + "1. Each git worktree has a branch.\n") == []
    assert host_problems(forge + "1. Run `gh pr view <n>` in your own shell.\n") == []
    assert host_problems(tools + "1. Read files on disk and compare them with the forge.\n")

    # One line, at top level, before the procedure starts.
    step = "1. Read `git -C <path> status`.\n"
    assert host_problems(step) == ["no top-level `On a host:` line"]
    assert host_problems(step + "\n" + runner)
    assert host_problems("- Run `git status`.\n\n" + runner)
    assert host_problems(runner + runner + step)
    assert host_problems("```text\n" + runner + "```\n\n" + step)
    assert host_problems("````markdown\n```\n" + runner + "```\n````\n\n" + step)
    assert host_problems("```text\n" + runner + step)
    # Four spaces of indent do not close a fence; a backtick in the info string means
    # it never opened.
    assert host_problems("```text\nexample\n    ```\n" + runner + step)
    assert host_problems("``` a `span`\n\n" + runner + step) == []
