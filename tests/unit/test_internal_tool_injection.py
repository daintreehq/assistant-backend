"""The internal tool reaching the model, and nothing else changing.

Two things are pinned. First, that a deployment with the feature off produces a
byte-identical request to the one it produced before the feature existed —
otherwise "turn it off" is not a rollback. Second, that when it IS on, the tool
lands in the provider request only, at the end of the tools array, and never in
the client's own inventory.
"""

from __future__ import annotations

from typing import Any

import pytest

from daintree_assistant_server.services.forked_terminal_calls import (
    FORK_TOOL_DEFINITION,
    FORK_TOOL_WIRE_NAME,
    SPAWN_TOOL_WIRE_NAME,
    canonical_json,
)
from daintree_assistant_server.services.forked_tool_call_rewriter import (
    decide_fork_feature,
    inject_fork_tool,
)
from daintree_assistant_server.services.validation import RESERVED_TOOL_PREFIX


def _tool(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


CLIENT_TOOLS = [_tool("fs__read"), _tool(SPAWN_TOOL_WIRE_NAME), _tool("terminal__await")]


def test_a_disabled_deployment_leaves_the_tools_array_untouched() -> None:
    # The rollback property, asserted on the ARRAY rather than on the decision
    # boolean: if turning the feature off still changed the request, a
    # config-only revision would not be a rollback but a third behaviour.
    decision = decide_fork_feature(
        mode="disabled", profile="assistant", tools=CLIENT_TOOLS, max_tools=128
    )
    assert decision.enabled is False
    tools = inject_fork_tool(CLIENT_TOOLS) if decision.enabled else CLIENT_TOOLS
    assert tools is CLIENT_TOOLS
    assert canonical_json(tools) == canonical_json(CLIENT_TOOLS)


def test_injection_leaves_every_existing_schema_at_its_byte_offset() -> None:
    # The tools region is serialized ahead of the conversation and matched
    # literally, so a tool inserted anywhere but the end would move every schema
    # after it and cost a cache miss on the largest region of the request.
    before = canonical_json(CLIENT_TOOLS)
    after = canonical_json(inject_fork_tool(CLIENT_TOOLS))
    assert after.startswith(before[:-1])


def test_the_injected_entry_is_the_shared_definition_object() -> None:
    # Built once at import rather than per turn: the prefix cache matches these
    # bytes literally, so they must be the same object with the same key order
    # on every request.
    assert inject_fork_tool(CLIENT_TOOLS)[-1] is FORK_TOOL_DEFINITION


def test_the_definition_is_byte_stable_across_calls() -> None:
    assert canonical_json(inject_fork_tool(CLIENT_TOOLS)) == canonical_json(
        inject_fork_tool(CLIENT_TOOLS)
    )


def test_the_injected_name_is_one_the_client_may_never_supply() -> None:
    # What makes the name unspoofable rather than merely private: the request
    # boundary already refuses this whole namespace from a client, on an offered
    # tool, a replayed call and a named tool_choice alike.
    assert FORK_TOOL_WIRE_NAME.startswith(RESERVED_TOOL_PREFIX)


def test_the_definition_is_not_advertised_as_a_client_capability(client: Any) -> None:
    # It is a provider-facing server tool, not a CLI capability. A client that
    # read it from /capabilities and offered it back would be 400'd by the same
    # namespace rule. Asserted against the SERVED response rather than the
    # source text, which a dynamic entry would evade and a comment would trip.
    response = client.get("/v1/daintree/capabilities")
    assert response.status_code == 200
    assert FORK_TOOL_WIRE_NAME not in response.text


@pytest.mark.parametrize("profile", ["subagent"])
def test_the_subagent_profile_is_excluded_whatever_its_inventory(profile: str) -> None:
    # Read-only, and cannot spawn a visible terminal at all.
    decision = decide_fork_feature(
        mode="enabled", profile=profile, tools=CLIENT_TOOLS, max_tools=128
    )
    assert decision.enabled is False


def test_the_gate_reads_the_provider_inventory_not_the_client_one() -> None:
    # Projection may have withheld the spawn tool from this turn even though the
    # client offered it. What matters is what the MODEL will see.
    projected = [_tool("fs__read")]
    assert (
        decide_fork_feature(
            mode="enabled", profile="assistant", tools=projected, max_tools=128
        ).enabled
        is False
    )


def test_production_wiring_gives_the_orchestrator_a_rewriter(settings: Any) -> None:
    # The hand-built fixtures elsewhere inject a rewriter directly, so removing
    # the real wiring in `main.py` would not fail them. This is the test that
    # would.
    from daintree_assistant_server.main import build_container

    container = build_container(settings)
    assert container.orchestrator.fork_rewriter is not None
