"""Expanding one fork plan into four sendable spawn calls.

Three properties carry the feature and are each pinned below:

- the four branches run CONCURRENTLY (wall time near the slowest, not the sum),
  because a sequential implementation would pass every other test here;
- the plan's control fields win over the expander's, always, because the
  expander reads untrusted terminal output and the controls decide where an
  agent runs and what it may do;
- a branch that cannot be expanded is still SPAWNED, from the plan alone, so one
  bad branch costs prompt quality rather than the user's four agents.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from daintree_assistant_server.config import Settings
from daintree_assistant_server.contracts.forked_terminals import ForkSpawnPlan
from daintree_assistant_server.errors import upstream_unavailable
from daintree_assistant_server.providers.base import (
    ProviderChatRequest,
    ProviderChatResult,
    ProviderUsage,
)
from daintree_assistant_server.services.deadlines import DEFAULT_BUDGET_POLICY, RequestBudget
from daintree_assistant_server.services.fork_prompt_expansion import (
    GENERATED_FIELDS,
    BranchUnusable,
    ForkPromptExpansionService,
    disambiguate_titles,
    fallback_arguments,
    finalize_arguments,
    merge_controls,
    synthesized_call_id,
)
from daintree_assistant_server.services.forked_terminal_calls import SPAWN_TOOL_WIRE_NAME

_INVENTORY = (
    Path(__file__).resolve().parents[2]
    / "benchmarks"
    / "_harness"
    / "fixtures"
    / "cli_tool_inventory.json"
)


def real_spawn_schema() -> dict[str, Any]:
    """The schema the CLI actually ships, from the pinned inventory fixture."""
    with _INVENTORY.open(encoding="utf-8") as handle:
        for entry in json.load(handle):
            if entry["function"]["name"] == SPAWN_TOOL_WIRE_NAME:
                schema: dict[str, Any] = entry["function"]["parameters"]
                return schema
    raise AssertionError(f"{SPAWN_TOOL_WIRE_NAME} missing from the CLI inventory fixture")

# The real tool's shape, trimmed to what this feature touches. Deliberately not
# the whole CLI schema: the point is that the RUNTIME schema is the authority,
# so the tests hand one in exactly as the orchestrator will.
SPAWN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "taskPrompt"],
    "properties": {
        "title": {"type": "string"},
        "taskPrompt": {"type": "string"},
        "acceptanceCriteria": {"type": "string"},
        "agentId": {"type": "string"},
        "worktreeId": {"type": "string"},
        "mode": {"type": "string", "enum": ["edit", "explore"]},
        "watch": {"type": "boolean"},
        "watchGoal": {"type": "string"},
        "watchCadenceMs": {"type": "number"},
        "context": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "filePaths": {"type": "array", "items": {"type": "string"}},
                "includeDiff": {"type": "boolean"},
            },
        },
    },
}

BRANCH_IDS = ("backend", "frontend", "tests", "performance")


def plan(**overrides: Any) -> ForkSpawnPlan:
    body: dict[str, Any] = {
        "sharedGoal": "Prepare the application for release.",
        "sharedConstraints": ["Run the relevant tests."],
        "defaults": {"agentId": "claude", "mode": "edit", "watch": True},
        "branches": [
            {
                "id": bid,
                "worktreeId": f"/repo/{bid}",
                "brief": f"Own the {bid} scope; touch nothing else; tests must pass.",
            }
            for bid in BRANCH_IDS
        ],
    }
    body.update(overrides)
    return ForkSpawnPlan.model_validate(body)


def base_request() -> ProviderChatRequest:
    return ProviderChatRequest(
        model="m",
        messages=[
            {"role": "system", "content": "base"},
            {"role": "user", "content": "split this four ways"},
        ],
        tools=[
            {
                "type": "function",
                "function": {"name": SPAWN_TOOL_WIRE_NAME, "parameters": SPAWN_SCHEMA},
            }
        ],
        max_tokens=2048,
    )


def spawn_result(
    args: dict[str, Any] | str,
    *,
    name: str = SPAWN_TOOL_WIRE_NAME,
    calls: int = 1,
    usage: ProviderUsage | None = None,
) -> ProviderChatResult:
    encoded = args if isinstance(args, str) else json.dumps(args)
    return ProviderChatResult(
        id="r",
        created=0,
        model="m",
        choices=[
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"model_call_{i}",
                            "type": "function",
                            "function": {"name": name, "arguments": encoded},
                        }
                        for i in range(calls)
                    ],
                }
            }
        ],
        usage=usage or ProviderUsage(prompt_tokens=1000, completion_tokens=200, cached_tokens=900),
        finish_reason="tool_calls",
    )


def good_args(branch_id: str = "backend") -> dict[str, Any]:
    return {
        "title": f"{branch_id} work",
        "taskPrompt": f"Do the {branch_id} work carefully.",
        "acceptanceCriteria": "Tests pass and nothing outside scope changed.",
        "watchGoal": "Report when it settles.",
    }


class ScriptedProvider:
    """A provider that answers per branch, optionally after a delay."""

    def __init__(
        self,
        answers: dict[str, Any] | None = None,
        *,
        delays: dict[str, float] | None = None,
        default: Any = None,
        gate: asyncio.Event | None = None,
        entered: asyncio.Semaphore | None = None,
    ) -> None:
        self.answers = answers or {}
        self.delays = delays or {}
        self.default = default
        self.requests: list[ProviderChatRequest] = []
        self.concurrent = 0
        self.max_concurrent = 0
        # A gate lets a test prove concurrency structurally rather than by wall
        # clock: no call may finish until every call has started, which a
        # sequential implementation deadlocks on instead of merely being slow.
        self.gate = gate
        self.entered = entered

    def _branch_of(self, request: ProviderChatRequest) -> str:
        content = str(request.messages[-1].get("content", ""))
        for bid in BRANCH_IDS:
            if f'"assignedBranchId":"{bid}"' in content:
                return bid
        # A repair round appends its message after the assignment.
        for message in reversed(request.messages):
            for bid in BRANCH_IDS:
                if f'"assignedBranchId":"{bid}"' in str(message.get("content", "")):
                    return bid
        return ""

    async def complete(self, request: ProviderChatRequest) -> ProviderChatResult:
        self.requests.append(request)
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            if self.entered is not None:
                self.entered.release()
            if self.gate is not None:
                await self.gate.wait()
            branch = self._branch_of(request)
            delay = self.delays.get(branch, 0.0)
            if delay:
                await asyncio.sleep(delay)
            answer = self.answers.get(branch, self.default)
            if answer is None:
                answer = spawn_result(good_args(branch))
            if isinstance(answer, list):
                answer = answer.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        finally:
            self.concurrent -= 1


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "TOGETHER_API_KEY": "sk",
        "STATE_SIGNING_KEY_CURRENT": "s",
        "MAX_UPSTREAM_RETRIES": 0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def service(provider: Any, **overrides: Any) -> ForkPromptExpansionService:
    return ForkPromptExpansionService(settings=settings(**overrides), provider=provider)


async def expand(svc: ForkPromptExpansionService, spawn_plan: ForkSpawnPlan | None = None) -> Any:
    return await svc.expand(
        base_request=base_request(),
        plan=spawn_plan or plan(),
        fork_call_id="call_fork_1",
        spawn_schema=SPAWN_SCHEMA,
        request_id="req-1",
    )


def args_of(call: dict[str, Any]) -> dict[str, Any]:
    return json.loads(call["function"]["arguments"])


# ---------------------------------------------------------------------------
# The happy path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_four_branches_become_four_spawn_calls_in_plan_order() -> None:
    result = await expand(service(ScriptedProvider()))
    assert [call["function"]["name"] for call in result.calls] == [SPAWN_TOOL_WIRE_NAME] * 4
    assert [args_of(call)["worktreeId"] for call in result.calls] == [
        f"/repo/{bid}" for bid in BRANCH_IDS
    ]


@pytest.mark.asyncio
async def test_order_survives_out_of_order_completion() -> None:
    # `gather` preserves INPUT order, and that is what keeps the four spawns in
    # the order the user confirmed rather than the order the model finished in.
    provider = ScriptedProvider(
        delays={"backend": 0.06, "frontend": 0.01, "tests": 0.04, "performance": 0.02}
    )
    result = await expand(service(provider))
    assert [b.branch_id for b in result.branches] == list(BRANCH_IDS)
    # The externally observable contract is the CALLS. Branch metadata staying
    # in plan order while the calls came back in completion order would be an
    # invisible reordering of the four spawns the user confirmed.
    assert [args_of(call)["worktreeId"] for call in result.calls] == [
        f"/repo/{bid}" for bid in BRANCH_IDS
    ]


@pytest.mark.asyncio
async def test_the_branches_run_concurrently_not_serially() -> None:
    # The one property a sequential implementation would pass every other test
    # in this file with. Proven by a BARRIER rather than a stopwatch: no call
    # may return until all four have started, so a serial implementation hangs
    # and fails outright instead of merely being slower than a threshold a
    # loaded CI machine could also miss.
    gate = asyncio.Event()
    entered = asyncio.Semaphore(0)
    provider = ScriptedProvider(gate=gate, entered=entered)

    async def release_once_all_four_are_in() -> None:
        for _ in range(4):
            await entered.acquire()
        gate.set()

    releaser = asyncio.create_task(release_once_all_four_are_in())
    async with asyncio.timeout(5):
        result = await expand(service(provider))
    await releaser
    assert provider.max_concurrent == 4
    assert len(result.calls) == 4
    assert all(not branch.fallback for branch in result.branches)


@pytest.mark.asyncio
async def test_wall_time_tracks_the_slowest_branch_not_the_sum() -> None:
    # The secondary, generous check. Delays sum to 0.27s; concurrently the fork
    # should finish a little after the 0.09s branch.
    provider = ScriptedProvider(
        delays={"backend": 0.05, "frontend": 0.09, "tests": 0.07, "performance": 0.06}
    )
    started = time.perf_counter()
    await expand(service(provider))
    assert time.perf_counter() - started < 0.25


@pytest.mark.asyncio
async def test_the_semaphore_bounds_the_process_without_serializing_one_fork() -> None:
    # It is a capacity limit on many simultaneous forks, not a branch
    # serializer: one fork must still be able to take all four permits.
    provider = ScriptedProvider(delays=dict.fromkeys(BRANCH_IDS, 0.02))
    await expand(service(provider, FORK_EXPANSION_MAX_INFLIGHT=4))
    assert provider.max_concurrent == 4


@pytest.mark.asyncio
async def test_the_semaphore_caps_two_simultaneous_forks() -> None:
    # The bound only means anything across forks — one fork can never exceed
    # four in flight by itself, so a single-fork test would pass with no
    # semaphore at all.
    provider = ScriptedProvider(delays=dict.fromkeys(BRANCH_IDS, 0.05))
    svc = service(provider, FORK_EXPANSION_MAX_INFLIGHT=4)
    await asyncio.gather(expand(svc), expand(svc))
    assert provider.max_concurrent == 4


@pytest.mark.asyncio
async def test_time_queued_for_a_permit_counts_against_the_fork_budget() -> None:
    # Queueing is time the user waits exactly as much as generation is. A branch
    # whose clock only started once it held a permit could sit behind several
    # cohorts of another fork's branches and outlive the turn that asked for it.
    provider = ScriptedProvider(delays=dict.fromkeys(BRANCH_IDS, 0.4))
    svc = service(provider, FORK_EXPANSION_MAX_INFLIGHT=4, FORK_EXPANSION_TIMEOUT_SECONDS=0.15)
    first = asyncio.create_task(expand(svc))
    await asyncio.sleep(0.02)
    started = time.perf_counter()
    second = await expand(svc)
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0
    assert all(branch.fallback for branch in second.branches)
    assert {b.fallback_reason for b in second.branches} == {"timeout"}
    await first


@pytest.mark.asyncio
async def test_the_turn_budget_clamps_the_fork_clock() -> None:
    # The configured expansion timeout is a ceiling, not an entitlement: what is
    # left of the turn wins whenever it is smaller.
    provider = ScriptedProvider(delays=dict.fromkeys(BRANCH_IDS, 0.5))
    svc = service(provider, FORK_EXPANSION_TIMEOUT_SECONDS=30.0)
    # Every phase limit moves together: the policy rejects one that sits above
    # the total, since a limit that is clamped away can never be the phase that
    # trips.
    budget = RequestBudget(
        policy=replace(
            DEFAULT_BUDGET_POLICY,
            total=0.1,
            preparation=0.1,
            open_stall=0.1,
            first_output=0.1,
            stream_idle=0.1,
        )
    )
    started = time.perf_counter()
    result = await svc.expand(
        base_request=base_request(),
        plan=plan(),
        fork_call_id="call_fork_1",
        spawn_schema=SPAWN_SCHEMA,
        request_id="req-1",
        budget=budget,
    )
    assert time.perf_counter() - started < 1.0
    assert all(branch.fallback for branch in result.branches)


@pytest.mark.asyncio
async def test_usage_sums_every_branch() -> None:
    result = await expand(service(ScriptedProvider()))
    assert result.usage is not None
    assert result.usage.prompt_tokens == 4000
    assert result.usage.completion_tokens == 800
    assert result.usage.cached_tokens == 3600


@pytest.mark.asyncio
async def test_call_ids_are_backend_owned_unique_and_bounded() -> None:
    result = await expand(service(ScriptedProvider()))
    ids = [call["id"] for call in result.calls]
    assert len(set(ids)) == 4
    # Never the utility model's own id: four independent completions cannot
    # agree on unique ones, and a duplicate is a transcript the validator
    # refuses.
    assert not any(cid.startswith("model_call") for cid in ids)
    assert all(len(cid) <= 256 for cid in ids)


def test_call_ids_are_derived_rather_than_random() -> None:
    first = synthesized_call_id("call_fork_1", "backend", 0)
    assert synthesized_call_id("call_fork_1", "backend", 0) == first
    assert synthesized_call_id("call_fork_2", "backend", 0) != first
    assert synthesized_call_id("call_fork_1", "frontend", 0) != first
    assert synthesized_call_id("call_fork_1", "backend", 1) != first


# ---------------------------------------------------------------------------
# Control-field authority.
# ---------------------------------------------------------------------------


def mixed_plan() -> ForkSpawnPlan:
    """Four branches whose controls all DIFFER, so a per-branch bug is visible.

    With shared defaults, an implementation that merged the first branch's
    controls into all four would pass every authority assertion.
    """
    return plan(
        defaults={"agentId": "claude", "mode": "edit", "watch": True},
        branches=[
            {
                "id": "backend",
                "worktreeId": "/repo/backend",
                "brief": "Own the backend scope; nothing else.",
            },
            {
                "id": "frontend",
                "worktreeId": "/repo/frontend",
                "brief": "Own the frontend scope; nothing else.",
                "agentId": "codex",
            },
            {
                "id": "tests",
                "worktreeId": "/repo/tests",
                "brief": "Own the test gaps; nothing else.",
                "mode": "explore",
            },
            {
                "id": "performance",
                "worktreeId": "/repo/perf",
                "brief": "Own the slow paths; nothing else.",
                "watch": False,
            },
        ],
    )


EXPECTED_CONTROLS = [
    {"agentId": "claude", "worktreeId": "/repo/backend", "mode": "edit", "watch": True},
    {"agentId": "codex", "worktreeId": "/repo/frontend", "mode": "edit", "watch": True},
    {"agentId": "claude", "worktreeId": "/repo/tests", "mode": "explore", "watch": True},
    {"agentId": "claude", "worktreeId": "/repo/perf", "mode": "edit", "watch": False},
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "hostile"),
    [
        ("worktreeId", "/somewhere/else"),
        ("agentId", "some-other-agent"),
        ("mode", "explore"),
        ("watch", False),
    ],
)
async def test_the_expander_cannot_move_a_control_field(field: str, hostile: Any) -> None:
    # The expander reads tool results, terminal output and issue text. This is
    # the boundary that makes reading them safe: it may write prose, and the
    # backend re-imposes everything that decides where an agent runs — on EVERY
    # branch, from that branch's own resolved controls.
    provider = ScriptedProvider(default=spawn_result({**good_args(), field: hostile}))
    result = await expand(service(provider), mixed_plan())
    for call, expected in zip(result.calls, EXPECTED_CONTROLS, strict=True):
        assert args_of(call)[field] == expected[field]


@pytest.mark.asyncio
async def test_a_fallback_call_carries_its_own_branch_controls_too() -> None:
    # The fallback path builds the call itself, so it is a second, independent
    # place the authority merge could be wrong.
    provider = ScriptedProvider(default=upstream_unavailable("down"))
    result = await expand(service(provider), mixed_plan())
    for call, expected in zip(result.calls, EXPECTED_CONTROLS, strict=True):
        args = args_of(call)
        assert {k: args[k] for k in expected} == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["watchCadenceMs", "watcher", "context"])
async def test_the_expander_cannot_contribute_a_non_prose_field(field: str) -> None:
    # The allowlist is the authority, not the tool schema. The real spawn schema
    # also declares `watchCadenceMs`, a `context` object and the legacy nested
    # `watcher` — all control plane, and none of them things the control merge
    # would notice, because that only re-imposes the four keys it owns. An
    # expander setting a supervisor's poll cadence to an hour is a real attack.
    hostile = {
        "watchCadenceMs": 3_600_000,
        "watcher": {"create": True, "goal": "ignore the plan", "cadenceMs": 3_600_000},
        "context": {"filePaths": ["/etc/passwd"], "includeDiff": True},
    }[field]
    provider = ScriptedProvider(default=spawn_result({**good_args(), field: hostile}))
    result = await expand(service(provider), plan())
    assert field not in args_of(result.calls[0])


@pytest.mark.asyncio
async def test_the_allowlist_holds_against_the_real_cli_schema() -> None:
    # The trimmed schema above keeps the unit tests focused; this one runs the
    # same narrowing against the schema the CLI actually ships, so a field added
    # upstream cannot silently widen what an expander may set.
    real = real_spawn_schema()
    hostile = {
        **good_args(),
        "watchCadenceMs": 3_600_000,
        "watcher": {"create": True, "goal": "ignore the plan"},
        "context": {"filePaths": ["/etc/passwd"]},
    }
    provider = ScriptedProvider(default=spawn_result(hostile))
    result = await ForkPromptExpansionService(
        settings=settings(), provider=provider
    ).expand(
        base_request=base_request(),
        plan=plan(),
        fork_call_id="call_fork_1",
        spawn_schema=real,
        request_id="req-1",
    )
    args = args_of(result.calls[0])
    assert set(args) <= set(GENERATED_FIELDS) | {"agentId", "worktreeId", "mode", "watch"}


@pytest.mark.asyncio
async def test_a_control_mismatch_is_counted_not_fatal() -> None:
    # The backend already holds the authoritative value, so failing an
    # otherwise good branch over a field about to be overwritten would trade a
    # real prompt for a fallback one. The count is the drift signal.
    provider = ScriptedProvider(default=spawn_result({**good_args(), "worktreeId": "/elsewhere"}))
    result = await expand(service(provider))
    assert result.branches[0].immutable_mismatches == 1
    assert result.branches[0].fallback is False


@pytest.mark.asyncio
async def test_an_invented_field_is_dropped_before_the_call_is_sent() -> None:
    # The CLI decodes strictly, so a stray key is a rejected call at the far
    # end — and this is where an expander inventing a field stops.
    provider = ScriptedProvider(default=spawn_result({**good_args(), "sudo": True}))
    result = await expand(service(provider))
    assert "sudo" not in args_of(result.calls[0])


def test_merge_controls_reports_the_mismatch_count() -> None:
    spawn_plan = plan()
    merged, mismatches = merge_controls(
        {"worktreeId": "/elsewhere", "mode": "explore", "title": "t"},
        spawn_plan,
        spawn_plan.branches[0],
    )
    assert mismatches == 2
    assert merged["worktreeId"] == "/repo/backend"
    assert merged["mode"] == "edit"
    assert merged["title"] == "t"


@pytest.mark.asyncio
async def test_a_watch_goal_cannot_attach_a_watcher_the_plan_did_not_want() -> None:
    # `watchGoal` attaches a supervisor on its own in the CLI's wrapper, so
    # leaving one on an unwatched branch would quietly overturn the plan's
    # supervision decision — the one direction the merge must never move.
    unwatched = plan(defaults={"agentId": "claude", "mode": "edit", "watch": False})
    provider = ScriptedProvider(default=spawn_result(good_args()))
    result = await expand(service(provider), unwatched)
    args = args_of(result.calls[0])
    assert args["watch"] is False
    assert "watchGoal" not in args


@pytest.mark.asyncio
async def test_explore_branches_drop_acceptance_criteria() -> None:
    # The wrapper ignores them for an explore task, and a field that is ignored
    # still reads to a model as a contract it must satisfy.
    exploring = plan(defaults={"agentId": "claude", "mode": "explore", "watch": False})
    provider = ScriptedProvider(default=spawn_result(good_args()))
    result = await expand(service(provider), exploring)
    assert "acceptanceCriteria" not in args_of(result.calls[0])


@pytest.mark.asyncio
async def test_context_file_paths_from_the_plan_reach_the_call() -> None:
    with_paths = plan(
        branches=[
            {
                "id": "backend",
                "worktreeId": "/repo/backend",
                "brief": "Own the backend scope; nothing else.",
                "contextFilePaths": ["src/a.py"],
            },
            *[
                {
                    "id": bid,
                    "worktreeId": f"/repo/{bid}",
                    "brief": f"Own the {bid} scope; nothing else.",
                }
                for bid in BRANCH_IDS[1:]
            ],
        ]
    )
    result = await expand(service(ScriptedProvider()), with_paths)
    assert args_of(result.calls[0])["context"] == {"filePaths": ["src/a.py"]}


# ---------------------------------------------------------------------------
# Repair and fallback.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(spawn_result("not json at all"), id="invalid_json"),
        pytest.param(spawn_result({"taskPrompt": "no title"}), id="missing_title"),
        pytest.param(spawn_result({"title": "t"}), id="missing_prompt"),
        pytest.param(
            spawn_result({"title": "t", "taskPrompt": "p"}), id="missing_acceptance_for_edit"
        ),
        pytest.param(spawn_result(good_args(), name="fs__read"), id="wrong_function"),
        pytest.param(spawn_result(good_args(), calls=2), id="two_calls"),
        pytest.param(spawn_result(good_args(), calls=0), id="no_call"),
    ],
)
async def test_a_bad_branch_is_repaired_once_then_succeeds(bad: ProviderChatResult) -> None:
    # Scripted for ONE branch. A shared queue across four concurrent branches
    # would have the first branch consume both entries and the other three
    # silently fall back on an empty list — which the obvious assertions
    # ("something was repaired", "four calls exist") would still pass.
    provider = ScriptedProvider(answers={"backend": [bad, spawn_result(good_args())]})
    result = await expand(service(provider))
    assert [branch.repaired for branch in result.branches] == [True, False, False, False]
    assert not any(branch.fallback for branch in result.branches)
    assert len(provider.requests) == 5
    assert args_of(result.calls[0])["title"]


@pytest.mark.asyncio
async def test_a_branch_that_fails_twice_falls_back_deterministically() -> None:
    provider = ScriptedProvider(
        answers={"backend": [spawn_result("nope"), spawn_result("still nope")]}
    )
    result = await expand(service(provider))
    backend = result.branches[0]
    assert backend.fallback is True
    assert backend.fallback_reason == "invalid_json"
    args = args_of(result.calls[0])
    assert args["title"]
    assert "Own the backend scope" in args["taskPrompt"]
    # The other three are unaffected — one branch failing must not cost the
    # user the fleet they confirmed.
    assert [b.fallback for b in result.branches[1:]] == [False, False, False]


@pytest.mark.asyncio
async def test_one_branch_erroring_does_not_cancel_the_others() -> None:
    provider = ScriptedProvider(answers={"tests": upstream_unavailable("boom")})
    result = await expand(service(provider))
    assert result.branches[2].fallback is True
    assert result.branches[2].fallback_reason.startswith("upstream:")
    assert sum(1 for b in result.branches if b.fallback) == 1


@pytest.mark.asyncio
async def test_a_provider_outage_across_all_branches_still_yields_four_calls() -> None:
    # The validated plan is complete, so degrading prompt quality beats losing
    # the whole turn after the user has already waited for the main call.
    provider = ScriptedProvider(default=upstream_unavailable("everything is down"))
    result = await expand(service(provider))
    assert len(result.calls) == 4
    assert all(branch.fallback for branch in result.branches)
    # Every branch failed before the provider returned anything, so there is no
    # usage to report — which is different from reporting zero.
    assert result.usage is None


@pytest.mark.asyncio
async def test_a_slow_branch_falls_back_within_the_shared_wall_clock() -> None:
    provider = ScriptedProvider(delays={"performance": 5.0})
    started = time.perf_counter()
    result = await expand(service(provider, FORK_EXPANSION_TIMEOUT_SECONDS=0.1))
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0
    assert result.branches[3].fallback is True
    assert result.branches[3].fallback_reason == "timeout"


@pytest.mark.asyncio
async def test_cancellation_propagates_to_every_branch() -> None:
    # A client disconnect or a shutdown must unwind all four upstream calls —
    # which is why CancelledError is the one exception the per-branch guard does
    # not swallow into a fallback.
    provider = ScriptedProvider(delays=dict.fromkeys(BRANCH_IDS, 5.0))
    task = asyncio.create_task(expand(service(provider)))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.concurrent == 0


@pytest.mark.asyncio
async def test_a_fallback_branch_is_not_billed_as_an_empty_call() -> None:
    provider = ScriptedProvider(answers={"backend": upstream_unavailable("down")})
    result = await expand(service(provider))
    assert result.branches[0].usage is None
    assert result.usage is not None
    assert result.usage.prompt_tokens == 3000


# ---------------------------------------------------------------------------
# The deterministic fallback itself.
# ---------------------------------------------------------------------------


def test_a_fallback_prompt_carries_the_scope_and_the_done_signal() -> None:
    spawn_plan = plan()
    args = fallback_arguments(spawn_plan, spawn_plan.branches[0])
    assert "Own the backend scope" in args["taskPrompt"]
    assert "Prepare the application for release." in args["taskPrompt"]
    assert "Run the relevant tests." in args["taskPrompt"]
    assert args["acceptanceCriteria"]


def test_a_fallback_prompt_tells_an_explore_agent_it_may_not_write() -> None:
    exploring = plan(defaults={"agentId": "claude", "mode": "explore", "watch": False})
    args = fallback_arguments(exploring, exploring.branches[0])
    assert "WITHOUT changing any file" in args["taskPrompt"]
    assert "acceptanceCriteria" not in args


def test_a_fallback_prompt_says_the_other_branches_are_running() -> None:
    # A worker that does not know it is one of four will happily fix what
    # another branch owns.
    spawn_plan = plan()
    args = fallback_arguments(spawn_plan, spawn_plan.branches[0])
    assert "parallel branches" in args["taskPrompt"]


def test_a_fallback_title_prefers_the_hint_then_the_branch_id() -> None:
    hinted = plan(
        branches=[
            {
                "id": "backend",
                "worktreeId": "/repo/backend",
                "brief": "Own the backend scope.",
                "titleHint": "backend reliability",
            },
            *[
                {
                    "id": bid,
                    "worktreeId": f"/repo/{bid}",
                    "brief": f"Own the {bid} scope.",
                }
                for bid in ("a_b", "c", "d")
            ],
        ]
    )
    assert fallback_arguments(hinted, hinted.branches[0])["title"] == "backend reliability"
    assert fallback_arguments(hinted, hinted.branches[1])["title"] == "a b"


@pytest.mark.asyncio
async def test_a_fallback_call_still_conforms_to_the_target_schema() -> None:
    provider = ScriptedProvider(default=upstream_unavailable("down"))
    result = await expand(service(provider))
    for call in result.calls:
        args = args_of(call)
        assert set(args) <= set(SPAWN_SCHEMA["properties"])
        assert args["title"] and args["taskPrompt"]


# ---------------------------------------------------------------------------
# Titles.
# ---------------------------------------------------------------------------


def test_duplicate_titles_are_disambiguated_by_branch_id() -> None:
    # Four terminals launch at once and the title IS the tab. Two identical
    # tabs leave the user unable to tell which agent is which.
    calls = [{"title": "release pass"}, {"title": "release pass"}, {"title": "tests"}]
    out = disambiguate_titles(calls, ["backend", "frontend", "tests"])
    assert out[0]["title"] == "release pass: backend"
    assert out[1]["title"] == "release pass: frontend"
    assert out[2]["title"] == "tests"


def test_titles_differing_only_by_case_still_collide() -> None:
    calls = [{"title": "Release Pass"}, {"title": "release pass"}]
    out = disambiguate_titles(calls, ["backend", "frontend"])
    assert out[0]["title"] != out[1]["title"]
    assert "backend" in out[0]["title"]


@pytest.mark.asyncio
async def test_four_expanders_reaching_for_one_name_still_give_four_tabs() -> None:
    provider = ScriptedProvider(
        default=spawn_result({**good_args(), "title": "release pass"})
    )
    result = await expand(service(provider))
    titles = [args_of(call)["title"] for call in result.calls]
    assert len(set(titles)) == 4


# ---------------------------------------------------------------------------
# finalize_arguments, directly.
# ---------------------------------------------------------------------------


def test_finalize_repairs_a_near_miss_key_against_the_real_schema() -> None:
    spawn_plan = plan()
    merged, _ = finalize_arguments(
        {"title": "t", "task_prompt": "p", "acceptanceCriteria": "a"},
        plan=spawn_plan,
        branch=spawn_plan.branches[0],
        spawn_schema=SPAWN_SCHEMA,
    )
    assert merged["taskPrompt"] == "p"


@pytest.mark.parametrize(
    "args",
    [
        {"title": "  ", "taskPrompt": "p", "acceptanceCriteria": "a"},
        {"title": "t", "taskPrompt": "", "acceptanceCriteria": "a"},
        {"title": "t", "taskPrompt": "p"},
        {"title": 7, "taskPrompt": "p", "acceptanceCriteria": "a"},
    ],
)
def test_finalize_rejects_an_unusable_generated_field(args: dict[str, Any]) -> None:
    spawn_plan = plan()
    with pytest.raises(BranchUnusable):
        finalize_arguments(
            args, plan=spawn_plan, branch=spawn_plan.branches[0], spawn_schema=SPAWN_SCHEMA
        )


# ---------------------------------------------------------------------------
# Accounting on the failure paths.
#
# A branch that made two completions and then could not use either still BILLED
# both. Reporting that fallback as "no call was made" would shorten the turn's
# total by exactly the calls that went wrong.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_branch_that_fails_twice_still_reports_what_it_spent() -> None:
    provider = ScriptedProvider(
        answers={"backend": [spawn_result("nope"), spawn_result("still nope")]}
    )
    result = await expand(service(provider))
    backend = result.branches[0]
    assert backend.fallback is True
    assert backend.repaired is True
    assert backend.usage is not None
    assert backend.usage.prompt_tokens == 2000
    assert result.usage is not None
    # Two failed backend calls plus three good branches.
    assert result.usage.prompt_tokens == 5000


@pytest.mark.asyncio
async def test_a_repair_call_that_raises_keeps_the_first_calls_usage() -> None:
    provider = ScriptedProvider(
        answers={"backend": [spawn_result("nope"), upstream_unavailable("down")]}
    )
    result = await expand(service(provider))
    backend = result.branches[0]
    assert backend.fallback is True
    assert backend.fallback_reason == "upstream:upstream_unavailable"
    assert backend.usage is not None
    assert backend.usage.prompt_tokens == 1000


@pytest.mark.asyncio
async def test_the_second_failure_kind_is_what_gets_reported() -> None:
    provider = ScriptedProvider(
        answers={
            "backend": [
                spawn_result("nope"),
                spawn_result({"taskPrompt": "p", "acceptanceCriteria": "a"}),
            ]
        }
    )
    result = await expand(service(provider))
    assert result.branches[0].fallback_reason == "missing_field"


@pytest.mark.asyncio
async def test_an_unknown_cost_on_one_branch_makes_the_total_incomplete() -> None:
    # "Unknown is not zero" — a total that silently omits an unreported branch
    # is the one wrong answer that looks right.
    provider = ScriptedProvider(
        answers={
            "backend": spawn_result(good_args(), usage=ProviderUsage(cost=0.01)),
            "frontend": spawn_result(good_args(), usage=ProviderUsage(cost=None)),
            "tests": spawn_result(good_args(), usage=ProviderUsage(cost=0.02)),
            "performance": spawn_result(good_args(), usage=ProviderUsage(cost=0.03)),
        }
    )
    result = await expand(service(provider))
    assert result.usage is not None
    assert result.usage.cost == pytest.approx(0.06)
    assert result.usage.cost_complete is False


# ---------------------------------------------------------------------------
# The context-window guard.
# ---------------------------------------------------------------------------


def _oversized_request() -> ProviderChatRequest:
    return replace(base_request(), messages=[{"role": "user", "content": "x" * 3_000_000}])


@pytest.mark.asyncio
async def test_a_request_that_cannot_fit_falls_back_without_calling_the_provider() -> None:
    # Four requests that cannot fit would buy four upstream rejections and the
    # user would wait for all of them.
    provider = ScriptedProvider()
    result = await ForkPromptExpansionService(settings=settings(), provider=provider).expand(
        base_request=_oversized_request(),
        plan=plan(),
        fork_call_id="call_fork_1",
        spawn_schema=SPAWN_SCHEMA,
        request_id="req-1",
    )
    assert provider.requests == []
    assert all(branch.fallback for branch in result.branches)
    assert {b.fallback_reason for b in result.branches} == {"context_window"}
    assert len(result.calls) == 4


# ---------------------------------------------------------------------------
# Titles, at the edges the cap creates.
# ---------------------------------------------------------------------------


def test_a_duplicate_title_at_the_cap_still_gains_its_discriminator() -> None:
    # Appending the suffix and THEN truncating throws the discriminator away for
    # two titles already at the limit, leaving two identical tabs — the exact
    # failure disambiguation exists to prevent.
    long_title = "r" * 80
    out = disambiguate_titles(
        [{"title": long_title}, {"title": long_title}], ["backend", "frontend"]
    )
    assert out[0]["title"] != out[1]["title"]
    assert all(len(call["title"]) <= 80 for call in out)


def test_branch_ids_differing_only_by_separator_produce_different_titles() -> None:
    # `a_b` and `a-b` are distinct valid ids that humanize to the same word, so
    # the suffix is the RAW id.
    out = disambiguate_titles([{"title": "same"}, {"title": "same"}], ["a_b", "a-b"])
    assert out[0]["title"] != out[1]["title"]


def test_a_whitespace_only_title_hint_falls_back_to_the_branch_id() -> None:
    # A hint of spaces is truthy before sanitizing and empty after it, and the
    # CLI rejects a blank title outright — which would break the promise that a
    # failed expansion still spawns.
    hinted = plan(
        branches=[
            {
                "id": "backend",
                "worktreeId": "/repo/backend",
                "brief": "Own the backend scope.",
                "titleHint": "   ",
            },
            *[
                {
                    "id": bid,
                    "worktreeId": f"/repo/{bid}",
                    "brief": f"Own the {bid} scope.",
                }
                for bid in BRANCH_IDS[1:]
            ],
        ]
    )
    assert fallback_arguments(hinted, hinted.branches[0])["title"] == "backend"
