#!/usr/bin/env python3
"""Offline planning for hub and lane trials. Launches nothing, writes nothing.

    trial_plan.py check    [--register FILE]   validate the owed-trial register
    trial_plan.py coverage [--register FILE]   landed daintree.hosts.* runbooks with no entry
    trial_plan.py show ID  [--register FILE]   one entry as an operator's runsheet
    trial_plan.py layout RUN [--hosts N] [--root DIR] [--project NAME]
                             [--mcp-base PORT] [--runbooks-port PORT]
                                               a staged run's paths, ports and transcript
                                               locations, checked for isolation
    trial_plan.py revision                     the catalog revision a trial must see served
    trial_plan.py scenarios [--issue N] [--runbook ID] [--family F] [--minimum]
                                               failure and recovery scenarios an issue,
                                               runbook or family names, with what each owes
    trial_plan.py grounding [--app-repo DIR]   every scenario citation read at app_sha
                                               (reads the app checkout; writes nothing)

The register (`references/owed-trials.yaml`) holds every hub, lane and host-pane
trial a runbook still owes: who runs it, which CLI pairs, the exact prompts, what each
role must be seen to do, and the independent checks. `check` holds it to the rules in
`references/hub-lane-trials.md`: real runbook ids, actions the caller is offered, the
search line on the first prompt, no test words or runbook ids leaking into a prompt,
both CLIs covered within the two-trial cap, and the staged path asked only for what
one Mac can stage. Passing `check` proves a plan is well formed, nothing about how an
agent behaves.

The scenarios (`references/failure-scenarios.yaml`) are reusable failure and recovery
runs an issue names by id. `check` also holds them to their rules: each injected
condition is a result value the captured output schema of an action that role is
offered declares (or a cited app error code), each scenario cites the app, and the
minimum staged subset is bounded and runs both CLIs in both roles. `coverage` also
lists observations and owner issues no scenario covers.

Run it through `dtrb.sh plan ...`, which supplies the backend's interpreter.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
SKILL = HERE.parents[1]
REPO = HERE.parents[4]
REGISTER = SKILL / "references" / "owed-trials.yaml"

CLIS = ("claude", "codex")
#: The captured surface each caller is checked against.
ROLE_SURFACE = {"hub": "hub-full", "lane": "lane-full", "pane": "pane-full"}
#: Which roles a trial may expect things of, by the caller the runbook is written for.
CALLER_ROLES = {"hub": {"hub", "lane"}, "lane": {"hub", "lane"}, "pane": {"pane"}}
PATHS = ("staged", "fleet")
STATUSES = ("owed", "partial", "passed")
#: One Mac stages a Shell and at most this many host instances.
STAGED_MAX_HOSTS = 2
#: Conditions one Mac cannot honestly stand in for.
FLEET_ONLY_NEEDS = frozenset(
    {
        "real-ssh",  # a network hop, a real sshd, real latency and link loss
        "linux-host",  # host sockets under /run/user; staging is macOS-only
        "headless-host",  # a host nobody is logged in at, start at login
        "power",  # sleep and keep-awake
        "distinct-sign-ins",  # a host whose CLIs sign in differently (e.g. Codex only)
        "separate-loopback",  # one port held on the Shell and another on a host: one Mac
        # has one loopback, so two listeners cannot both hold the same port
    }
)
#: Every other need a register entry may declare.
STAGEABLE_NEEDS = frozenset(
    {
        "shell-restart",
        "host-restart",
        "start-consent",
        "unattended-confirmation",
        "busy-host",
    }
)
MAX_COMBOS = 2
SEARCH_LINE = (
    "Before acting, use search_runbooks from the daintree-runbooks MCP to find the "
    "procedure for this and follow it."
)
#: What a prompt may name, filled in by the operator from the stage or fleet.
PLACEHOLDERS = frozenset(
    {"host_a", "host_b", "project", "remote", "branch", "sha", "sha_b", "sha_c", "run", "port"}
)
HOST_PLACEHOLDERS = {"host_a": 1, "host_b": 2}
#: Words that leak the test into what an agent reads (trials.md, Gotchas).
_LEAKS = (
    (re.compile(r"\bdaintree\.[a-z]", re.I), "a runbook id"),
    (re.compile(r"(?<![-_])\brunbooks?\b", re.I), "the word runbook"),
    (re.compile(r"\btrials?\b", re.I), "the word trial"),
    (re.compile(r"\bharness\b", re.I), "the word harness"),
)
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
NOTICES = {
    # electron/services/mcp-server/terminalNotify.ts, formatExchangeUpdateLine
    "hub-update": r"^Daintree: delegated exchange update \(exchanges? [A-Za-z0-9, -]+\)\.",
    # formatLaneWakeLine
    "lane-wake": r"^Daintree: assistant inbox has (a new message|\d+ new messages) "
    r"\(exchanges? [A-Za-z0-9, -]+\)\. Read (it|them) with assistant\.getInbox\.",
    "pane-notice": r"^Daintree: ",
}
ENTRY_KEYS = {
    "id",
    "runbook",
    "issue",
    "caller",
    "combos",
    "path",
    "hosts",
    "status",
    "setup",
    "steps",
    "checks",
}
OPTIONAL_KEYS = {"needs", "also", "evidence", "execution", "lanes", "discovery"}
#: `discovery: entry` marks a run of the app's own role-entry path: an ordinary request on
#: the hub and an inbox wake on the lane, with no instruction to search. Every other entry
#: hands the assistant the search line, which proves it can follow a procedure it was told
#: to look up, not that its own prompt leads it to look one up (`hub-lane-trials.md`,
#: Paired role evidence).
DISCOVERY_MODES = frozenset({"entry"})
_LANE_HOST = re.compile(r"host_([a-z])")
EXPECT_KEYS = {"selected", "calls", "never", "notices"}
#: The person's own Daintree listens here by default; a staged instance never does.
PERSON_MCP_PORT = 45454
#: macOS sun_path is 104 bytes with its NUL (electron/remote/host/hostSocketPath.ts).
MAC_SOCKET_LIMIT = 103
#: A path an agent reads must not tell it it is being tested.
_PATH_LEAK = re.compile(r"test|trial|runbook|e2e|harness", re.I)
#: A stage root's own name, and a project folder's: one plain path component.
_SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")

# Failure and recovery scenarios (`references/failure-scenarios.yaml`): reusable fault
# definitions an issue names by id instead of repeating the matrix.
SCENARIOS = SKILL / "references" / "failure-scenarios.yaml"
#: What a run needs, whether a register entry or a scenario carries it.
RUN_KEYS = ENTRY_KEYS - {"id", "issue", "status"}
RUN_OPTIONAL = OPTIONAL_KEYS - {"evidence"}
SCENARIO_KEYS = {
    "id",
    "family",
    "observes",
    "invariants",
    "issues",
    "inject",
    "conditions",
    "grounding",
    "staged",
    "real_host",
}
SCENARIO_OPTIONAL = {"run", "app_tests"}
#: Staged and real-host evidence: owed, passed with its record, or n/a with the reason.
_EVIDENCE = re.compile(r"owed|passed: \S.*|n/a: \S.*")
#: A citation of the app at the pinned revision: path:line, then what is there.
_CITATION = re.compile(r"[\w./@-]+\.[A-Za-z]+:\d+(-\d+)? \S.*")
#: The minimum staged subset is bounded: two trials per scenario.
MINIMUM_MAX = 6
#: Codes the app raises as tool errors rather than result values, so the captured
#: output schemas cannot show them. At the register's app_sha: a lane's messaging
#: refusals (`AssistantMessagingRefusalCode`, shared/types/assistantMessaging.ts:217-227),
#: an unanswered confirmation (electron/services/mcp-server/shared.ts:323) and a
#: renderer that never came back (shared.ts:326).
APP_ERROR_CODES = frozenset(
    {
        "NOT_ELIGIBLE",
        "INVALID_ARGS",
        "UNKNOWN_TASK",
        "EXCHANGE_CLOSED",
        "TOO_MANY_RESPONSES",
        "PAUSED",
        "HOP_LIMIT",
        "TOO_LARGE",
        "OUTBOX_FULL",
        "UNAVAILABLE",
        "CONFIRMATION_TIMEOUT",
        "SESSION_BINDING_GONE",
    }
)


# --------------------------------------------------------------------------- register


def load_register(path: Path = REGISTER) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("trials"), list):
        raise ValueError(f"{path}: expected a mapping with a 'trials' list")
    return data


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise TypeError("expected a string or a list of strings")


def _prompt_problems(text: str, hosts: int) -> list[str]:
    problems = []
    body = text.replace(SEARCH_LINE, "")
    for pattern, what in _LEAKS:
        if pattern.search(body):
            problems.append(f"names {what}")
    for name in _PLACEHOLDER.findall(text):
        if name not in PLACEHOLDERS:
            problems.append(f"unknown placeholder {{{name}}}")
        elif HOST_PLACEHOLDERS.get(name, 0) > hosts:
            problems.append(f"{{{name}}} but the entry stages {hosts} host(s)")
    if len(text) > 2000:
        problems.append("longer than 2000 characters")
    return problems


def _expect_problems(
    expect: Any,
    caller: str,
    known_ids: frozenset[str],
    surfaces: Mapping[str, frozenset[str]],
) -> tuple[list[str], dict[str, set[str]], dict[str, set[str]]]:
    """Problems in one step's `expect`, and the calls and selections it expects, each
    per role."""
    problems: list[str] = []
    calls: dict[str, set[str]] = {}
    selected: dict[str, set[str]] = {}
    if expect is None:
        return problems, calls, selected
    if not isinstance(expect, dict):
        return ["expect must map roles to observations"], calls, selected
    for role, obs in expect.items():
        if role not in CALLER_ROLES[caller]:
            problems.append(f"expects of role '{role}', which a {caller} trial does not run")
            continue
        if not isinstance(obs, dict) or not set(obs) <= EXPECT_KEYS:
            problems.append(f"{role}: observations must be among {sorted(EXPECT_KEYS)}")
            continue
        surface = surfaces[ROLE_SURFACE[role]]
        try:
            sel = _strings(obs.get("selected"))
            want = _strings(obs.get("calls"))
            never = _strings(obs.get("never"))
            notices = _strings(obs.get("notices"))
        except TypeError as exc:
            problems.append(f"{role}: {exc}")
            continue
        if unknown := sorted(set(sel) - known_ids):
            problems.append(f"{role}: selects unknown runbooks {unknown}")
        for label, ids in (("calls", want), ("never", never)):
            if off := sorted(set(ids) - surface):
                problems.append(f"{role}: {label} {off} not offered on {ROLE_SURFACE[role]}")
        if both := sorted(set(want) & set(never)):
            problems.append(f"{role}: {both} both expected and forbidden")
        if bad := sorted(set(notices) - set(NOTICES)):
            problems.append(f"{role}: unknown notices {bad}")
        calls.setdefault(role, set()).update(want)
        selected.setdefault(role, set()).update(sel)
    return problems, calls, selected


def entry_problems(
    entry: Any,
    known_ids: frozenset[str],
    surfaces: Mapping[str, frozenset[str]],
) -> list[str]:
    """Everything wrong with one register entry, as plain sentences."""
    if not isinstance(entry, dict):
        return ["not a mapping"]
    problems: list[str] = []
    if missing := sorted(ENTRY_KEYS - set(entry)):
        problems.append(f"missing {missing}")
    if extra := sorted(set(entry) - ENTRY_KEYS - OPTIONAL_KEYS):
        problems.append(f"unknown keys {extra}")
    if problems:
        return problems

    if not _SLUG.fullmatch(str(entry["id"])):
        problems.append("id must be a lowercase slug")
    if not isinstance(entry["issue"], int):
        problems.append("issue must be the issue number")
    if entry["status"] not in STATUSES:
        problems.append(f"status must be one of {list(STATUSES)}")
    elif entry["status"] != "owed" and not entry.get("evidence"):
        problems.append(f"status {entry['status']} needs evidence")
    return problems + run_problems(entry, known_ids, surfaces)


def run_problems(
    entry: dict[str, Any],
    known_ids: frozenset[str],
    surfaces: Mapping[str, frozenset[str]],
) -> list[str]:
    """Everything wrong with what a trial runs (caller, combos, hosts, setup, steps,
    checks): shared by a register entry and a scenario that carries its own run."""
    problems: list[str] = []
    runbooks = [entry["runbook"], *(_strings(entry.get("also")))]
    if unknown := sorted(set(runbooks) - known_ids):
        problems.append(f"unknown runbooks {unknown}")

    caller = entry["caller"]
    if caller not in CALLER_ROLES:
        return [*problems, f"caller must be one of {sorted(CALLER_ROLES)}"]

    combos = entry["combos"]
    keys = {"pane"} if caller == "pane" else {"hub", "lane"}
    if not isinstance(combos, list) or not combos:
        problems.append("combos must be a non-empty list")
        combos = []
    elif len(combos) > MAX_COMBOS:
        problems.append(f"{len(combos)} combos: at most {MAX_COMBOS} trials per runbook")
    seen: set[tuple[tuple[str, str], ...]] = set()
    for combo in combos:
        if not isinstance(combo, dict) or not set(combo) <= keys or not combo:
            problems.append(f"combo {combo!r}: keys must be among {sorted(keys)}")
            continue
        if bad := sorted(v for v in combo.values() if v not in CLIS):
            problems.append(f"combo {combo!r}: unknown CLIs {bad}")
        key = tuple(sorted(combo.items()))
        if key in seen:
            problems.append(f"combo {combo!r} twice")
        seen.add(key)
    covered = {c.get(caller) for c in combos if isinstance(c, dict)}
    if combos and covered != set(CLIS):
        problems.append(f"combos must run both {list(CLIS)} as the {caller}")
    if caller == "lane" and any(isinstance(c, dict) and "hub" not in c for c in combos):
        problems.append("a lane trial is reached through a hub: every combo names its hub")

    hosts = entry["hosts"]
    if not isinstance(hosts, int) or hosts < 0:
        problems.append("hosts must be a count")
        hosts = 0
    # `lanes` fixes each host's lane CLI (a host signed in to one CLI only) instead of
    # each combo choosing one.
    lanes = entry.get("lanes")
    if lanes is not None:
        if caller != "hub":
            problems.append("only a hub trial fixes lane CLIs per host; a lane trial varies them")
        elif not isinstance(lanes, dict) or not lanes:
            problems.append("lanes must map hosts to CLIs")
        else:
            for host, cli in lanes.items():
                m = _LANE_HOST.fullmatch(str(host))
                if not m or ord(m.group(1)) - ord("a") >= hosts:
                    problems.append(f"lanes: {host} is not one of the entry's {hosts} host(s)")
                if cli not in CLIS:
                    problems.append(f"lanes: {host} runs unknown CLI {cli!r}")
            if any(isinstance(c, dict) and "lane" in c for c in combos):
                problems.append("lanes fixes each host's lane CLI: combos name only the hub")
    try:
        needs = set(_strings(entry.get("needs")))
    except TypeError as exc:
        problems.append(f"needs: {exc}")
        needs = set()
    if bad := sorted(needs - FLEET_ONLY_NEEDS - STAGEABLE_NEEDS):
        problems.append(f"unknown needs {bad}")
    if entry["path"] not in PATHS:
        problems.append(f"path must be one of {list(PATHS)}")
    elif entry["path"] == "staged":
        if fleet := sorted(needs & FLEET_ONLY_NEEDS):
            problems.append(f"needs {fleet} cannot be staged on one Mac: path must be fleet")
        if hosts > STAGED_MAX_HOSTS:
            problems.append(f"{hosts} hosts: the staged path holds {STAGED_MAX_HOSTS}")

    try:
        _strings(entry["setup"])
        _strings(entry.get("execution"))
        checks = _strings(entry["checks"])
    except TypeError as exc:
        return [*problems, f"setup/checks/execution: {exc}"]
    if not checks:
        problems.append("no independent checks: an agent's report is not an outcome")

    discovery = entry.get("discovery")
    if discovery is not None and not (
        isinstance(discovery, str) and discovery in DISCOVERY_MODES
    ):
        problems.append(f"discovery must be one of {sorted(DISCOVERY_MODES)}")
    steps = entry["steps"]
    if not isinstance(steps, list) or not steps:
        return [*problems, "steps must be a non-empty list"]
    expected_calls: dict[str, set[str]] = {}
    expected_selected: dict[str, set[str]] = {}
    roles_used: set[str] = set()
    prompts: list[str] = []
    for n, step in enumerate(steps, 1):
        if not isinstance(step, dict) or len({"say", "person"} & set(step)) != 1:
            problems.append(f"step {n}: exactly one of 'say' or 'person'")
            continue
        if extra := sorted(set(step) - {"say", "person", "expect"}):
            problems.append(f"step {n}: unknown keys {extra}")
        if "person" in step:
            if "expect" in step:
                problems.append(f"step {n}: a person step expects nothing; put it on a 'say'")
            continue
        text = step["say"]
        if not isinstance(text, str) or not text.strip():
            problems.append(f"step {n}: empty prompt")
            continue
        prompts.append(text)
        if discovery == "entry":
            if SEARCH_LINE in text or "search_runbooks" in text:
                problems.append(f"step {n}: an entry-discovery run gives no search instruction")
        elif n == 1 and not text.rstrip().endswith(SEARCH_LINE):
            problems.append("step 1: the first prompt must end with the search line")
        problems.extend(f"step {n}: {p}" for p in _prompt_problems(text, hosts))
        found, calls, selected = _expect_problems(step.get("expect"), caller, known_ids, surfaces)
        problems.extend(f"step {n}: {p}" for p in found)
        if isinstance(step.get("expect"), dict):
            roles_used |= set(step["expect"]) & CALLER_ROLES[caller]
        for role, ids in calls.items():
            expected_calls.setdefault(role, set()).update(ids)
        for role, ids in selected.items():
            expected_selected.setdefault(role, set()).update(ids)
    if steps and isinstance(steps[0], dict) and "say" not in steps[0]:
        problems.append("step 1 must be a prompt: setup belongs in 'setup'")
    if entry["runbook"] not in expected_selected.get(caller, set()):
        problems.append(f"no step expects the {caller} to receive {entry['runbook']}")
    for combo in combos:
        named = set(combo) | ({"lane"} if lanes else set()) if isinstance(combo, dict) else set()
        if isinstance(combo, dict) and (unnamed := sorted(roles_used - named)):
            problems.append(f"combo {combo!r} names no CLI for {unnamed}, which steps expect of")
    launches = set().union(*expected_calls.values()) & {"agent.launch", "agent.launchMany"}
    if launches and not any("haiku" in p.lower() for p in prompts):
        problems.append("launches agents but no prompt asks for model haiku (cost cap)")
    return problems


def register_problems(
    data: dict[str, Any],
    known_ids: frozenset[str],
    surfaces: Mapping[str, frozenset[str]],
) -> list[str]:
    problems: list[str] = []
    ids: set[str] = set()
    for i, entry in enumerate(data["trials"]):
        name = entry.get("id", f"#{i}") if isinstance(entry, dict) else f"#{i}"
        if name in ids:
            problems.append(f"{name}: id used twice")
        ids.add(str(name))
        problems.extend(f"{name}: {p}" for p in entry_problems(entry, known_ids, surfaces))
    return problems


def uncovered(data: dict[str, Any], known_ids: Iterable[str]) -> list[str]:
    """Landed hosts runbooks no entry exercises (as its runbook or in `also`). A
    scenario does not count: each runbook's own issue owes its own run."""
    covered: set[str] = set()
    for entry in data["trials"]:
        if isinstance(entry, dict):
            covered.add(str(entry.get("runbook")))
            covered.update(_strings(entry.get("also")))
    return sorted(i for i in known_ids if i.startswith("daintree.hosts.") and i not in covered)


# --------------------------------------------------------------------------- scenarios


def load_scenarios(path: Path = SCENARIOS) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("scenarios"), list):
        raise ValueError(f"{path}: expected a mapping with a 'scenarios' list")
    return data


def output_values(schema: Any, field: str) -> set[str]:
    """Every enumerated value a captured output schema allows for a property named
    `field`, at any depth (an `outcome`, a task's `state`, a host's `connection`)."""
    found: set[str] = set()

    def enums(node: Any) -> None:
        if isinstance(node, dict):
            found.update(str(v) for v in node.get("enum") or [])
            for key in ("anyOf", "oneOf", "items"):
                if key in node:
                    enums(node[key])
        elif isinstance(node, list):
            for item in node:
                enums(item)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict) and field in props:
                enums(props[field])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return found


def condition_problems(
    cond: Any,
    roles: set[str],
    outputs: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """A scenario's injected condition must be one the app's contract declares: a
    result value in the captured output schema of an action the role is offered, or
    an error code the app raises (cited in APP_ERROR_CODES)."""
    if not isinstance(cond, dict):
        return ["a condition must be a mapping"]
    if bad := sorted(str(k) for k, v in cond.items() if not isinstance(v, str)):
        return [f"condition values must be plain strings, not {bad}"]
    if set(cond) == {"error"}:
        if cond["error"] not in APP_ERROR_CODES:
            return [f"error {cond['error']!r} is not an app error code in APP_ERROR_CODES"]
        return []
    role, action = cond.get("role"), cond.get("action")
    if role not in roles:
        return [f"condition role {role!r}: this run has only {sorted(roles)}"]
    surface = ROLE_SURFACE[str(role)]
    schema = outputs.get(surface, {}).get(str(action))
    if schema is None:
        return [f"condition {action!r} is not offered on {surface}"]
    fields = sorted(set(cond) - {"role", "action"})
    if len(fields) != 1:
        return [f"condition on {action} must name exactly one result field, not {fields}"]
    field = fields[0]
    allowed = output_values(schema, field)
    if not allowed:
        return [f"{action} declares no enumerated {field!r} on {surface}"]
    if cond[field] not in allowed:
        return [f"{action} {field} {cond[field]!r} is not among the captured {sorted(allowed)}"]
    return []


def _is_slug(value: Any) -> bool:
    return isinstance(value, str) and _SLUG.fullmatch(value) is not None


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _family_ok(tags: Any, issues: Any) -> bool:
    """A family names at least one observation (slugs) and at least one owner issue."""
    return (
        isinstance(tags, list)
        and bool(tags)
        and all(_is_slug(t) for t in tags)
        and isinstance(issues, list)
        and bool(issues)
        and all(isinstance(i, int) and not isinstance(i, bool) for i in issues)
    )


def _citations(value: Any, label: str) -> list[str]:
    try:
        cites = _strings(value)
    except TypeError as exc:
        return [f"{label}: {exc}"]
    return [f"{label}: {c!r} is not 'path:line what'" for c in cites if not _CITATION.fullmatch(c)]


def scenario_problems(
    sc: Any,
    known_ids: frozenset[str],
    surfaces: Mapping[str, frozenset[str]],
    outputs: Mapping[str, Mapping[str, Any]],
    entries: Mapping[str, dict[str, Any]],
    observations: Mapping[str, str],
    invariants: Iterable[str],
) -> list[str]:
    """Everything wrong with one scenario. `entries` are the register's entries by id
    (a scenario may run one of them); `observations` maps each observation to its
    family."""
    if not isinstance(sc, dict):
        return ["not a mapping"]
    problems: list[str] = []
    if missing := sorted(SCENARIO_KEYS - set(sc)):
        problems.append(f"missing {missing}")
    inline = set(sc) & (RUN_KEYS | RUN_OPTIONAL)
    if "run" in sc and inline:
        problems.append(f"runs register entry {sc['run']!r} and its own {sorted(inline)}")
    elif "run" not in sc and (lacking := sorted(RUN_KEYS - inline)):
        problems.append(f"no register 'run' and no run of its own: missing {lacking}")
    known_keys = SCENARIO_KEYS | SCENARIO_OPTIONAL | RUN_KEYS | RUN_OPTIONAL
    if extra := sorted(set(sc) - known_keys):
        problems.append(f"unknown keys {extra}")
    if problems:
        return problems

    if not _SLUG.fullmatch(str(sc["id"])):
        problems.append("id must be a lowercase slug")
    families = set(observations.values())
    if not isinstance(sc["family"], str) or sc["family"] not in families:
        problems.append(f"family must be one of {sorted(families)}")
    try:
        observes = _strings(sc["observes"])
        named = _strings(sc["invariants"])
        _strings(sc["inject"])
    except TypeError as exc:
        return [*problems, f"observes/invariants/inject: {exc}"]
    if not observes:
        problems.append("observes nothing")
    elif unknown := sorted(set(observes) - set(observations)):
        problems.append(f"observes unknown {unknown}")
    elif str(sc["family"]) not in {observations[o] for o in observes}:
        problems.append(f"observes nothing of its own family {sc['family']}")
    if not named:
        problems.append("names no invariant")
    elif unknown := sorted(set(named) - set(invariants)):
        problems.append(f"unknown invariants {unknown}")
    issues = sc["issues"]
    if not isinstance(issues, list) or not issues or not all(isinstance(i, int) for i in issues):
        problems.append("issues must be a non-empty list of issue numbers")
    if not str(sc["inject"]).strip():
        problems.append("inject must say what is injected and when")
    problems += _citations(sc["grounding"], "grounding")
    if not sc["grounding"]:
        problems.append("grounding: at least one citation of the app")
    problems += _citations(sc.get("app_tests"), "app_tests")

    if "run" in sc:
        run = entries.get(str(sc["run"]))
        if run is None:
            return [*problems, f"run {sc['run']!r} is not a register entry"]
    else:
        run = sc
        problems += run_problems(sc, known_ids, surfaces)
    caller = run.get("caller")
    roles = set(CALLER_ROLES.get(str(caller), set()))

    # Empty only when the fault lives in the world (a listener, a branch, a file).
    conds = sc["conditions"]
    if not isinstance(conds, list):
        problems.append("conditions must list what the app returns under the fault")
    else:
        for cond in conds:
            problems += condition_problems(cond, roles, outputs)

    for tier in ("staged", "real_host"):
        if not isinstance(sc[tier], str) or not _EVIDENCE.fullmatch(sc[tier]):
            problems.append(f"{tier} must be 'owed', 'passed: <record>' or 'n/a: <why>'")
    if run.get("path") == "fleet" and not str(sc["staged"]).startswith("n/a:"):
        problems.append("a fleet run cannot be staged: staged must be 'n/a: <why>'")
    if run.get("path") == "staged" and str(sc["staged"]).startswith("n/a:"):
        problems.append("a staged run's staged evidence is owed or passed, never n/a")
    if str(sc["staged"]).startswith("n/a:") and str(sc["real_host"]).startswith("n/a:"):
        problems.append("staged and real_host both n/a: nothing would ever run it")
    return problems


def scenarios_problems(
    data: dict[str, Any],
    register: dict[str, Any],
    known_ids: frozenset[str],
    surfaces: Mapping[str, frozenset[str]],
    outputs: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """The scenario file: its families, invariants, every scenario, and the bounded
    minimum staged subset."""
    problems: list[str] = []
    if data.get("app_sha") != register.get("app_sha"):
        problems.append("app_sha differs from the register's: ground both at one revision")
    observations: dict[str, str] = {}
    families = data.get("families")
    if not isinstance(families, dict) or not families:
        return [*problems, "families must map each family to its observations and issues"]
    for family, spec in families.items():
        if not isinstance(spec, dict) or set(spec) != {"title", "observations", "issues"}:
            problems.append(f"family {family}: needs exactly title, observations, issues")
            continue
        tags, issues = spec["observations"], spec["issues"]
        if not isinstance(tags, list) or not tags or not all(_is_slug(t) for t in tags):
            problems.append(f"family {family}: observations must be a list of slugs")
            continue
        if not _family_ok(tags, issues):
            problems.append(f"family {family}: issues must be a list of issue numbers")
        for tag in tags:
            if tag in observations:
                problems.append(f"observation {tag} in both {observations[tag]} and {family}")
            observations[tag] = str(family)
    invariants = data.get("invariants")
    if not isinstance(invariants, dict) or not invariants:
        problems.append("invariants must map each invariant to what an outcome check shows")
        invariants = {}

    entries = {str(e["id"]): e for e in register["trials"] if isinstance(e, dict) and "id" in e}
    ids: set[str] = set()
    by_id: dict[str, dict[str, Any]] = {}
    for i, sc in enumerate(data["scenarios"]):
        name = str(sc.get("id", f"#{i}")) if isinstance(sc, dict) else f"#{i}"
        if name in ids:
            problems.append(f"{name}: id used twice")
        elif name in entries:
            problems.append(f"{name}: id is also a register entry's")
        ids.add(name)
        found = scenario_problems(
            sc, known_ids, surfaces, outputs, entries, observations, invariants
        )
        problems += [f"{name}: {p}" for p in found]
        if isinstance(sc, dict):
            by_id[name] = sc

    minimum = data.get("minimum_staged")
    if not isinstance(minimum, list) or not minimum:
        return [*problems, "minimum_staged must list the staged subset run first"]
    if len(minimum) > MINIMUM_MAX:
        problems.append(f"minimum_staged: {len(minimum)} scenarios, at most {MINIMUM_MAX}")
    pairs: set[tuple[str, str]] = set()
    for sid in minimum:
        sc = by_id.get(str(sid))
        if sc is None:
            problems.append(f"minimum_staged: no scenario {sid}")
            continue
        run = entries.get(str(sc.get("run")), {}) if "run" in sc else sc
        if run.get("path") != "staged":
            problems.append(f"minimum_staged: {sid} is not a staged run")
        for combo in run.get("combos") or []:
            if isinstance(combo, dict) and "hub" in combo and "lane" in combo:
                pairs.add((str(combo["hub"]), str(combo["lane"])))
    hubs, lanes = {h for h, _ in pairs}, {lane for _, lane in pairs}
    if hubs != set(CLIS) or lanes != set(CLIS):
        problems.append("minimum_staged must run both CLIs as hub and both as lane")
    if not any(h != lane for h, lane in pairs):
        problems.append("minimum_staged must include a mixed-CLI pair")
    return problems


_CITED = re.compile(r"([\w./@-]+\.[A-Za-z]+):(\d+)(?:-(\d+))? (.*)")


def grounding_problems(data: dict[str, Any], read: Any) -> list[str]:
    """Every scenario citation against the app at app_sha: the file exists and holds
    the cited lines, and an app test's title is on its cited line. `read(path)` returns
    the file's text at that revision, or None."""
    problems: list[str] = []
    files: dict[str, list[str] | None] = {}
    for sc in data["scenarios"]:
        if not isinstance(sc, dict):
            continue
        cites = [(c, False) for c in _strings(sc.get("grounding"))]
        cites += [(c, True) for c in _strings(sc.get("app_tests"))]
        for cite, is_test in cites:
            m = _CITED.fullmatch(cite)
            if m is None:
                continue  # the shape is check's to report
            path, first, last, what = m.group(1), int(m.group(2)), m.group(3), m.group(4)
            if path not in files:
                text = read(path)
                files[path] = None if text is None else text.splitlines()
            lines = files[path]
            end = int(last) if last else first
            if lines is None:
                problems.append(f"{sc.get('id')}: {path} is not in the app at app_sha")
            elif not 1 <= first <= end <= len(lines):
                problems.append(f"{sc.get('id')}: {path} has {len(lines)} lines, not {first}-{end}")
            elif is_test and what[:24] not in lines[first - 1]:
                problems.append(f"{sc.get('id')}: {path}:{first} is not the test {what[:40]!r}")
    return problems


def scenario_gaps(data: dict[str, Any]) -> list[str]:
    """Observations no scenario observes, and family owner issues no scenario maps.
    Malformed parts are `check`'s to report; here they are a gap, never a pass."""
    families = data.get("families")
    if not isinstance(families, dict) or not families:
        return ["no families to cover"]
    scenarios = [s for s in data["scenarios"] if isinstance(s, dict)]
    observed = {o for s in scenarios for o in _list(s.get("observes")) if isinstance(o, str)}
    mapped = {i for s in scenarios for i in _list(s.get("issues")) if isinstance(i, int)}
    gaps: list[str] = []
    for family, spec in families.items():
        tags = _list(spec.get("observations")) if isinstance(spec, dict) else []
        issues = _list(spec.get("issues")) if isinstance(spec, dict) else []
        if not _family_ok(tags, issues):
            gaps.append(f"family {family} is malformed (plan check)")
            continue
        gaps += [f"no scenario observes {family}: {t}" for t in tags if t not in observed]
        gaps += [f"no scenario maps issue #{i} ({family})" for i in issues if i not in mapped]
    return gaps


def scenario_run(sc: dict[str, Any], register: dict[str, Any]) -> dict[str, Any]:
    """The run a scenario names: a register entry, or its own."""
    if "run" not in sc:
        return sc
    for entry in register["trials"]:
        if isinstance(entry, dict) and entry.get("id") == sc["run"]:
            return entry
    raise ValueError(f"scenario {sc.get('id')}: run {sc['run']!r} is not a register entry")


def select_scenarios(
    data: dict[str, Any],
    register: dict[str, Any],
    *,
    issue: int | None = None,
    runbook: str | None = None,
    family: str | None = None,
    minimum: bool = False,
) -> list[dict[str, Any]]:
    """The scenarios an issue, a runbook or a family names, or the minimum subset."""
    chosen = []
    keep = {str(s) for s in data.get("minimum_staged") or []}
    for sc in data["scenarios"]:
        if issue is not None and issue not in sc["issues"]:
            continue
        if family is not None and sc["family"] != family:
            continue
        if minimum and sc["id"] not in keep:
            continue
        if runbook is not None:
            run = scenario_run(sc, register)
            if runbook not in {run["runbook"], *_strings(run.get("also"))}:
                continue
        chosen.append(sc)
    return chosen


def scenario_table(chosen: list[dict[str, Any]], register: dict[str, Any]) -> str:
    """One line per scenario, then what the selection still owes, by tier."""
    out = []
    for sc in chosen:
        run = scenario_run(sc, register)
        runbooks = ", ".join([run["runbook"], *_strings(run.get("also"))])
        app = len(_strings(sc.get("app_tests")))
        out += [
            f"{sc['id']} [{sc['family']}] observes {', '.join(_strings(sc['observes']))}",
            f"    runbooks: {runbooks}",
            f"    contract: plan check | staged: {sc['staged']} | real host: {sc['real_host']}"
            f" | app tests: {app or 'none'}",
        ]
    staged = [s for s in chosen if s["staged"] == "owed"]
    real = [s for s in chosen if s["real_host"] == "owed"]
    trials = sum(len(scenario_run(s, register)["combos"]) for s in staged)
    out.append(
        f"{len(chosen)} scenarios; staged owed {len(staged)} ({trials} trials), "
        f"real host owed {len(real)}"
    )
    return "\n".join(out) + "\n"


def scenario_sheet(
    sc: dict[str, Any], register: dict[str, Any], revision: str | None = None
) -> str:
    """One scenario as the operator runs it: the fault and its evidence tiers, then
    the run's own runsheet."""
    run = scenario_run(sc, register)
    out = [
        f"# scenario {sc['id']} ({sc['family']}): issues "
        + ", ".join(f"#{i}" for i in sc["issues"]),
        "",
        f"Observes: {', '.join(_strings(sc['observes']))}",
        f"Invariants: {', '.join(_strings(sc['invariants']))}",
        f"Injected: {str(sc['inject']).strip()}",
        "Conditions (checked offline against the captured contract, never live proof):",
    ]
    for cond in sc["conditions"]:
        out.append("- " + ", ".join(f"{k} {v}" for k, v in cond.items()))
    if not sc["conditions"]:
        out.append("- none: the fault is in the world, not a value the app returns")
    out += [
        f"Evidence: contract checked by plan check; staged {sc['staged']}; "
        f"real host {sc['real_host']}",
        "Grounding (app source at app_sha):",
        *(f"- {c}" for c in _strings(sc["grounding"])),
        "App harness (the app's own behaviour, never runbook following):",
        *(f"- {c}" for c in _strings(sc.get("app_tests")) or ["none"]),
    ]
    if "run" in sc:
        out.append(f"Run: register entry {sc['run']}")
    out.append("")
    return "\n".join(out) + "\n" + runsheet(run, revision)


# --------------------------------------------------------------------------- runsheet


def runsheet(entry: dict[str, Any], revision: str | None = None) -> str:
    """One entry as the operator follows it."""
    caller = {
        "hub": "the hub: the sidebar assistant in the Shell's own project",
        "lane": "a lane: a host project's sidebar assistant, reached through the hub",
        "pane": "an agent pane in a host's project, opened from the Shell",
    }[entry["caller"]]
    path = {
        "staged": "staged on one Mac (hub-lane-trials.md, Staged path)",
        "fleet": "person-assisted on real machines (hub-lane-trials.md, Fleet path)",
    }[entry["path"]]
    combos = "; ".join(
        " + ".join(f"{role} {cli}" for role, cli in combo.items()) for combo in entry["combos"]
    )
    owner = "a scenario's own run"
    if "issue" in entry:
        owner = f"{entry['status']}, issue #{entry['issue']}"
    out = [
        f"# {entry['id']}: {entry['runbook']} ({owner})",
        "",
        f"Caller: {caller}",
        f"Path: {path}; hosts: {entry['hosts']}",
        f"Trials (one per combo): {combos}",
    ]
    if entry.get("lanes"):
        fixed = ", ".join(f"{host} {cli}" for host, cli in entry["lanes"].items())
        out.append(f"Lanes, fixed per host: {fixed}")
    if entry.get("needs"):
        out.append(f"Needs: {', '.join(_strings(entry['needs']))}")
    if entry.get("also"):
        out.append(f"Also exercises: {', '.join(_strings(entry['also']))}")
    if entry.get("discovery") == "entry":
        out.append(
            "Entry discovery: type the prompts as written and nothing more; the assistants' own "
            "prompts must lead each to search (record the query, or that none was made)"
        )
    if revision:
        out.append(f"Every search result must carry catalog_revision {revision}")
    out += ["", "## Setup", *(f"{n}. {s}" for n, s in enumerate(_strings(entry["setup"]), 1))]
    out += ["", "## Steps"]
    for n, step in enumerate(entry["steps"], 1):
        if "person" in step:
            out.append(f"{n}. Person: {step['person'].strip()}")
            continue
        target = "agent pane" if entry["caller"] == "pane" else "hub"
        out.append(f"{n}. Type into the {target}:")
        out += [f"   > {line}" for line in step["say"].strip().splitlines()]
        for role, obs in (step.get("expect") or {}).items():
            parts = []
            for key in ("selected", "calls", "never", "notices"):
                if values := _strings(obs.get(key)):
                    parts.append(f"{key} {', '.join(values)}")
            out.append(f"   Expect {role}: {'; '.join(parts)}")
    if execution := _strings(entry.get("execution")):
        out += ["", "## Execution checks (layer 4: from transcripts, never the outcome)"]
        out += [f"- {c}" for c in execution]
    out += ["", "## Outcome checks (layer 5: by you, on the world, never read off a reply)"]
    out += [f"- {c}" for c in _strings(entry["checks"])]
    out += [
        "",
        "## Record ($W/trials/<id>-<hub>-<lane>.md, one per combo)",
        "- run id, app SHA, backend SHA, catalog_revision seen by each role",
        "- per role: transcript path, search query, selected ids, actions with arguments,",
        "  notice lines (Daintree: ...) and when they arrived",
        "- each independent check: the command and what it printed",
        "- verdict PASS / PARTIAL / FAIL with the class of each finding",
        "  (RUNBOOK / MODEL / ROUTING / APP / HARNESS), and the proof layer reached",
    ]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- layout


def _claude_glob(sessions_dir: str) -> str:
    """Where Claude Code keeps the transcripts of sessions under `sessions_dir`: its
    project folder name is the cwd with every non-alphanumeric character as '-'
    (summarise.py, claude_dir)."""
    return f"~/.claude/projects/{re.sub(r'[^A-Za-z0-9]', '-', sessions_dir)}-*"


def layout(
    run: str,
    *,
    hosts: int = 1,
    root: str | None = None,
    project: str = "tally",
    mcp_base: int = 45610,
    runbooks_port: int = 8611,
    home: str | None = None,
    tmp: str | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """A staged run's paths and ports, and every isolation rule they break.

    Pure: nothing is created or probed. The host's userData sits exactly where a
    Shell probing that sshd session's HOME looks for the discovery file
    (`remoteHostSocketLocation`, `MAC_APP_DIR_NAME`), and the host runs with that
    staged HOME: enabling Host mode removes a start-at-login file under HOME
    (`HostModeService.setEnabled`), which must never be the person's. Its CLIs, and
    the app's own sign-in check, reach the person's sign-ins through the `links`.

    The root is one new folder directly in the temporary directory, so removing it
    at teardown, and matching processes by it, can reach nothing else."""
    problems: list[str] = []
    real_home = home or str(Path.home())
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,15}", run):
        problems.append("run id must be a short lowercase slug (at most 16 characters)")
    if not 1 <= hosts <= STAGED_MAX_HOSTS:
        problems.append(f"hosts must be 1..{STAGED_MAX_HOSTS} on the staged path")
    if not _PROJECT_NAME.fullmatch(project):
        problems.append("project must be one plain folder name")
    tmp_dir = tmp or str(Path("/tmp").resolve())
    base = root or f"{tmp_dir}/dsw-{run}"
    parent, _, name = base.rpartition("/")
    if parent != tmp_dir or not _SAFE_NAME.fullmatch(name):
        problems.append(f"root must be one new lowercase folder directly in {tmp_dir}, not {base}")
    if _PATH_LEAK.search(base):
        problems.append(f"root {base} tells agents they are being tested")
    runbooks_url = f"http://127.0.0.1:{runbooks_port}/v1/daintree/mcp"

    shell_ud = f"{base}/shell/userdata"
    ports = {"shell": mcp_base}
    roles: dict[str, Any] = {
        "shell": {
            "user_data": shell_ud,
            "mcp_port": mcp_base,
            "project": f"{base}/shell/work/{project}",
            "ssh_wrapper": f"{base}/shell/bin/ssh",
            "env": {
                "DAINTREE_SSH": f"{base}/shell/bin/ssh",
                "DAINTREE_RUNBOOKS_MCP_URL": runbooks_url,
            },
            "hub_sessions": f"{shell_ud}/help-sessions",
            "claude_transcripts": _claude_glob(f"{shell_ud}/help-sessions"),
        }
    }
    for i in range(1, max(hosts, 0) + 1):
        h = f"{base}/h{i}"
        sshd_home = f"{h}/home"
        user_data = f"{sshd_home}/Library/Application Support/Daintree"
        socket = f"{user_data}/host.sock"
        port = mcp_base + i
        ports[f"h{i}"] = port
        if len(socket.encode()) > MAC_SOCKET_LIMIT:
            problems.append(
                f"h{i}: host socket is {len(socket.encode())} bytes, over macOS's "
                f"{MAC_SOCKET_LIMIT}: use a shorter --root"
            )
        if sshd_home == real_home:
            problems.append(
                f"h{i}: the sshd HOME is the real home; the Shell would find the "
                "person's own Daintree"
            )
        roles[f"h{i}"] = {
            "sshd_dir": h,
            "sshd_home": sshd_home,
            "user_data": user_data,
            "host_socket": socket,
            "discovery_file": f"{user_data}/host.json",
            "mcp_port": port,
            "env": {"HOME": sshd_home, "DAINTREE_RUNBOOKS_MCP_URL": runbooks_url},
            # The app reads sign-ins under HOME (CliAvailabilityService.checkAuth joins
            # homedir() with each CLI's config path), as the CLIs do by default.
            "links": {
                f"{sshd_home}/{name}": f"{real_home}/{name}"
                for name in (".claude", ".claude.json", ".codex")
            },
            "project": f"{h}/work/{project}",
            "lane_sessions": f"{user_data}/help-sessions",
            "claude_transcripts": _claude_glob(f"{user_data}/help-sessions"),
            "worker_cwds": f"{h}/work",
        }
    if len(set(ports.values())) != len(ports):
        problems.append("two instances share an MCP port")
    for name, port in ports.items():
        if port == PERSON_MCP_PORT:
            problems.append(f"{name}: MCP port {port} is the person's own Daintree's default")
        if not 1024 < port < 65536:
            problems.append(f"{name}: MCP port {port} out of range")
    if runbooks_port in ports.values():
        problems.append("the runbook server shares a port with an instance")
    plan = {
        "run": run,
        "root": base,
        "origin": f"{base}/origin.git",
        "runbooks_url": runbooks_url,
        "codex_transcripts": "~/.codex/sessions (match session_meta cwd to a role's sessions dir)",
        "owned_processes": f"argv contains {base}",
        "roles": roles,
    }
    return plan, problems


# --------------------------------------------------------------------------- loading


#: Hubs and lanes are the inbox model's (v0.44): this register and its protocol are
#: read against the inbox catalog and the capture frozen with it. The control
#: catalog's hosts runbooks, followed by a Shell's assistant through `host.call`, owe
#: trials of their own that this protocol does not describe.
MODEL = "inbox"


def load_catalog_ids() -> tuple[frozenset[str], str]:
    sys.path.insert(0, str(REPO / "src"))
    from daintree_assistant_server.runbooks.catalog import load_catalogs

    catalog = load_catalogs().for_model(MODEL)
    return frozenset(catalog.known_ids), str(catalog.revision)


def load_surfaces() -> dict[str, frozenset[str]]:
    sys.path.insert(0, str(REPO / "tests"))
    import daintree_mcp_surfaces as surfaces

    return {
        name: frozenset(surfaces.action_ids(name, MODEL)) for name in set(ROLE_SURFACE.values())
    }


def load_outputs() -> dict[str, dict[str, Any]]:
    """Each caller's captured output schemas, by action id."""
    sys.path.insert(0, str(REPO / "tests"))
    import daintree_mcp_surfaces as surfaces

    return {
        name: {
            str(tool["action_id"]): tool.get("outputSchema") or {}
            for tool in surfaces.capture(name, MODEL)["tools"]
        }
        for name in set(ROLE_SURFACE.values())
    }


def _default_app_repo() -> Path:
    """The app checkout: $DAINTREE_APP_REPO, else a `daintree` beside the main checkout."""
    if env := os.environ.get("DAINTREE_APP_REPO"):
        return Path(env)
    common = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    main_checkout = Path(common).parent if common else REPO
    return main_checkout.parent / "daintree"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trial_plan.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("check", "coverage", "show", "scenarios", "grounding"):
        p = sub.add_parser(name)
        if name == "show":
            p.add_argument("id")
        if name == "scenarios":
            p.add_argument("--issue", type=int)
            p.add_argument("--runbook")
            p.add_argument("--family")
            p.add_argument("--minimum", action="store_true")
        if name == "grounding":
            p.add_argument("--app-repo", type=Path, default=_default_app_repo())
        p.add_argument("--register", type=Path, default=REGISTER)
        p.add_argument("--scenarios", type=Path, default=SCENARIOS)
    lay = sub.add_parser("layout")
    lay.add_argument("run")
    lay.add_argument("--hosts", type=int, default=1)
    lay.add_argument("--root")
    lay.add_argument("--project", default="tally")
    lay.add_argument("--mcp-base", type=int, default=45610)
    lay.add_argument("--runbooks-port", type=int, default=8611)
    sub.add_parser("revision")
    args = parser.parse_args(argv)

    if args.cmd == "layout":
        plan, problems = layout(
            args.run,
            hosts=args.hosts,
            root=args.root,
            project=args.project,
            mcp_base=args.mcp_base,
            runbooks_port=args.runbooks_port,
        )
        print(yaml.safe_dump(plan, sort_keys=False, width=120), end="")
        for problem in problems:
            print(f"problem: {problem}", file=sys.stderr)
        return 1 if problems else 0

    known, revision = load_catalog_ids()
    if args.cmd == "revision":
        print(revision)
        return 0
    data = load_register(args.register)
    scenarios = load_scenarios(args.scenarios)
    if args.cmd == "coverage":
        missing = uncovered(data, known)
        for rid in missing:
            print(f"no owed or passed trial recorded: {rid}")
        hosts = sum(1 for rid in known if rid.startswith("daintree.hosts."))
        print(f"coverage: {hosts - len(missing)} of {hosts} hosts runbooks have an entry")
        gaps = scenario_gaps(scenarios)
        for gap in gaps:
            print(gap)
        print(f"scenarios: {len(scenarios['scenarios'])}, {len(gaps)} gaps")
        return 1 if missing or gaps else 0
    if args.cmd == "show":
        entry = next((e for e in data["trials"] if e.get("id") == args.id), None)
        if entry is not None:
            print(runsheet(entry, revision), end="")
            return 0
        sc = next((s for s in scenarios["scenarios"] if s.get("id") == args.id), None)
        if sc is None:
            print(f"no entry or scenario {args.id}", file=sys.stderr)
            return 2
        try:
            print(scenario_sheet(sc, data, revision), end="")
        except (ValueError, KeyError, TypeError) as exc:
            print(f"problem: {exc} (run plan check)", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "grounding":
        sha = str(scenarios["app_sha"])

        def read(path: str) -> str | None:
            done = subprocess.run(
                ["git", "-C", str(args.app_repo), "show", f"{sha}:{path}"],
                capture_output=True,
                text=True,
                check=False,
            )
            return done.stdout if done.returncode == 0 else None

        problems = grounding_problems(scenarios, read)
        for problem in problems:
            print(f"problem: {problem}")
        print(f"grounding: citations read at {sha[:10]}, {len(problems)} problems")
        return 1 if problems else 0
    if args.cmd == "scenarios":
        try:
            chosen = select_scenarios(
                scenarios,
                data,
                issue=args.issue,
                runbook=args.runbook,
                family=args.family,
                minimum=args.minimum,
            )
            print(scenario_table(chosen, data), end="")
        except (ValueError, KeyError, TypeError) as exc:
            print(f"problem: {exc} (run plan check)", file=sys.stderr)
            return 1
        return 0
    surfaces = load_surfaces()
    problems = register_problems(data, known, surfaces)
    problems += scenarios_problems(scenarios, data, known, surfaces, load_outputs())
    for problem in problems:
        print(f"problem: {problem}")
    owed = sum(1 for e in data["trials"] if isinstance(e, dict) and e.get("status") == "owed")
    print(f"register: {len(data['trials'])} entries, {owed} owed, {len(problems)} problems")
    print(f"scenarios: {len(scenarios['scenarios'])}, checked against the captured contract")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
