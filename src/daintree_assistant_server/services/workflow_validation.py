"""Semantic validation for workflow-intelligence task outputs.

Structural checks Pydantic field validation cannot express: node-id uniqueness,
dependency/edge existence, DAG-ness, terminal-status immutability, and tool-name
grounding against the client-supplied tool inventory.

Two failure postures, chosen deliberately:

- A STRUCTURAL defect (cycle, duplicate id, unknown node reference, illegal
  transition) raises :class:`WorkflowValidationError`. The task runner treats it
  like a parse failure, so the one repair retry re-anchors the model on the
  contract; if the repair also fails the task 502s rather than shipping a
  corrupt graph to the client.
- A TOOL-NAME mismatch DEGRADES: the recommendation is dropped (``next_action``
  nulled) and a warning explains the missing capability. A model that plans
  around a tool the client doesn't have should not sink the whole task — the
  client computes ready nodes locally and validates every tool name again
  before dispatch anyway.

The client re-validates everything against its own invariants before committing
a plan or patch; this layer exists so an obviously-broken output never leaves
the server as a "valid" task result.
"""

from __future__ import annotations

from typing import Any

from ..contracts.tasks import (
    TERMINAL_NODE_STATUSES,
    RecommendedActionOut,
    WorkflowEdgeOut,
    WorkflowNodeOut,
    WorkflowPlanOutput,
    WorkflowReconcileOutput,
)


class WorkflowValidationError(Exception):
    """A structural defect in a model-produced plan or patch."""


def known_tool_names(tool_inventory: list[dict[str, Any]]) -> set[str] | None:
    """Extract tool names from the client inventory; ``None`` disables grounding
    (the client did not supply an inventory, so nothing can be checked)."""
    if not tool_inventory:
        return None
    names = {
        name
        for item in tool_inventory
        if isinstance(name := item.get("name"), str) and name
    }
    return names or None


def _check_unique_ids(nodes: list[WorkflowNodeOut], existing: set[str]) -> set[str]:
    ids: set[str] = set()
    for node in nodes:
        if node.id in ids or node.id in existing:
            raise WorkflowValidationError(f"duplicate node id {node.id!r}")
        ids.add(node.id)
    return ids


def _check_acyclic(deps: dict[str, set[str]]) -> None:
    """DFS cycle detection over a node→dependencies adjacency map."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(deps, WHITE)
    for start in deps:
        if color[start] != WHITE:
            continue
        stack: list[tuple[str, list[str]]] = [(start, list(deps[start]))]
        color[start] = GRAY
        while stack:
            node, pending = stack[-1]
            if not pending:
                color[node] = BLACK
                stack.pop()
                continue
            nxt = pending.pop()
            if nxt not in deps:
                continue
            if color[nxt] == GRAY:
                raise WorkflowValidationError(f"dependency cycle through node {nxt!r}")
            if color[nxt] == WHITE:
                color[nxt] = GRAY
                stack.append((nxt, list(deps[nxt])))


def _combined_dependencies(
    nodes: list[WorkflowNodeOut],
    edges: list[WorkflowEdgeOut],
    existing_deps: dict[str, set[str]] | None = None,
) -> dict[str, set[str]]:
    """Merge ``depends_on`` lists and explicit edges (edge source → target reads
    "target depends on source") into one adjacency map."""
    deps: dict[str, set[str]] = {k: set(v) for k, v in (existing_deps or {}).items()}
    for node in nodes:
        deps.setdefault(node.id, set()).update(node.depends_on)
    for edge in edges:
        deps.setdefault(edge.target, set()).add(edge.source)
        deps.setdefault(edge.source, set())
    return deps


def _check_references(
    nodes: list[WorkflowNodeOut], edges: list[WorkflowEdgeOut], known: set[str]
) -> None:
    for node in nodes:
        for dep in node.depends_on:
            if dep not in known:
                raise WorkflowValidationError(
                    f"node {node.id!r} depends on unknown node {dep!r}"
                )
    for edge in edges:
        if edge.source not in known or edge.target not in known:
            raise WorkflowValidationError(
                f"edge {edge.source!r} -> {edge.target!r} references an unknown node"
            )


def _ground_tool_names(
    *,
    nodes: list[WorkflowNodeOut],
    next_action: RecommendedActionOut | None,
    tools: set[str] | None,
    warnings: list[str],
) -> tuple[RecommendedActionOut | None, list[str]]:
    """Apply the degrade-and-warn tool policy; returns (next_action, warnings)."""
    if tools is None:
        return next_action, warnings
    out_warnings = list(warnings)
    for node in nodes:
        if node.tool_name and node.tool_name not in tools:
            out_warnings.append(
                f"node {node.id!r} references tool {node.tool_name!r} "
                "not present in the tool inventory"
            )
    if next_action is not None and next_action.tool_name and next_action.tool_name not in tools:
        out_warnings.append(
            f"next_action referenced unknown tool {next_action.tool_name!r}; "
            "dropped — the capability is not in the tool inventory"
        )
        next_action = None
    return next_action, out_warnings[:10]


def validate_plan(
    plan: WorkflowPlanOutput, *, tool_inventory: list[dict[str, Any]]
) -> WorkflowPlanOutput:
    """Structurally validate a fresh plan and ground its tool references."""
    node_ids = _check_unique_ids(plan.nodes, existing=set())
    _check_references(plan.nodes, plan.edges, node_ids)
    _check_acyclic(_combined_dependencies(plan.nodes, plan.edges))

    next_action, warnings = _ground_tool_names(
        nodes=plan.nodes,
        next_action=plan.next_action,
        tools=known_tool_names(tool_inventory),
        warnings=plan.warnings,
    )
    if next_action is not plan.next_action or warnings != plan.warnings:
        plan = plan.model_copy(update={"next_action": next_action, "warnings": warnings})
    return plan


def _existing_graph(workflow: dict[str, Any]) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Leniently read node ids/statuses/dependencies out of the client's workflow
    snapshot (an untrusted dict). Unparseable entries are skipped: the client is
    the source of truth and re-validates the patch against its real graph."""
    statuses: dict[str, str] = {}
    deps: dict[str, set[str]] = {}
    nodes = workflow.get("nodes")
    if not isinstance(nodes, list):
        return statuses, deps
    for raw in nodes:
        if not isinstance(raw, dict):
            continue
        node_id = raw.get("id")
        if not isinstance(node_id, str) or not node_id:
            continue
        status = raw.get("status")
        statuses[node_id] = status if isinstance(status, str) else ""
        depends = raw.get("depends_on")
        if isinstance(depends, list):
            deps[node_id] = {d for d in depends if isinstance(d, str)}
        else:
            deps[node_id] = set()
    return statuses, deps


def validate_reconcile(
    out: WorkflowReconcileOutput,
    *,
    workflow: dict[str, Any],
    tool_inventory: list[dict[str, Any]],
) -> WorkflowReconcileOutput:
    """Structurally validate a reconcile patch against the client's snapshot."""
    patch = out.patch
    statuses, existing_deps = _existing_graph(workflow)
    have_graph = bool(statuses)

    added_ids = _check_unique_ids(patch.add_nodes, existing=set(statuses))
    known = set(statuses) | added_ids

    if have_graph:
        for update in patch.node_updates:
            if update.node_id not in known:
                raise WorkflowValidationError(
                    f"node_updates references unknown node {update.node_id!r}"
                )
            current = statuses.get(update.node_id)
            if (
                update.status is not None
                and current in TERMINAL_NODE_STATUSES
                and update.status != current
            ):
                raise WorkflowValidationError(
                    f"node {update.node_id!r} is already {current!r} (final) and "
                    f"cannot move to {update.status!r}"
                )
        _check_references(patch.add_nodes, patch.add_edges, known)

    # A cycle among the patch's OWN add_nodes/add_edges needs no existing graph to
    # detect, so this check runs unconditionally — unlike _check_references, which
    # WOULD false-reject a legitimate reference into a snapshot we simply could not
    # read. `existing_deps` is empty in that case, so only patch-internal edges count.
    _check_acyclic(_combined_dependencies(patch.add_nodes, patch.add_edges, existing_deps))

    next_action, warnings = _ground_tool_names(
        nodes=patch.add_nodes,
        next_action=out.next_action,
        tools=known_tool_names(tool_inventory),
        warnings=out.warnings,
    )
    if next_action is not out.next_action or warnings != out.warnings:
        out = out.model_copy(update={"next_action": next_action, "warnings": warnings})
    return out
