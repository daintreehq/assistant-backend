"""``training.loss_inventory`` splits losses by whether search could have sent the case."""

from __future__ import annotations

from benchmarks.runbook_selection.cases import BenchCase
from daintree_assistant_server.contracts.openai import ChatMessage
from training.loss_inventory import shape


def _case(
    *,
    turns: int = 1,
    active: tuple[str, ...] = (),
    turn_context: str | None = None,
    category: str = "single-runbook",
) -> BenchCase:
    roles = ["user", "assistant"] * turns
    return BenchCase(
        id="c",
        category=category,
        source="",
        messages=[ChatMessage(role=r, content="x") for r in roles[: 2 * turns - 1]],
        active=list(active),
        required=[],
        optional=[],
        turn_context=turn_context,
        available_tools=[],
        note="",
    )


def test_a_lone_message_with_nothing_loaded_is_the_search_shape() -> None:
    assert shape(_case()) == "bare"


def test_the_old_assistants_turns_are_not_search_shaped() -> None:
    assert shape(_case(turns=2)) == "history"
    assert shape(_case(active=("daintree.git.sync-with-base",))) == "active"
    assert shape(_case(turn_context="an agent finished")) == "wake"
    assert shape(_case(category="docs-classification")) == "docs"
