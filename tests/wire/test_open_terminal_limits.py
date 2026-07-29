"""Pin the OpenTerminal field limits the Go CLI mirrors.

``cli/internal/backend/contracts.go`` hardcodes these as Go constants with the
comment "These MUST mirror the backend OpenTerminal pydantic max_length
constraints", and the CLI clamps to them before sending. Nothing on THIS side
pinned them, so tightening one by a single character would 422 real turns from a
client that believes it already clamped correctly.

The backend is the authority; this test makes any change to it a deliberate,
visible edit that names the Go file to update in lockstep.
"""

from __future__ import annotations

from daintree_assistant_server.contracts.extensions import OpenTerminal

# Mirror of `openTerminal*Max` in cli/internal/backend/contracts.go.
# Changing a value here REQUIRES the matching Go constant to change too.
EXPECTED_MAX_LENGTHS = {
    "id": 256,
    "kind": 64,
    "worktree_id": 4096,
    "title": 512,
    "agent_id": 256,
    "agent_state": 64,
    "waiting_reason": 512,
}


def _max_length(field_name: str) -> int | None:
    field = OpenTerminal.model_fields[field_name]
    for meta in field.metadata:
        value = getattr(meta, "max_length", None)
        if value is not None:
            return int(value)
    return None


def test_open_terminal_max_lengths_match_the_cli_constants() -> None:
    actual = {name: _max_length(name) for name in EXPECTED_MAX_LENGTHS}
    assert actual == EXPECTED_MAX_LENGTHS, (
        "OpenTerminal field limits changed. The Go CLI hardcodes these as "
        "openTerminal*Max in cli/internal/backend/contracts.go and clamps to them "
        "before sending — update BOTH sides in lockstep, or a clamped-but-too-long "
        "field will 422 the whole turn."
    )


def test_every_bounded_open_terminal_field_is_pinned() -> None:
    """A NEW bounded field must be added here (and mirrored in Go) deliberately."""
    bounded = {
        name for name in OpenTerminal.model_fields if _max_length(name) is not None
    }
    assert bounded == set(EXPECTED_MAX_LENGTHS), (
        f"unpinned bounded field(s): {bounded ^ set(EXPECTED_MAX_LENGTHS)} — add them "
        "here and mirror the limit in cli/internal/backend/contracts.go"
    )
