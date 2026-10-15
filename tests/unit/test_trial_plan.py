"""The runbook skill's offline planner for hub and lane trials (`trial_plan.py`).

The script lives with the skill's trial kit (`.claude/skills/runbook/scripts/trial/`),
so it is loaded by path. What is pinned: the committed owed-trial register holds to the
protocol in `references/hub-lane-trials.md` against the real catalog and captures; each
rule the planner enforces rejects the plan that breaks it; a staged layout refuses the
paths and ports that would reach the person's own Daintree; and the pane holder runs
agents on the user's sign-ins. None of this proves an agent's behaviour: only a live
trial does.
"""

from __future__ import annotations

import copy
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_KIT = Path(__file__).resolve().parents[2] / ".claude/skills/runbook/scripts/trial"
_SCRIPT = _KIT / "trial_plan.py"


@pytest.fixture(scope="module")
def tp() -> ModuleType:
    spec = importlib.util.spec_from_file_location("trial_plan", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["trial_plan"] = module
    spec.loader.exec_module(module)
    return module


KNOWN = frozenset({"daintree.hosts.ask", "daintree.hosts.work", "daintree.hosts.unused"})
SURFACES = {
    "hub-full": frozenset({"host.list", "host.sendMessage", "host.getTask", "agent.launch"}),
    "lane-full": frozenset({"assistant.getInbox", "assistant.reply", "agent.launch"}),
    "pane-full": frozenset({"terminal.list", "agent.launch"}),
}


def _entry(tp: ModuleType) -> dict[str, Any]:
    return {
        "id": "ask",
        "runbook": "daintree.hosts.ask",
        "issue": 1,
        "caller": "hub",
        "combos": [{"hub": "claude", "lane": "codex"}, {"hub": "codex", "lane": "claude"}],
        "path": "staged",
        "hosts": 1,
        "status": "owed",
        "setup": ["Stage one host."],
        "steps": [
            {
                "say": f"Ask {{host_a}} how {{branch}} is going.\n{tp.SEARCH_LINE}",
                "expect": {
                    "hub": {
                        "selected": ["daintree.hosts.ask"],
                        "calls": ["host.list", "host.sendMessage"],
                        "notices": ["hub-update"],
                    },
                    "lane": {"calls": ["assistant.getInbox", "assistant.reply"]},
                },
            },
            {"person": "Pause messaging."},
            {"say": "Ask again.", "expect": {"hub": {"never": ["host.sendMessage"]}}},
        ],
        "checks": ["One exchange in the Shell's record."],
    }


def _problems(tp: ModuleType, entry: dict[str, Any]) -> list[str]:
    result: list[str] = tp.entry_problems(entry, KNOWN, SURFACES)
    return result


def test_a_well_formed_entry_has_no_problems(tp: ModuleType) -> None:
    assert _problems(tp, _entry(tp)) == []


def test_the_committed_register_holds_to_the_protocol(tp: ModuleType) -> None:
    known, _revision = tp.load_catalog_ids()
    data = tp.load_register()
    assert tp.register_problems(data, known, tp.load_surfaces()) == []
    assert data["trials"], "the register is empty"


def _set(path: str, value: Any) -> Any:
    def apply(entry: dict[str, Any]) -> None:
        *head, last = path.split(".")
        node: Any = entry
        for key in head:
            node = node[int(key)] if key.isdigit() else node[key]
        if value is _DELETE:
            del node[last]
        else:
            node[int(last) if last.isdigit() else last] = value

    return apply


_DELETE = object()


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (_set("caller", "sidebar"), "caller must be one of"),
        (_set("runbook", "daintree.hosts.gone"), "unknown runbooks"),
        (_set("issue", "#1"), "issue must be the issue number"),
        (_set("checks", []), "no independent checks"),
        (_set("status", "passed"), "needs evidence"),
        (_set("path", "somewhere"), "path must be one of"),
        (_set("hosts", 3), "the staged path holds 2"),
        (_set("needs", ["real-ssh"]), "cannot be staged on one Mac"),
        (_set("needs", ["separate-loopback"]), "cannot be staged on one Mac"),
        (_set("needs", ["a-quiet-day"]), "unknown needs"),
        (_set("combos", [{"hub": "claude", "lane": "codex"}]), "must run both"),
        (
            _set(
                "combos",
                [{"hub": "claude"}, {"hub": "codex"}, {"hub": "claude", "lane": "claude"}],
            ),
            "at most 2 trials",
        ),
        (_set("combos.0.hub", "gemini"), "unknown CLIs"),
        (_set("steps.0.say", "Ask {host_a} how it is going."), "must end with the search line"),
        (_set("steps.2.say", "Use daintree.hosts.ask for this."), "names a runbook id"),
        (_set("steps.2.say", "This is a runbook check."), "names the word runbook"),
        (_set("steps.2.say", "Part of the trial."), "names the word trial"),
        (_set("steps.2.say", "Ask {host_b} too."), "stages 1 host"),
        (_set("steps.2.say", "Ask {hostname}."), "unknown placeholder"),
        (_set("steps.0.expect.lane.calls", ["host.sendMessage"]), "not offered on lane-full"),
        (_set("steps.0.expect.hub.never", ["host.list"]), "both expected and forbidden"),
        (_set("steps.0.expect.hub.notices", ["a-bell"]), "unknown notices"),
        (_set("steps.0.expect.pane", {"calls": ["terminal.list"]}), "role 'pane'"),
        (_set("steps.0.expect.hub.selected", ["daintree.hosts.work"]), "no step expects"),
        # Received by the wrong role: a lane cannot follow a hub procedure.
        (_set("steps.0.expect.hub.selected", _DELETE), "no step expects the hub"),
        (_set("combos", [{"hub": "claude"}, {"hub": "codex"}]), "names no CLI for ['lane']"),
        (_set("execution", [1]), "setup/checks/execution"),
        (_set("steps.1.expect", {"hub": {}}), "a person step expects nothing"),
        (_set("steps.0.expect.hub.calls", ["agent.launch"]), "model haiku"),
        (_set("setup", _DELETE), "missing ['setup']"),
    ],
)
def test_each_rule_rejects_the_plan_that_breaks_it(
    tp: ModuleType, mutate: Any, expected: str
) -> None:
    entry = copy.deepcopy(_entry(tp))
    mutate(entry)
    problems = _problems(tp, entry)
    assert any(expected in p for p in problems), problems


def test_selection_by_the_wrong_role_does_not_count(tp: ModuleType) -> None:
    entry = _entry(tp)
    entry["steps"][0]["expect"]["lane"]["selected"] = ["daintree.hosts.ask"]
    del entry["steps"][0]["expect"]["hub"]["selected"]
    problems = _problems(tp, entry)
    assert any("no step expects the hub to receive" in p for p in problems), problems


def test_a_lane_trial_names_its_hub_and_covers_both_lane_clis(tp: ModuleType) -> None:
    entry = _entry(tp)
    entry["caller"] = "lane"
    entry["steps"][0]["expect"]["lane"]["selected"] = ["daintree.hosts.ask"]
    del entry["steps"][0]["expect"]["hub"]["selected"]
    assert _problems(tp, entry) == []
    entry["combos"] = [{"lane": "claude"}, {"hub": "codex", "lane": "claude"}]
    problems = _problems(tp, entry)
    assert any("every combo names its hub" in p for p in problems), problems
    assert any("must run both" in p for p in problems), problems


def test_lanes_fix_each_hosts_cli_for_a_hub_trial(tp: ModuleType) -> None:
    entry = _entry(tp)
    entry.update(hosts=2, lanes={"host_a": "claude", "host_b": "codex"})
    entry["combos"] = [{"hub": "claude"}, {"hub": "codex"}]
    assert _problems(tp, entry) == []
    assert "Lanes, fixed per host: host_a claude, host_b codex" in tp.runsheet(entry)
    entry["combos"] = [{"hub": "claude", "lane": "codex"}, {"hub": "codex"}]
    entry["lanes"] = {"host_a": "claude", "host_c": "gemini"}
    problems = _problems(tp, entry)
    for expected in ("combos name only the hub", "host_c is not one of", "unknown CLI"):
        assert any(expected in p for p in problems), (expected, problems)
    entry["caller"] = "lane"
    assert any("only a hub trial" in p for p in _problems(tp, entry))


def test_a_fleet_entry_may_need_what_one_mac_cannot_stage(tp: ModuleType) -> None:
    entry = _entry(tp)
    entry.update(path="fleet", hosts=4, needs=["distinct-sign-ins", "real-ssh"])
    assert _problems(tp, entry) == []


def test_duplicate_ids_are_refused(tp: ModuleType) -> None:
    data = {"trials": [_entry(tp), _entry(tp)]}
    problems = tp.register_problems(data, KNOWN, SURFACES)
    assert any("id used twice" in p for p in problems), problems


def test_coverage_counts_also_and_ignores_other_domains(tp: ModuleType) -> None:
    entry = _entry(tp)
    entry["also"] = ["daintree.hosts.work"]
    known = KNOWN | {"daintree.agents.other"}
    assert tp.uncovered({"trials": [entry]}, known) == ["daintree.hosts.unused"]


def test_the_notice_patterns_match_the_apps_fixed_lines(tp: ModuleType) -> None:
    # electron/services/mcp-server/terminalNotify.ts at the pinned app revision.
    lane_one = "Daintree: assistant inbox has a new message (exchange ab12-cd). Read it with assistant.getInbox."
    lane_two = "Daintree: assistant inbox has 2 new messages (exchanges ab12, cd34). Read them with assistant.getInbox."
    hub_one = "Daintree: delegated exchange update (exchange ab12-cd)."
    hub_two = "Daintree: delegated exchange update (exchanges ab12, cd34)."
    assert re.match(tp.NOTICES["lane-wake"], lane_one)
    assert re.match(tp.NOTICES["lane-wake"], lane_two)
    assert re.match(tp.NOTICES["hub-update"], hub_one)
    assert re.match(tp.NOTICES["hub-update"], hub_two)
    assert not re.match(tp.NOTICES["hub-update"], lane_one)


def test_a_staged_layout_puts_each_host_where_the_shell_probe_looks(tp: ModuleType) -> None:
    plan, problems = tp.layout("r1", hosts=2, home="/Users/someone", tmp="/private/tmp")
    assert problems == []
    assert plan["root"] == "/private/tmp/dsw-r1"
    ports = [role["mcp_port"] for role in plan["roles"].values()]
    assert len(set(ports)) == len(ports) and 45454 not in ports
    for name in ("h1", "h2"):
        role = plan["roles"][name]
        assert role["user_data"] == f"{role['sshd_home']}/Library/Application Support/Daintree"
        assert role["host_socket"] == f"{role['user_data']}/host.sock"
        assert role["claude_transcripts"].startswith("~/.claude/projects/-private-tmp-dsw-r1-")
    assert plan["owned_processes"] == "argv contains /private/tmp/dsw-r1"
    # Hosts run on a staged HOME (Host mode removes a start-at-login file under HOME),
    # reaching the person's CLI sign-ins only through these.
    h1 = plan["roles"]["h1"]
    assert h1["env"]["HOME"] == h1["sshd_home"]
    assert h1["links"] == {
        f"{h1['sshd_home']}/{name}": f"/Users/someone/{name}"
        for name in (".claude", ".claude.json", ".codex")
    }
    assert "HOME" not in plan["roles"]["shell"]["env"]


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"root": "/private/tmp/" + "x" * 60}, "over macOS's 103"),
        ({"mcp_base": 45453}, "the person's own Daintree"),
        ({"root": "/private/tmp/runbook-r1"}, "being tested"),
        ({"root": "/Users/someone/Library/Application Support/Daintree"}, "directly in"),
        ({"root": "/"}, "directly in"),
        ({"root": "/Users/someone/.ssh"}, "directly in"),
        ({"root": "/private/tmp/../../Users/someone/Library"}, "directly in"),
        ({"root": "/private/tmp/dsw-r1/nested"}, "directly in"),
        ({"project": "../escape"}, "one plain folder name"),
        ({"runbooks_port": 45611}, "shares a port"),
        ({"hosts": 3}, "hosts must be 1..2"),
    ],
)
def test_a_staged_layout_refuses_what_would_break_isolation(
    tp: ModuleType, kwargs: dict[str, Any], expected: str
) -> None:
    _plan, problems = tp.layout("r1", home="/Users/someone", tmp="/private/tmp", **kwargs)
    assert any(expected in p for p in problems), problems


def test_a_run_id_must_be_a_short_slug(tp: ModuleType) -> None:
    _plan, problems = tp.layout("Run One", home="/Users/someone", tmp="/private/tmp")
    assert any("short lowercase slug" in p for p in problems), problems


def test_the_runsheet_carries_prompts_expectations_and_checks(tp: ModuleType) -> None:
    entry = _entry(tp)
    entry["execution"] = ["The sends carried no waitMs."]
    sheet = tp.runsheet(entry, "sha256:abc")
    assert "## Execution checks (layer 4" in sheet
    assert "- The sends carried no waitMs." in sheet
    assert "## Outcome checks (layer 5" in sheet
    assert "catalog_revision sha256:abc" in sheet
    assert "> Ask {host_a} how {branch} is going." in sheet
    assert "Expect hub: selected daintree.hosts.ask; calls host.list, host.sendMessage" in sheet
    assert "2. Person: Pause messaging." in sheet
    assert "- One exchange in the Shell's record." in sheet


def test_the_cli_check_fails_on_a_broken_register(tp: ModuleType, tmp_path: Path) -> None:
    broken = tmp_path / "register.yaml"
    broken.write_text("trials:\n  - id: x\n    runbook: daintree.hosts.foundation\n")
    assert tp.main(["check", "--register", str(broken)]) == 1
    assert tp.main(["layout", "r1"]) == 0


def test_the_pane_holder_runs_agents_on_the_users_sign_ins() -> None:
    # The app's e2e launch helper isolates HOME (and CLAUDE_CONFIG_DIR, CODEX_HOME)
    # unless told not to, which would sign every agent CLI out of a trial.
    holder = (_KIT / "harness" / "hold.spec.ts").read_text(encoding="utf-8")
    assert re.search(r"launchApp\(\{[^}]*isolateHome:\s*false", holder)


def test_the_driver_routes_plan_and_parses() -> None:
    driver = _KIT / "dtrb.sh"
    assert re.search(r"^\s*plan\) shift; cmd_plan \"\$@\" ;;$", driver.read_text(), re.M)
    assert subprocess.run(["bash", "-n", str(driver)], check=False).returncode == 0
