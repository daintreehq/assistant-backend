"""The prompt-cache invariant, asserted on BYTES.

DeepSeek serializes [all system messages] -> [tools] -> [non-system messages], so
a volatile value anywhere in the stable region lands before the ~18k-token tool
schemas and busts their cache (measured: 36% hit on byte-identical turns -> 99%
after the fix). That invariant was covered only by ORDERING assertions, so a
refactor that folded a per-turn value into a stable block kept every test green.

These tests pin it directly: two requests differing ONLY in volatile runtime/turn
state must produce a byte-identical stable prefix, with all the variation confined
to the single trailing user-role tail.

The same invariant recurs one level DOWN, inside a single message string: the
selector's system prompt is ~28 KB of catalog rendered as one message, and a
per-request value spliced into its opening lines forks the prefix hash for
everything after it just as surely as a volatile system message does. Prefix
caching matches from token 0 with no suffix matching and no realignment, so the
rule is identical in both places — per-request state rides at the END.
"""

from __future__ import annotations

import json
from typing import Any

from conftest import respond_body

from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.services.prompt_assembler import PromptAssembler
from daintree_assistant_server.skills.models import Skill

_MESSAGES = [{"role": "user", "content": "start the work"}]


def _request(**kw: Any) -> RespondRequest:
    return RespondRequest.model_validate(respond_body(_MESSAGES, **kw))


def _build(assembler: PromptAssembler, request: RespondRequest, skills: list[Skill]) -> Any:
    return assembler.build_main_request(
        request=request, skills=skills, upstream_model="deepseek-v4-flash"
    )


def _messages(built: Any) -> list[dict[str, Any]]:
    return list(built.messages)


def test_volatile_state_changes_only_the_trailing_tail(assembler, catalog) -> None:
    """Issue #38: the terminal's own width is volatile state like any other.

    The two requests straddle WIDE_TERMINAL_WIDTH, so both the width and every
    value derived from it (narrow/wide, the table-column cap) differ. A resize is
    the common case — the CLI republishes its measured width every round — and
    while the geometry lived in the response contract it re-encoded the whole
    system region, the 77 tool schemas behind it, and the conversation.
    """
    skills = [catalog.skills[0]]
    a = _messages(
        _build(
            assembler,
            _request(runtime={"permission_tier": "operator", "display": {"content_width": 40}}),
            skills,
        )
    )
    b = _messages(
        _build(
            assembler,
            _request(
                runtime={"permission_tier": "operator", "display": {"content_width": 100}},
                turn={"goal": "a completely different objective this turn"},
            ),
            skills,
        )
    )

    # Same shape, and everything except the final tail is byte-identical.
    assert len(a) == len(b), "a volatile field changed the MESSAGE COUNT"
    for i, (left, right) in enumerate(zip(a[:-1], b[:-1], strict=True)):
        assert json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True), (
            f"message {i} ({left.get('role')}) differs between turns — a volatile "
            "value leaked into the cache-stable prefix"
        )
    assert a[-1] != b[-1], "the volatile tail should carry the difference"
    assert a[-1]["role"] == "user"
    # ...and the geometry really is IN that tail, not merely absent from the prefix.
    assert "Usable content width: 40 terminal cells (narrow)." in a[-1]["content"]
    assert "Usable content width: 100 terminal cells (wide)." in b[-1]["content"]


def test_no_system_message_follows_the_conversation(assembler, catalog) -> None:
    built = _messages(_build(assembler, _request(), [catalog.skills[0]]))
    roles = [m["role"] for m in built]
    # Every system message must come first: one placed after the conversation would
    # still be serialized before the tools, silently busting their cache.
    last_system = max(i for i, r in enumerate(roles) if r == "system")
    assert roles[: last_system + 1] == ["system"] * (last_system + 1)
    # ...and the stable startup block sits between the systems and the conversation.
    startup = next(
        i for i, m in enumerate(built) if str(m.get("content", "")).startswith("[Injected startup")
    )
    convo = next(i for i, m in enumerate(built) if m.get("content") == "start the work")
    assert last_system < startup < convo


def test_appending_a_turn_preserves_the_previous_prefix(assembler, catalog) -> None:
    """The visible conversation is APPEND-ONLY, so turn N+1 extends turn N's prefix.

    Both requests carry a runtime block so the tail each strips is the full one. The
    tail is always present now (the display fragment renders unconditionally), so
    slicing off ``[-1]`` can no longer eat the last conversation message — but
    keeping runtime here holds this test on the realistic shape.
    """
    skills = [catalog.skills[0]]
    runtime = {"permission_tier": "operator"}
    first = _messages(_build(assembler, _request(runtime=runtime), skills))
    longer = RespondRequest.model_validate(
        respond_body(
            [
                *_MESSAGES,
                {"role": "assistant", "content": "on it"},
                {"role": "user", "content": "keep going"},
            ],
            runtime=runtime,
        )
    )
    second = _messages(_build(assembler, longer, skills))

    # Both tails are the injected-context block, so stripping one really does leave
    # the full conversation behind.
    for run in (first, second):
        assert run[-1]["role"] == "user"
        assert run[-1]["content"].startswith("[Injected context")

    a, b = first[:-1], second[:-1]
    assert len(b) == len(a) + 2, "the two appended turns should be the only new messages"
    assert a[-1]["content"] == "start the work"  # the prefix really ends in conversation
    for i, left in enumerate(a):
        assert json.dumps(left, sort_keys=True) == json.dumps(b[i], sort_keys=True), (
            f"message {i} changed when a turn was appended — the prefix is not stable"
        )


def test_the_active_set_changes_selector_bytes_only_after_the_catalog(
    selector, catalog_manager
) -> None:  # type: ignore[no-untyped-def]
    """The selector prompt's ONE volatile value must not lead: it must trail.

    ``active_ids`` is the only per-request input to this render, and the catalog it
    would fork is the single largest block on the turn's first-byte path. A fork
    anywhere above the trailing active-set line splits every caller into two
    cohorts that share no prefix worth caching, for whatever wording nicety
    prompted it — so this asserts on BYTES rather than on the one phrasing that
    happened to break it, and fails on ANY future early fork.

    Deliberately rendered through ``_render_system`` against the REAL catalog, so
    it guards the bytes production actually sends.
    """
    catalog = catalog_manager.catalog
    selectable = sorted(catalog.selectable_ids)
    assert selectable, "no selectable skills — the test would prove nothing"

    cold = selector._render_system(catalog, [])
    warm = selector._render_system(catalog, [selectable[0]])

    marker = "\nCurrently active skills: "
    assert cold.count(marker) == warm.count(marker) == 1, (
        "the active-set marker is not unique — a skill's summary/whenToUse now "
        "contains it, so this test would partition the prompt in the wrong place"
    )
    cold_head, _, cold_tail = cold.partition(marker)
    warm_head, _, warm_tail = warm.partition(marker)

    assert cold_head == warm_head, (
        "the active set changed selector prompt bytes BEFORE the trailing tail, "
        "forking the catalog-scale shared prefix that every call depends on"
    )
    # ...and the marker really does sit after the WHOLE catalog, so the assertion
    # above is a guarantee about all ~28 KB of it and not about a short preamble.
    for entry in catalog.selector_metadata():
        assert f"- id: {entry['id']}" in cold_head
    assert cold_tail != warm_tail, "the tail should carry the whole difference"


def test_identical_inputs_are_byte_identical(assembler, catalog) -> None:
    skills = [catalog.skills[0]]
    a = _messages(_build(assembler, _request(), skills))
    b = _messages(_build(assembler, _request(), skills))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True), (
        "the same input rendered differently twice — an unstable iteration order or "
        "timestamp is in the prompt"
    )
