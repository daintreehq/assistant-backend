"""Golden wire-contract tests for the workflow-intelligence tasks.

The backend Pydantic models in ``contracts/tasks.py`` are the AUTHORITY for the
workflow wire shapes; the Go CLI copies the fixture files under
``fixtures/workflow/`` verbatim into its testdata and decodes them with its real
DTOs. These tests pin the fixtures to the exact bytes the server produces
(``model_dump()`` — no aliases, no ``exclude_none``, every field always present)
so drift on either side fails a test instead of failing in production.

The CLI-side copies live at ``internal/backend/testdata/workflow/`` and are read
by ``internal/backend/workflowwire_test.go``. They must stay byte-identical to
these — when a shape below changes, update BOTH.

See ``fixtures/workflow/README.md`` for which wire layer each file represents.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from daintree_assistant_server.contracts.tasks import (
    NodePatchOut,
    WorkflowBlockerOut,
    WorkflowEdgeOut,
    WorkflowNodeOut,
    WorkflowPlanOutput,
    WorkflowReconcileInput,
    WorkflowReconcileOutput,
    WorkflowResumeDigestOutput,
)
from daintree_assistant_server.services.workflow_validation import (
    WorkflowValidationError,
    validate_plan,
    validate_reconcile,
)

FIXTURES = Path(__file__).parent / "fixtures" / "workflow"


def _read(name: str) -> tuple[dict[str, Any], str]:
    raw = (FIXTURES / name).read_text(encoding="utf-8")
    data = json.loads(raw)
    assert isinstance(data, dict)
    return data, raw


def _wire_bytes(dumped: dict[str, Any]) -> str:
    """Serialize a ``model_dump()`` dict exactly as the fixtures are written."""
    return json.dumps(dumped, indent=2, ensure_ascii=False) + "\n"


@pytest.fixture()
def reconcile_input() -> dict[str, Any]:
    data, _ = _read("reconcile_input.json")
    return data


# --------------------------------------------------------------------------
# workflow_plan output
# --------------------------------------------------------------------------


def test_plan_output_round_trips_byte_identical() -> None:
    data, raw = _read("plan_output.json")
    plan = WorkflowPlanOutput.model_validate(data)
    dumped = plan.model_dump()  # exact task_runner serialization (_p_workflow_plan)
    assert dumped == data
    assert _wire_bytes(dumped) == raw


def test_plan_output_survives_semantic_validation_unchanged(
    reconcile_input: dict[str, Any],
) -> None:
    # The fixture graph must be structurally valid (unique ids, known refs,
    # acyclic) and fully tool-grounded: validation must not null next_action or
    # append warnings.
    data, _ = _read("plan_output.json")
    plan = WorkflowPlanOutput.model_validate(data)
    validated = validate_plan(plan, tool_inventory=reconcile_input["tool_inventory"])
    assert validated.model_dump() == data


def test_plan_output_drift_prone_keys() -> None:
    data, _ = _read("plan_output.json")
    node = data["nodes"][0]
    assert "depends_on" in node
    assert "dependsOn" not in node
    for key in (
        "id",
        "title",
        "kind",
        "status",
        "depends_on",
        "tool_name",
        "tool_args",
        "risk",
        "requires_confirm",
        "expected_evidence",
        "async_policy",
        "owner",
    ):
        assert key in node, f"node missing canonical key {key!r}"
    for edge in data["edges"]:
        assert set(edge) == {"source", "target"}
    action = data["next_action"]
    assert set(action) == {
        "label",
        "tool_name",
        "args",
        "risk",
        "requires_confirmation",
        "node_id",
    }


# --------------------------------------------------------------------------
# workflow_reconcile output
# --------------------------------------------------------------------------


def test_reconcile_output_round_trips_byte_identical() -> None:
    data, raw = _read("reconcile_output.json")
    out = WorkflowReconcileOutput.model_validate(data)
    dumped = out.model_dump()  # exact task_runner serialization (_p_workflow_reconcile)
    assert dumped == data
    assert _wire_bytes(dumped) == raw


def test_reconcile_output_valid_against_reconcile_input_snapshot(
    reconcile_input: dict[str, Any],
) -> None:
    # The output fixture is a real patch against the input fixture's graph:
    # semantic validation (unknown refs, terminal-status guard, cycles, tool
    # grounding) must accept it without degrading anything.
    data, _ = _read("reconcile_output.json")
    out = WorkflowReconcileOutput.model_validate(data)
    inp = WorkflowReconcileInput.model_validate(reconcile_input)
    validated = validate_reconcile(
        out, workflow=inp.workflow, tool_inventory=inp.tool_inventory
    )
    assert validated.model_dump() == data


def test_reconcile_output_drift_prone_keys() -> None:
    data, _ = _read("reconcile_output.json")
    patch = data["patch"]
    assert "base_revision" in patch
    assert set(patch["node_updates"][0]) == {
        "node_id",
        "status",
        "title",
        "last_error",
        "note",
    }
    assert set(patch["resource_updates"][0]) == {"resource_id", "status", "node_id", "label"}
    for blocker in patch["add_blockers"]:
        assert set(blocker) == {"id", "summary", "node_id", "kind"}
    for edge in patch["add_edges"]:
        assert set(edge) == {"source", "target"}
    assert "depends_on" in patch["add_nodes"][0]
    assert "resolve_blockers" in patch


# --------------------------------------------------------------------------
# workflow_resume_digest output
# --------------------------------------------------------------------------


def test_resume_output_round_trips_byte_identical() -> None:
    data, raw = _read("resume_output.json")
    out = WorkflowResumeDigestOutput.model_validate(data)
    dumped = out.model_dump()  # exact task_runner serialization (_p_workflow_resume)
    assert dumped == data
    assert _wire_bytes(dumped) == raw


def test_resume_output_drift_prone_keys() -> None:
    data, _ = _read("resume_output.json")
    full, minimal = data["ranked"]
    for item in (full, minimal):
        assert set(item) == {
            "workflow_id",
            "rank",
            "headline",
            "status",
            "summary",
            "blockers",
            "recommended_action",
        }
    # Defaults are serialized, never omitted: the minimal item still carries
    # every key, with "" / [] / null values.
    assert minimal["status"] == ""
    assert minimal["blockers"] == []
    assert minimal["recommended_action"] is None
    assert full["recommended_action"]["requires_confirmation"] is False
    assert "suggested_focus_workflow_id" in data
    assert "assistant_context_lines" in data


# --------------------------------------------------------------------------
# workflow_reconcile INPUT (client -> server)
# --------------------------------------------------------------------------


def test_reconcile_input_is_accepted_canonically(reconcile_input: dict[str, Any]) -> None:
    inp = WorkflowReconcileInput.model_validate(reconcile_input)
    # Round-trips without loss: nothing in the canonical fixture is dropped.
    assert inp.model_dump() == reconcile_input
    assert inp.reason == "tool_result"
    node = inp.workflow["nodes"][0]
    assert set(node) >= {"id", "status", "depends_on"}


def _cyclic_patch() -> WorkflowReconcileOutput:
    """A patch whose edge closes a cycle through the snapshot's depends_on chain
    (orient-repo -> ... -> report-outcome -> orient-repo)."""
    return WorkflowReconcileOutput.model_validate(
        {"patch": {"add_edges": [{"source": "report-outcome", "target": "orient-repo"}]}}
    )


def _terminal_move_patch() -> WorkflowReconcileOutput:
    """A patch that tries to move a done (final) node back to running."""
    return WorkflowReconcileOutput.model_validate(
        {"patch": {"node_updates": [{"node_id": "await-agent", "status": "running"}]}}
    )


def test_canonical_input_enables_cycle_detection(reconcile_input: dict[str, Any]) -> None:
    inp = WorkflowReconcileInput.model_validate(reconcile_input)
    with pytest.raises(WorkflowValidationError, match="cycle"):
        validate_reconcile(
            _cyclic_patch(), workflow=inp.workflow, tool_inventory=inp.tool_inventory
        )


def test_canonical_input_enables_terminal_status_guard(
    reconcile_input: dict[str, Any],
) -> None:
    inp = WorkflowReconcileInput.model_validate(reconcile_input)
    with pytest.raises(WorkflowValidationError, match="final"):
        validate_reconcile(
            _terminal_move_patch(), workflow=inp.workflow, tool_inventory=inp.tool_inventory
        )


def test_camelcase_depends_on_silently_disables_cycle_detection(
    reconcile_input: dict[str, Any],
) -> None:
    # HAZARD, pinned deliberately: the input models are extra="ignore" and the
    # snapshot reader (_existing_graph) is lenient, so a drifted client sending
    # camelCase `dependsOn` is NOT rejected — the dependency chain just becomes
    # invisible and the cycle-creating patch above sails through validation.
    # The canonical snake_case form is therefore load-bearing for safety, not
    # style. If this test starts FAILING because the drifted snapshot is now
    # rejected (or its deps are now read), the backend has started accepting
    # camelCase: update the fixtures and the Go contract deliberately, never
    # silently.
    drifted = json.loads(json.dumps(reconcile_input))
    for node in drifted["workflow"]["nodes"]:
        node["dependsOn"] = node.pop("depends_on")
    inp = WorkflowReconcileInput.model_validate(drifted)  # accepted, not rejected
    validated = validate_reconcile(
        _cyclic_patch(), workflow=inp.workflow, tool_inventory=inp.tool_inventory
    )
    # The cycle went undetected: the patch comes back as if it were valid.
    assert validated.patch.add_edges[0].source == "report-outcome"


def test_camelcase_node_ids_silently_disable_all_graph_checks(
    reconcile_input: dict[str, Any],
) -> None:
    # Same hazard, worse blast radius: with `nodeId` instead of `id` the reader
    # sees an empty graph, so unknown-node references AND the terminal-status
    # guard are skipped entirely. Pinned so accepting this drift must be a
    # deliberate contract change.
    drifted = json.loads(json.dumps(reconcile_input))
    for node in drifted["workflow"]["nodes"]:
        node["nodeId"] = node.pop("id")
    inp = WorkflowReconcileInput.model_validate(drifted)
    validated = validate_reconcile(
        _terminal_move_patch(), workflow=inp.workflow, tool_inventory=inp.tool_inventory
    )
    assert validated.patch.node_updates[0].node_id == "await-agent"


# --------------------------------------------------------------------------
# Output-model rejection of drifted key spellings
# --------------------------------------------------------------------------


def test_output_models_reject_camelcase_and_from_to_spellings() -> None:
    # Required fields make the canonical spelling load-bearing on the OUTPUT
    # side: a drifted key means the required snake_case key is absent.
    with pytest.raises(ValidationError):
        WorkflowEdgeOut.model_validate({"from": "a", "to": "b"})
    with pytest.raises(ValidationError):
        NodePatchOut.model_validate({"nodeId": "n1", "status": "done"})
    with pytest.raises(ValidationError):
        WorkflowBlockerOut.model_validate({"kind": "other"})  # summary is required


def test_node_camelcase_depends_on_is_dropped_not_read() -> None:
    # HAZARD, pinned deliberately: WorkflowNodeOut is extra="ignore", so a node
    # arriving with `dependsOn` validates — but its dependencies are silently
    # LOST (depends_on falls back to []). Only the canonical snake_case key is
    # ever read. If this assertion starts failing, camelCase became readable:
    # that is a contract change, update the fixtures on both sides.
    node = WorkflowNodeOut.model_validate(
        {"id": "n1", "title": "T", "kind": "orient", "dependsOn": ["n0"]}
    )
    assert node.depends_on == []
