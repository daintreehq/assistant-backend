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

The register (`references/owed-trials.yaml`) holds every hub, lane and host-pane
trial a runbook still owes: who runs it, which CLI pairs, the exact prompts, what each
role must be seen to do, and the independent checks. `check` holds it to the rules in
`references/hub-lane-trials.md`: real runbook ids, actions the caller is offered, the
search line on the first prompt, no test words or runbook ids leaking into a prompt,
both CLIs covered within the two-trial cap, and the staged path asked only for what
one Mac can stage. Passing `check` proves a plan is well formed, nothing about how an
agent behaves.

Run it through `dtrb.sh plan ...`, which supplies the backend's interpreter.
"""

from __future__ import annotations

import argparse
import re
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
OPTIONAL_KEYS = {"needs", "also", "evidence", "execution", "lanes"}
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

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(entry["id"])):
        problems.append("id must be a lowercase slug")
    if not isinstance(entry["issue"], int):
        problems.append("issue must be the issue number")
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

    if entry["status"] not in STATUSES:
        problems.append(f"status must be one of {list(STATUSES)}")
    elif entry["status"] != "owed" and not entry.get("evidence"):
        problems.append(f"status {entry['status']} needs evidence")

    try:
        _strings(entry["setup"])
        _strings(entry.get("execution"))
        checks = _strings(entry["checks"])
    except TypeError as exc:
        return [*problems, f"setup/checks/execution: {exc}"]
    if not checks:
        problems.append("no independent checks: an agent's report is not an outcome")

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
        if n == 1 and not text.rstrip().endswith(SEARCH_LINE):
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
    """Landed hosts runbooks no entry exercises (as its runbook or in `also`)."""
    covered: set[str] = set()
    for entry in data["trials"]:
        if isinstance(entry, dict):
            covered.add(str(entry.get("runbook")))
            covered.update(_strings(entry.get("also")))
    return sorted(i for i in known_ids if i.startswith("daintree.hosts.") and i not in covered)


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
    out = [
        f"# {entry['id']}: {entry['runbook']} ({entry['status']}, issue #{entry['issue']})",
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


def load_catalog_ids() -> tuple[frozenset[str], str]:
    sys.path.insert(0, str(REPO / "src"))
    from daintree_assistant_server.runbooks.catalog import load_catalog

    catalog = load_catalog()
    return frozenset(catalog.known_ids), str(catalog.revision)


def load_surfaces() -> dict[str, frozenset[str]]:
    sys.path.insert(0, str(REPO / "tests"))
    import daintree_mcp_surfaces as surfaces

    return {name: frozenset(surfaces.action_ids(name)) for name in set(ROLE_SURFACE.values())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="trial_plan.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("check", "coverage", "show"):
        p = sub.add_parser(name)
        if name == "show":
            p.add_argument("id")
        p.add_argument("--register", type=Path, default=REGISTER)
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
        for p in problems:
            print(f"problem: {p}", file=sys.stderr)
        return 1 if problems else 0

    known, revision = load_catalog_ids()
    if args.cmd == "revision":
        print(revision)
        return 0
    data = load_register(args.register)
    if args.cmd == "coverage":
        missing = uncovered(data, known)
        for rid in missing:
            print(f"no owed or passed trial recorded: {rid}")
        hosts = sum(1 for rid in known if rid.startswith("daintree.hosts."))
        print(f"coverage: {hosts - len(missing)} of {hosts} hosts runbooks have an entry")
        return 1 if missing else 0
    if args.cmd == "show":
        entry = next((e for e in data["trials"] if e.get("id") == args.id), None)
        if entry is None:
            print(f"no entry {args.id}", file=sys.stderr)
            return 2
        print(runsheet(entry, revision), end="")
        return 0
    problems = register_problems(data, known, load_surfaces())
    for p in problems:
        print(f"problem: {p}")
    owed = sum(1 for e in data["trials"] if isinstance(e, dict) and e.get("status") == "owed")
    print(f"register: {len(data['trials'])} entries, {owed} owed, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
