"""Workflow-intelligence task tests: plan, reconcile, and resume digest.

Covers the structural validation layer (cycles, duplicate ids, unknown
references, terminal-status immutability), the degrade-and-warn tool grounding,
the repair retry, and the id-grounding of resume rankings.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fakes import FakeProvider, make_result
from pydantic import SecretStr

from daintree_assistant_server.contracts.tasks import TaskRequest
from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.services.task_runner import TaskRunner

SKILL_PREP = "daintree.workflow.prep-branch-for-review"

TOOLS = [
    {"name": "context.snapshot", "risk": "read"},
    {"name": "agentTask.spawnForEdits", "risk": "project"},
    {"name": "terminal.await.async", "risk": "local"},
]


def node(node_id: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": node_id,
        "title": f"Do {node_id}",
        "kind": "orient",
        "status": "pending",
        "depends_on": [],
    }
    base.update(over)
    return base


def plan_payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "goal": "Fix watcher tests",
        "status": "active",
        "success_criteria": ["watcher tests pass"],
        "nodes": [
            node("n_orient", kind="orient", status="ready", tool_name="context.snapshot"),
            node(
                "n_fix",
                kind="delegate",
                depends_on=["n_orient"],
                tool_name="agentTask.spawnForEdits",
                async_policy="async_required",
                owner="agent:claude",
            ),
            node("n_verify", kind="verify", depends_on=["n_fix"]),
        ],
        "edges": [],
        "next_action": {
            "label": "Snapshot the repo state",
            "tool_name": "context.snapshot",
            "node_id": "n_orient",
        },
        "confidence": 0.9,
    }
    base.update(over)
    return base


TEST_API_KEY = SecretStr("test-caller-key")

async def _run(runner: TaskRunner, task: str, inp: dict[str, Any]) -> dict[str, Any]:
    result = await runner.run(TaskRequest(task=task, input=inp), api_key=TEST_API_KEY)
    return result.output


# --------------------------------------------------------------------------
# workflow_plan
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_happy_path(task_runner: TaskRunner, fake_provider: FakeProvider) -> None:
    fake_provider.complete_result = make_result(content=json.dumps(plan_payload()))
    out = await _run(
        task_runner, "workflow_plan", {"goal": "Fix watcher tests", "tool_inventory": TOOLS}
    )
    assert out["goal"] == "Fix watcher tests"
    assert [n["id"] for n in out["nodes"]] == ["n_orient", "n_fix", "n_verify"]
    assert out["nodes"][1]["async_policy"] == "async_required"
    assert out["next_action"]["tool_name"] == "context.snapshot"
    assert out["warnings"] == []
    assert len(fake_provider.requests) == 1


@pytest.mark.asyncio
async def test_plan_duplicate_node_ids_repair(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    bad = plan_payload(nodes=[node("n_a"), node("n_a")])
    results = iter([
        make_result(content=json.dumps(bad)),
        make_result(content=json.dumps(plan_payload())),
    ])
    fake_provider.complete_result = lambda _req: next(results)
    out = await _run(task_runner, "workflow_plan", {"goal": "g", "tool_inventory": TOOLS})
    assert len(fake_provider.requests) == 2  # repaired
    assert [n["id"] for n in out["nodes"]] == ["n_orient", "n_fix", "n_verify"]
    # The repair retry re-anchors on the plan contract.
    repair_text = fake_provider.requests[1].messages[-1]["content"]
    assert "acyclic" in repair_text
    # ...and names the STRUCTURAL defect the validator found, rather than telling
    # a model that produced valid, correctly-shaped JSON that its JSON was bad.
    # The opener is asserted as well as the detail: reverting the validator
    # re-raise to the default `schema` kind would still carry "duplicate node id"
    # and would still not say "not valid JSON", so the detail alone pins nothing.
    assert repair_text.startswith(
        "Your previous response was valid JSON of the right shape, but it is not a "
        "usable result."
    )
    assert "The problem: <repair_error>duplicate node id" in repair_text
    assert "not valid JSON" not in repair_text
    # The rejected graph comes back framed as data, inside the user-role
    # correction — never as a fabricated assistant turn.
    assert "<rejected_output>" in repair_text
    assert all(m["role"] != "assistant" for m in fake_provider.requests[1].messages)


@pytest.mark.asyncio
async def test_plan_cycle_rejected(task_runner: TaskRunner, fake_provider: FakeProvider) -> None:
    cyclic = plan_payload(
        nodes=[node("n_a", depends_on=["n_b"]), node("n_b", depends_on=["n_a"])],
        next_action=None,
    )
    fake_provider.complete_result = make_result(content=json.dumps(cyclic))
    with pytest.raises(DaintreeError) as exc:
        await _run(task_runner, "workflow_plan", {"goal": "g"})
    assert exc.value.code == "task_output_invalid"


@pytest.mark.asyncio
async def test_plan_cycle_via_edges_rejected(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    cyclic = plan_payload(
        nodes=[node("n_a"), node("n_b", depends_on=["n_a"])],
        edges=[{"source": "n_b", "target": "n_a"}],
        next_action=None,
    )
    fake_provider.complete_result = make_result(content=json.dumps(cyclic))
    with pytest.raises(DaintreeError) as exc:
        await _run(task_runner, "workflow_plan", {"goal": "g"})
    assert exc.value.code == "task_output_invalid"


@pytest.mark.asyncio
async def test_plan_unknown_dependency_rejected(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    bad = plan_payload(nodes=[node("n_a", depends_on=["n_ghost"])], next_action=None)
    fake_provider.complete_result = make_result(content=json.dumps(bad))
    with pytest.raises(DaintreeError) as exc:
        await _run(task_runner, "workflow_plan", {"goal": "g"})
    assert exc.value.code == "task_output_invalid"


@pytest.mark.asyncio
async def test_plan_edge_unknown_endpoint_rejected(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    bad = plan_payload(edges=[{"source": "n_orient", "target": "n_ghost"}], next_action=None)
    fake_provider.complete_result = make_result(content=json.dumps(bad))
    with pytest.raises(DaintreeError) as exc:
        await _run(task_runner, "workflow_plan", {"goal": "g"})
    assert exc.value.code == "task_output_invalid"


@pytest.mark.asyncio
async def test_plan_too_many_nodes_rejected(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    bad = plan_payload(nodes=[node(f"n_{i}") for i in range(21)], next_action=None)
    fake_provider.complete_result = make_result(content=json.dumps(bad))
    with pytest.raises(DaintreeError) as exc:
        await _run(task_runner, "workflow_plan", {"goal": "g"})
    assert exc.value.code == "task_output_invalid"


@pytest.mark.asyncio
async def test_plan_unknown_next_action_tool_dropped_with_warning(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    payload = plan_payload(
        next_action={"label": "Do magic", "tool_name": "magic.wand", "node_id": "n_orient"}
    )
    fake_provider.complete_result = make_result(content=json.dumps(payload))
    out = await _run(task_runner, "workflow_plan", {"goal": "g", "tool_inventory": TOOLS})
    # Degrades, never 502s: recommendation dropped, warning explains.
    assert out["next_action"] is None
    assert any("magic.wand" in w for w in out["warnings"])
    assert len(fake_provider.requests) == 1  # no repair round


@pytest.mark.asyncio
async def test_plan_unknown_node_tool_warns_but_keeps_node(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    payload = plan_payload()
    payload["nodes"][2]["tool_name"] = "tests.runMagically"
    fake_provider.complete_result = make_result(content=json.dumps(payload))
    out = await _run(task_runner, "workflow_plan", {"goal": "g", "tool_inventory": TOOLS})
    assert out["nodes"][2]["tool_name"] == "tests.runMagically"
    assert any("tests.runMagically" in w for w in out["warnings"])
    assert out["next_action"] is not None  # its own tool is valid


@pytest.mark.asyncio
async def test_plan_without_inventory_skips_tool_grounding(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    payload = plan_payload(
        next_action={"label": "Do magic", "tool_name": "magic.wand", "node_id": "n_orient"}
    )
    fake_provider.complete_result = make_result(content=json.dumps(payload))
    out = await _run(task_runner, "workflow_plan", {"goal": "g"})
    assert out["next_action"]["tool_name"] == "magic.wand"
    assert out["warnings"] == []


@pytest.mark.asyncio
async def test_plan_confidence_clamped(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    fake_provider.complete_result = make_result(content=json.dumps(plan_payload(confidence=5)))
    out = await _run(task_runner, "workflow_plan", {"goal": "g"})
    assert out["confidence"] == 1.0


@pytest.mark.asyncio
async def test_plan_prompt_includes_skill_outline_not_body(
    task_runner: TaskRunner, fake_provider: FakeProvider, catalog
) -> None:
    fake_provider.complete_result = make_result(content=json.dumps(plan_payload()))
    await _run(
        task_runner,
        "workflow_plan",
        {"goal": "g", "tool_inventory": TOOLS, "active_skill_ids": [SKILL_PREP]},
    )
    user_msg = fake_provider.requests[0].messages[-1]["content"]
    assert "Active skill outlines" in user_msg
    assert f"- id: {SKILL_PREP}" in user_msg
    # Outline only — the raw runbook body must never reach the task prompt.
    body = catalog.get(SKILL_PREP).body
    assert body not in user_msg


@pytest.mark.asyncio
async def test_plan_unknown_skill_id_ignored(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    fake_provider.complete_result = make_result(content=json.dumps(plan_payload()))
    await _run(
        task_runner, "workflow_plan", {"goal": "g", "active_skill_ids": ["nope.missing"]}
    )
    user_msg = fake_provider.requests[0].messages[-1]["content"]
    assert "Active skill outlines" not in user_msg


@pytest.mark.asyncio
async def test_plan_requires_goal(task_runner: TaskRunner) -> None:
    with pytest.raises(DaintreeError) as exc:
        await task_runner.run(TaskRequest(task="workflow_plan", input={}), api_key=TEST_API_KEY)
    assert exc.value.status_code == 400


# --------------------------------------------------------------------------
# workflow_reconcile
# --------------------------------------------------------------------------


def workflow_snapshot(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "wf_1",
        "goal": "Fix watcher tests",
        "status": "active",
        "revision": 7,
        "nodes": [
            node("n_orient", status="done"),
            node("n_fix", status="waiting", depends_on=["n_orient"]),
            node("n_verify", status="pending", depends_on=["n_fix"]),
        ],
    }
    base.update(over)
    return base


def reconcile_payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "patch": {
            "base_revision": 7,
            "node_updates": [{"node_id": "n_fix", "status": "done"}],
            "rationale": "Agent completed the edits.",
        },
        "next_action": {
            "label": "Verify the tests",
            "tool_name": "terminal.await.async",
            "node_id": "n_verify",
        },
        "summary": "Fix node completed; verification is ready.",
        "confidence": 0.8,
    }
    base.update(over)
    return base


def reconcile_input(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "workflow": workflow_snapshot(),
        "reason": "tool_result",
        "tool_inventory": TOOLS,
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_reconcile_happy_path(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    fake_provider.complete_result = make_result(content=json.dumps(reconcile_payload()))
    out = await _run(task_runner, "workflow_reconcile", reconcile_input())
    assert out["patch"]["node_updates"] == [
        {"node_id": "n_fix", "status": "done", "title": None, "last_error": None, "note": None}
    ]
    assert out["next_action"]["node_id"] == "n_verify"
    assert out["patch"]["base_revision"] == 7


@pytest.mark.asyncio
async def test_reconcile_unknown_node_rejected(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    bad = reconcile_payload(
        patch={"node_updates": [{"node_id": "n_ghost", "status": "done"}]}, next_action=None
    )
    fake_provider.complete_result = make_result(content=json.dumps(bad))
    with pytest.raises(DaintreeError) as exc:
        await _run(task_runner, "workflow_reconcile", reconcile_input())
    assert exc.value.code == "task_output_invalid"


@pytest.mark.asyncio
async def test_reconcile_terminal_transition_rejected(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    # n_orient is done (final); moving it back to running is an impossible transition.
    bad = reconcile_payload(
        patch={"node_updates": [{"node_id": "n_orient", "status": "running"}]},
        next_action=None,
    )
    fake_provider.complete_result = make_result(content=json.dumps(bad))
    with pytest.raises(DaintreeError) as exc:
        await _run(task_runner, "workflow_reconcile", reconcile_input())
    assert exc.value.code == "task_output_invalid"


@pytest.mark.asyncio
async def test_reconcile_same_terminal_status_allowed(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    same = reconcile_payload(
        patch={"node_updates": [{"node_id": "n_orient", "status": "done"}]}, next_action=None
    )
    fake_provider.complete_result = make_result(content=json.dumps(same))
    out = await _run(task_runner, "workflow_reconcile", reconcile_input())
    assert out["patch"]["node_updates"][0]["status"] == "done"


@pytest.mark.asyncio
async def test_reconcile_add_nodes_and_edges(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    payload = reconcile_payload(
        patch={
            "node_updates": [{"node_id": "n_fix", "status": "failed", "last_error": "lint"}],
            "add_nodes": [node("n_retry", kind="delegate", depends_on=["n_orient"])],
            "add_edges": [{"source": "n_retry", "target": "n_verify"}],
            "add_blockers": [{"summary": "Agent hit lint errors", "node_id": "n_fix"}],
        },
        next_action=None,
    )
    fake_provider.complete_result = make_result(content=json.dumps(payload))
    out = await _run(task_runner, "workflow_reconcile", reconcile_input())
    assert out["patch"]["add_nodes"][0]["id"] == "n_retry"
    assert out["patch"]["add_blockers"][0]["summary"] == "Agent hit lint errors"


@pytest.mark.asyncio
async def test_reconcile_add_node_duplicate_id_rejected(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    bad = reconcile_payload(patch={"add_nodes": [node("n_fix")]}, next_action=None)
    fake_provider.complete_result = make_result(content=json.dumps(bad))
    with pytest.raises(DaintreeError) as exc:
        await _run(task_runner, "workflow_reconcile", reconcile_input())
    assert exc.value.code == "task_output_invalid"


@pytest.mark.asyncio
async def test_reconcile_cycle_via_added_edge_rejected(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    # n_verify already depends on n_fix; an edge n_verify -> n_fix closes a cycle.
    bad = reconcile_payload(
        patch={"add_edges": [{"source": "n_verify", "target": "n_fix"}]}, next_action=None
    )
    fake_provider.complete_result = make_result(content=json.dumps(bad))
    with pytest.raises(DaintreeError) as exc:
        await _run(task_runner, "workflow_reconcile", reconcile_input())
    assert exc.value.code == "task_output_invalid"


@pytest.mark.asyncio
async def test_reconcile_unknown_tool_next_action_dropped(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    payload = reconcile_payload(
        next_action={"label": "Do magic", "tool_name": "magic.wand"}
    )
    fake_provider.complete_result = make_result(content=json.dumps(payload))
    out = await _run(task_runner, "workflow_reconcile", reconcile_input())
    assert out["next_action"] is None
    assert any("magic.wand" in w for w in out["warnings"])


@pytest.mark.asyncio
async def test_reconcile_unparseable_snapshot_fails_open(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    # The client's snapshot has no readable nodes: reference checks are skipped
    # (the client re-validates against its real graph) instead of 502ing.
    payload = reconcile_payload(next_action=None)
    fake_provider.complete_result = make_result(content=json.dumps(payload))
    out = await _run(
        task_runner,
        "workflow_reconcile",
        reconcile_input(workflow={"id": "wf_1", "nodes": "corrupt"}),
    )
    assert out["patch"]["node_updates"][0]["node_id"] == "n_fix"


@pytest.mark.asyncio
async def test_reconcile_patch_internal_cycle_rejected_without_a_snapshot(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    # A cycle among the patch's OWN add_nodes/add_edges needs no existing graph to
    # detect, so the acyclic check runs even when the snapshot is unreadable. Only
    # _check_references stays gated (it would false-reject a legitimate reference
    # into a graph we simply could not parse).
    bad = reconcile_payload(
        patch={
            "add_nodes": [
                {"id": "n_a", "title": "A", "kind": "tool_call"},
                {"id": "n_b", "title": "B", "kind": "tool_call", "depends_on": ["n_a"]},
            ],
            "add_edges": [{"source": "n_b", "target": "n_a"}],
            "node_updates": [],
        },
        next_action=None,
    )
    fake_provider.complete_result = make_result(content=json.dumps(bad))
    with pytest.raises(DaintreeError) as exc:
        await _run(
            task_runner,
            "workflow_reconcile",
            reconcile_input(workflow={"id": "wf_1", "nodes": "corrupt"}),
        )
    assert exc.value.code == "task_output_invalid"


@pytest.mark.asyncio
async def test_reconcile_requires_reason(task_runner: TaskRunner) -> None:
    with pytest.raises(DaintreeError) as exc:
        await task_runner.run(
            TaskRequest(task="workflow_reconcile", input={"workflow": workflow_snapshot()}),
            api_key=TEST_API_KEY,
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_reconcile_repair_path(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    results = iter([
        make_result(content="not json"),
        make_result(content=json.dumps(reconcile_payload())),
    ])
    fake_provider.complete_result = lambda _req: next(results)
    out = await _run(task_runner, "workflow_reconcile", reconcile_input())
    assert out["summary"].startswith("Fix node completed")
    assert len(fake_provider.requests) == 2
    repair_text = fake_provider.requests[1].messages[-1]["content"]
    assert "done/skipped/cancelled" in repair_text


# --------------------------------------------------------------------------
# workflow_resume_digest
# --------------------------------------------------------------------------


def resume_input() -> dict[str, Any]:
    return {
        "workflows": [
            {"id": "wf_1", "goal": "Fix watcher tests", "status": "active"},
            {"id": "wf_2", "goal": "Prepare PR for issue 286", "status": "blocked"},
        ],
        "runtime_summary": {"project_id": "p1"},
        "user_message": "where were we?",
    }


def resume_payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ranked": [
            {
                "workflow_id": "wf_2",
                "rank": 1,
                "headline": "Blocked on branch strategy choice",
                "status": "blocked",
                "blockers": ["waiting on user: choose branch strategy"],
            },
            {
                "workflow_id": "wf_1",
                "rank": 2,
                "headline": "Agent finished edits; tests not yet run",
                "status": "active",
            },
        ],
        "suggested_focus_workflow_id": "wf_2",
        "assistant_context_lines": ["wf_2 blocked on user decision; wf_1 ready to verify"],
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_resume_happy_path(task_runner: TaskRunner, fake_provider: FakeProvider) -> None:
    fake_provider.complete_result = make_result(content=json.dumps(resume_payload()))
    out = await _run(task_runner, "workflow_resume_digest", resume_input())
    assert [r["workflow_id"] for r in out["ranked"]] == ["wf_2", "wf_1"]
    assert out["suggested_focus_workflow_id"] == "wf_2"
    assert out["assistant_context_lines"]


@pytest.mark.asyncio
async def test_resume_drops_invented_workflows_and_focus(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    payload = resume_payload(
        ranked=[
            {"workflow_id": "wf_ghost", "rank": 1, "headline": "Invented"},
            {"workflow_id": "wf_1", "rank": 2, "headline": "Real"},
        ],
        suggested_focus_workflow_id="wf_ghost",
    )
    fake_provider.complete_result = make_result(content=json.dumps(payload))
    out = await _run(task_runner, "workflow_resume_digest", resume_input())
    assert [r["workflow_id"] for r in out["ranked"]] == ["wf_1"]
    assert out["suggested_focus_workflow_id"] is None


@pytest.mark.asyncio
async def test_resume_orders_by_rank(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    payload = resume_payload(
        ranked=[
            {"workflow_id": "wf_1", "rank": 2, "headline": "Second"},
            {"workflow_id": "wf_2", "rank": 1, "headline": "First"},
        ]
    )
    fake_provider.complete_result = make_result(content=json.dumps(payload))
    out = await _run(task_runner, "workflow_resume_digest", resume_input())
    assert [r["rank"] for r in out["ranked"]] == [1, 2]


@pytest.mark.asyncio
async def test_resume_empty_workflows(
    task_runner: TaskRunner, fake_provider: FakeProvider
) -> None:
    fake_provider.complete_result = make_result(
        content=json.dumps({"ranked": [], "suggested_focus_workflow_id": None})
    )
    out = await _run(task_runner, "workflow_resume_digest", {"workflows": []})
    assert out == {
        "ranked": [],
        "suggested_focus_workflow_id": None,
        "assistant_context_lines": [],
    }
