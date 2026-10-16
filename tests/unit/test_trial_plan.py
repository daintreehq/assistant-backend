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
import yaml

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


# --------------------------------------------------------------------------- scenarios
#
# The failure and recovery scenarios (`references/failure-scenarios.yaml`, issue #143).
# What `check` proves for them is the contract layer only: the runbooks exist, each
# role is offered what a step expects of it, and each injected condition is a value
# the app's captured contract declares. None of it is live evidence.

OUTPUTS: dict[str, dict[str, Any]] = {
    "hub-full": {
        "host.sendMessage": {
            "type": "object",
            "properties": {
                "outcome": {"type": "string", "enum": ["ok", "ambiguous", "rate_limited"]},
                "task": {
                    "anyOf": [
                        {
                            "type": "object",
                            "properties": {"state": {"enum": ["working", "completed"]}},
                        },
                        {"type": "null"},
                    ]
                },
            },
        },
        "host.list": {
            "properties": {
                "hosts": {
                    "type": "array",
                    "items": {
                        "properties": {"connection": {"enum": ["connected", "disconnected"]}}
                    },
                }
            }
        },
    },
    "lane-full": {"assistant.reply": {"properties": {"state": {"enum": ["completed"]}}}},
    "pane-full": {},
}
OBSERVATIONS = {"lost-reply": "delivery", "reuse": "startup"}
INVARIANTS = ("no-duplicate-write", "no-false-claim")

#: The faults issue #143 names, as observation tags: each needs a scenario.
ISSUE_143_FAULTS = (
    "lost-send-reply",
    "lost-start-reply",
    "duplicate-wake",
    "delayed-reply",
    "input-required",
    "rate-limit",
    "oversized-reply",
    "stale-generation",
    "consent-withdrawn",
    "slots-full",
    "wrong-cli",
    "confirmation-timeout",
    "hub-replacement",
    "link-loss",
    "host-restart",
    "lane-loss",
    "multiple-projects",
    "second-shell",
)
#: The evidence each scenario's outcome checks must show held (issue #143).
ISSUE_143_INVARIANTS = {
    "no-duplicate-write",
    "no-displaced-lane",
    "no-false-claim",
    "no-unrelated-resource",
}


def _scenario(tp: ModuleType) -> dict[str, Any]:
    run = {k: v for k, v in _entry(tp).items() if k not in ("id", "issue", "status")}
    return {
        "id": "lost-reply",
        "family": "delivery",
        "observes": ["lost-reply"],
        "invariants": ["no-duplicate-write"],
        "issues": [1],
        "inject": "Cut the link while the send is in flight.",
        "conditions": [{"role": "hub", "action": "host.sendMessage", "outcome": "ambiguous"}],
        "grounding": ["src/send.ts:10 a send cut off mid-call is ambiguous"],
        "staged": "owed",
        "real_host": "owed",
        **run,
    }


def _scenario_file(tp: ModuleType) -> tuple[dict[str, Any], dict[str, Any]]:
    data = {
        "app_sha": "abc",
        "families": {
            "delivery": {"title": "Delivery", "observations": ["lost-reply"], "issues": [1]},
            "startup": {"title": "Startup", "observations": ["reuse"], "issues": [2]},
        },
        "invariants": dict.fromkeys(INVARIANTS, "shown by an outcome check"),
        "minimum_staged": ["lost-reply"],
        "scenarios": [_scenario(tp)],
    }
    return data, {"app_sha": "abc", "trials": [_entry(tp)]}


def _scenario_problems(tp: ModuleType, sc: dict[str, Any]) -> list[str]:
    entries = {"ask": _entry(tp)}
    result: list[str] = tp.scenario_problems(
        sc, KNOWN, SURFACES, OUTPUTS, entries, OBSERVATIONS, INVARIANTS
    )
    return result


def test_a_well_formed_scenario_has_no_problems(tp: ModuleType) -> None:
    assert _scenario_problems(tp, _scenario(tp)) == []
    data, register = _scenario_file(tp)
    assert tp.scenarios_problems(data, register, KNOWN, SURFACES, OUTPUTS) == []


def test_a_scenario_may_run_a_register_entry_instead_of_its_own(tp: ModuleType) -> None:
    sc = {k: v for k, v in _scenario(tp).items() if k in tp.SCENARIO_KEYS}
    sc["run"] = "ask"
    assert _scenario_problems(tp, sc) == []
    sc["run"] = "gone"
    assert any("not a register entry" in p for p in _scenario_problems(tp, sc))
    sc["run"] = "ask"
    sc["steps"] = []
    assert any("and its own ['steps']" in p for p in _scenario_problems(tp, sc))


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (_set("inject", _DELETE), "missing ['inject']"),
        (_set("steps", _DELETE), "no run of its own: missing ['steps']"),
        (_set("colour", "red"), "unknown keys ['colour']"),
        (_set("id", "Lost Reply"), "lowercase slug"),
        (_set("family", "weather"), "family must be one of"),
        (_set("observes", []), "observes nothing"),
        (_set("observes", ["sunshine"]), "observes unknown"),
        (_set("observes", ["reuse"]), "nothing of its own family"),
        (_set("invariants", []), "names no invariant"),
        (_set("invariants", ["no-rain"]), "unknown invariants"),
        (_set("issues", ["#1"]), "issue numbers"),
        (_set("inject", " "), "inject must say"),
        (_set("grounding", []), "at least one citation"),
        (_set("grounding", ["somewhere in the app"]), "not 'path:line what'"),
        (_set("app_tests", ["a test somewhere"]), "not 'path:line what'"),
        (_set("staged", "pending"), "staged must be"),
        (_set("real_host", "n/a"), "real_host must be"),
        (_set("staged", "n/a: one Mac"), "never n/a"),
        (_set("path", "fleet"), "a fleet run cannot be staged"),
        (_set("conditions", "ambiguous"), "conditions must list"),
        # The run itself is held to the register's rules.
        (_set("combos", [{"hub": "claude", "lane": "codex"}]), "must run both"),
        (_set("steps.0.expect.lane.calls", ["host.sendMessage"]), "not offered on lane-full"),
    ],
)
def test_each_scenario_rule_rejects_the_scenario_that_breaks_it(
    tp: ModuleType, mutate: Any, expected: str
) -> None:
    sc = copy.deepcopy(_scenario(tp))
    mutate(sc)
    problems = _scenario_problems(tp, sc)
    assert any(expected in p for p in problems), problems


def test_a_scenario_nothing_would_ever_run_is_refused(tp: ModuleType) -> None:
    sc = _scenario(tp)
    sc.update(path="fleet", staged="n/a: one Mac", real_host="n/a: settled offline")
    assert any("both n/a" in p for p in _scenario_problems(tp, sc))


@pytest.mark.parametrize(
    ("cond", "expected"),
    [
        (
            {"role": "hub", "action": "host.sendMessage", "outcome": "lost"},
            "not among the captured",
        ),
        ({"role": "hub", "action": "host.sendMessage", "state": "exploded"}, "not among"),
        ({"role": "hub", "action": "host.teleport", "outcome": "ok"}, "not offered on hub-full"),
        ({"role": "pane", "action": "terminal.list", "state": "idle"}, "this run has only"),
        ({"role": "hub", "action": "host.sendMessage"}, "exactly one result field"),
        (
            {"role": "hub", "action": "host.sendMessage", "outcome": "ok", "state": "working"},
            "exactly one result field",
        ),
        ({"role": "hub", "action": "host.sendMessage", "detail": "x"}, "no enumerated 'detail'"),
        ({"error": "SOMETHING_ODD"}, "not an app error code"),
        ({"error": ["TOO_LARGE"]}, "plain strings"),
        ({"role": "hub", "action": "host.sendMessage", "outcome": {"x": 1}}, "plain strings"),
        ("ambiguous", "must be a mapping"),
    ],
)
def test_a_condition_must_be_one_the_captured_contract_declares(
    tp: ModuleType, cond: Any, expected: str
) -> None:
    problems = tp.condition_problems(cond, {"hub", "lane"}, OUTPUTS)
    assert any(expected in p for p in problems), problems


def test_conditions_reach_nested_enums_and_cited_error_codes(tp: ModuleType) -> None:
    for cond in (
        {"role": "hub", "action": "host.sendMessage", "state": "working"},
        {"role": "hub", "action": "host.list", "connection": "disconnected"},
        {"role": "lane", "action": "assistant.reply", "state": "completed"},
        {"error": "CONFIRMATION_TIMEOUT"},
        {"error": "TOO_LARGE"},
    ):
        assert tp.condition_problems(cond, {"hub", "lane"}, OUTPUTS) == [], cond
    # A fault in the world (a listener, a branch) declares no app value.
    sc = _scenario(tp)
    sc["conditions"] = []
    assert _scenario_problems(tp, sc) == []


def test_the_captured_contract_declares_the_faults_the_scenarios_inject(tp: ModuleType) -> None:
    outputs = tp.load_outputs()
    send = outputs["hub-full"]["host.sendMessage"]
    assert {"ambiguous", "rate_limited", "too_large", "lane_unavailable"} <= tp.output_values(
        send, "outcome"
    )
    assert "input-required" in tp.output_values(outputs["hub-full"]["host.getTask"], "state")
    assert "disconnected" in tp.output_values(outputs["hub-full"]["host.list"], "connection")
    assert tp.output_values(outputs["lane-full"]["assistant.reply"], "state") == {
        "input-required",
        "completed",
    }


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d.update(app_sha="def"), "app_sha differs"),
        (lambda d: d.update(families={}), "families must map"),
        (
            lambda d: d["families"]["startup"].update(observations=["lost-reply"]),
            "in both delivery and startup",
        ),
        (lambda d: d["families"]["startup"].pop("issues"), "needs exactly title"),
        (lambda d: d["families"]["startup"].update(issues=143), "list of issue numbers"),
        (lambda d: d["families"]["startup"].update(issues=[]), "list of issue numbers"),
        (lambda d: d["families"]["startup"].update(observations=[]), "list of slugs"),
        (lambda d: d["families"]["startup"].update(observations="reuse"), "list of slugs"),
        (lambda d: d["scenarios"][0].update(family=["delivery"]), "family must be one of"),
        (lambda d: d.update(invariants={}), "invariants must map"),
        (lambda d: d["scenarios"].append(copy.deepcopy(d["scenarios"][0])), "id used twice"),
        (lambda d: d["scenarios"][0].update(id="ask"), "also a register entry's"),
        (lambda d: d.update(minimum_staged=[]), "minimum_staged must list"),
        (lambda d: d.update(minimum_staged=["nope"]), "no scenario nope"),
        (lambda d: d.update(minimum_staged=["lost-reply"] * 7), "at most 6"),
        (
            lambda d: d["scenarios"][0].update(
                path="fleet", staged="n/a: one Mac", needs=["real-ssh"]
            ),
            "is not a staged run",
        ),
        (
            lambda d: d["scenarios"][0].update(
                combos=[{"hub": "claude", "lane": "claude"}, {"hub": "codex", "lane": "codex"}]
            ),
            "a mixed-CLI pair",
        ),
        (
            lambda d: d["scenarios"][0].update(
                combos=[{"hub": "claude", "lane": "codex"}, {"hub": "codex", "lane": "codex"}]
            ),
            "both CLIs as hub and both as lane",
        ),
    ],
)
def test_the_scenario_file_rules(tp: ModuleType, mutate: Any, expected: str) -> None:
    data, register = _scenario_file(tp)
    mutate(data)
    problems = tp.scenarios_problems(data, register, KNOWN, SURFACES, OUTPUTS)
    assert any(expected in p for p in problems), problems


def test_gaps_name_unobserved_observations_and_unmapped_issues(tp: ModuleType) -> None:
    data, _register = _scenario_file(tp)
    assert tp.scenario_gaps(data) == [
        "no scenario observes startup: reuse",
        "no scenario maps issue #2 (startup)",
    ]
    # Malformed families are a gap, never a vacuous pass or a crash.
    for bad in ({"issues": 143}, {"issues": []}, {"observations": []}, {"observations": [["x"]]}):
        broken = copy.deepcopy(data)
        broken["families"]["startup"].update(bad)
        assert "family startup is malformed (plan check)" in tp.scenario_gaps(broken), bad
    data["scenarios"][0]["observes"] = [["lost-reply"]]
    assert "no scenario observes delivery: lost-reply" in tp.scenario_gaps(data)
    data["families"] = None
    assert tp.scenario_gaps(data) == ["no families to cover"]


def test_scenarios_are_selected_by_issue_runbook_family_and_minimum(tp: ModuleType) -> None:
    data, register = _scenario_file(tp)
    by_ref = {k: v for k, v in _scenario(tp).items() if k in tp.SCENARIO_KEYS}
    by_ref.update(id="reuse-lane", family="startup", observes=["reuse"], issues=[2], run="ask")
    data["scenarios"].append(by_ref)

    def ids(**kw: Any) -> list[str]:
        return [s["id"] for s in tp.select_scenarios(data, register, **kw)]

    assert ids(issue=2) == ["reuse-lane"]
    assert ids(runbook="daintree.hosts.ask") == ["lost-reply", "reuse-lane"]
    assert ids(runbook="daintree.hosts.work") == []
    assert ids(family="delivery") == ["lost-reply"]
    assert ids(minimum=True) == ["lost-reply"]
    table = tp.scenario_table(tp.select_scenarios(data, register), register)
    assert "2 scenarios; staged owed 2 (4 trials), real host owed 2" in table
    assert "staged: owed | real host: owed | app tests: none" in table


def test_a_scenario_sheet_keeps_contract_staged_and_real_host_evidence_apart(
    tp: ModuleType,
) -> None:
    _data, register = _scenario_file(tp)
    sc = _scenario(tp)
    sc["app_tests"] = ["src/send.test.ts:4 reports a lost answer as ambiguous"]
    sheet = tp.scenario_sheet(sc, register, "sha256:abc")
    assert "Evidence: contract checked by plan check; staged owed; real host owed" in sheet
    assert "- role hub, action host.sendMessage, outcome ambiguous" in sheet
    assert "App harness (the app's own behaviour, never runbook following):" in sheet
    assert "## Outcome checks (layer 5" in sheet
    assert "# lost-reply: daintree.hosts.ask (a scenario's own run)" in sheet
    sc["conditions"] = []
    assert "none: the fault is in the world" in tp.scenario_sheet(sc, register)


def test_grounding_reads_every_citation_at_the_pinned_revision(tp: ModuleType) -> None:
    data, _register = _scenario_file(tp)
    sc = data["scenarios"][0]
    sc["app_tests"] = ["src/send.test.ts:2 reports a lost answer as ambiguous"]
    files = {
        "src/send.ts": "\n".join(f"line {n}" for n in range(1, 21)),
        "src/send.test.ts": 'import x\nit("reports a lost answer as ambiguous", () => {})\n',
    }
    assert tp.grounding_problems(data, files.get) == []
    sc["grounding"] = ["src/send.ts:19-25 past the end", "src/gone.ts:1 not there"]
    sc["app_tests"] = ["src/send.test.ts:1 reports a lost answer as ambiguous"]
    problems = tp.grounding_problems(data, files.get)
    assert any("has 20 lines, not 19-25" in p for p in problems), problems
    assert any("src/gone.ts is not in the app" in p for p in problems), problems
    assert any("is not the test" in p for p in problems), problems


def test_the_committed_scenarios_hold_to_their_rules(tp: ModuleType) -> None:
    known, _revision = tp.load_catalog_ids()
    data, register = tp.load_scenarios(), tp.load_register()
    problems = tp.scenarios_problems(data, register, known, tp.load_surfaces(), tp.load_outputs())
    assert problems == []
    assert tp.scenario_gaps(data) == []


def test_the_committed_scenarios_cover_what_issue_143_names(tp: ModuleType) -> None:
    data, register = tp.load_scenarios(), tp.load_register()
    observed = {o for sc in data["scenarios"] for o in sc["observes"]}
    assert sorted(set(ISSUE_143_FAULTS) - observed) == []
    # The issue references its scenarios by id: listing it finds every named fault.
    own = tp.select_scenarios(data, register, issue=143)
    assert sorted(set(ISSUE_143_FAULTS) - {o for sc in own for o in sc["observes"]}) == []
    assert set(data["invariants"]) == ISSUE_143_INVARIANTS
    # Every staged scenario owes a staged run and every fleet one a real-host run, and
    # the minimum subset stays within twelve trials.
    for sc in data["scenarios"]:
        run = tp.scenario_run(sc, register)
        tier = "staged" if run["path"] == "staged" else "real_host"
        assert sc[tier] == "owed", (sc["id"], tier)
    minimum = tp.select_scenarios(data, register, minimum=True)
    assert sum(len(tp.scenario_run(s, register)["combos"]) for s in minimum) <= 12


def test_the_cli_lists_scenarios_and_fails_a_broken_file(tp: ModuleType, tmp_path: Path) -> None:
    assert tp.main(["scenarios", "--issue", "143"]) == 0
    assert tp.main(["show", "lost-send-reply"]) == 0
    broken = tmp_path / "scenarios.yaml"
    broken.write_text("app_sha: x\nfamilies: {}\nscenarios:\n  - id: y\n")
    assert tp.main(["check", "--scenarios", str(broken)]) == 1


def test_a_scenario_naming_a_missing_run_fails_cleanly(tp: ModuleType, tmp_path: Path) -> None:
    data, register = _scenario_file(tp)
    sc = {k: v for k, v in _scenario(tp).items() if k in tp.SCENARIO_KEYS}
    sc["run"] = "gone"
    with pytest.raises(ValueError, match="'gone' is not a register entry"):
        tp.scenario_run(sc, register)
    broken = tmp_path / "scenarios.yaml"
    data["scenarios"] = [sc]
    broken.write_text(yaml.safe_dump(data))
    assert tp.main(["scenarios", "--scenarios", str(broken)]) == 1
    assert tp.main(["show", sc["id"], "--scenarios", str(broken)]) == 1
