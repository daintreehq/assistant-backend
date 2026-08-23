"""The prompt-cache invariant, asserted on BYTES.

DeepSeek serializes [all system messages] -> [tools] -> [non-system messages], so
a volatile value anywhere in the stable region lands before the ~21.5k-token tool
schemas and busts their cache (measured: 36% hit on byte-identical turns -> 99%
after the fix). That invariant was covered only by ORDERING assertions, so a
refactor that folded a per-turn value into a stable block kept every test green.

Only the base prompt and the response contract are system-role now: "stable" means
stable for EVERY caller, not merely within one session, so the skill bodies and the
integration surface ride user-role rows below the schemas (issue #39).

These tests pin it directly: two requests differing ONLY in volatile runtime/turn
state must produce a byte-identical stable prefix, with all the variation confined
to the single trailing user-role tail.

The same invariant recurs one level DOWN, inside a single message string: the
selector's system prompt is the whole catalog rendered as one message — tens of
KB — and a per-request value spliced into its opening lines forks the prefix for
everything after it just as surely as a volatile system message does. Prefix
caching matches from token 0 with no suffix matching and no realignment, so the
rule is identical in both places — per-request state rides at the END.
"""

from __future__ import annotations

import json
import re
from typing import Any

from conftest import respond_body

from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.services.prompt_assembler import (
    PromptAssembler,
    PromptSegment,
    segment_log_fields,
)
from daintree_assistant_server.skills.models import Skill
from daintree_assistant_server.skills.tool_projection import tools_bytes

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
    # ...and the system region is EXACTLY the two fully-static blocks. The ordering
    # check above passes just as happily with the skill or integrations block back in
    # the system region, since they precede the conversation either way — this is the
    # assertion that actually pins them below the schemas (issue #39).
    assert roles.count("system") == 2
    assert str(built[1].get("content", "")).startswith("# Response contract")
    # ...and the stable startup block sits between the systems and the conversation.
    startup = next(
        i for i, m in enumerate(built) if str(m.get("content", "")).startswith("[Injected startup")
    )
    convo = next(i for i, m in enumerate(built) if m.get("content") == "start the work")
    assert last_system < startup < convo


def test_tool_downstream_blocks_follow_cache_scope_order(assembler, catalog) -> None:
    """Issue #39: the rows below the schemas are ordered by how widely each is shared.

    The tool schemas serialize immediately after the system region, so everything
    below them is ordered widest-shared first — skill bodies (shared by TASK, so any
    two sessions running the same runbook share the prefix through it), then
    integrations and startup (per session, instance and project). Reversing that pair
    still yields "user-role and before the conversation", so a naive layout test would
    not notice, while the cross-session prefix it buys would quietly disappear.
    """
    built = _build(
        assembler,
        _request(
            runtime={"permission_tier": "operator", "mcp_servers": [{"name": "daintree"}]},
            tools=[{"type": "function", "function": {"name": "git__status", "description": "d"}}],
        ),
        [catalog.skills[0]],
    )
    msgs = _messages(built)
    # The schemas really are present, so "below the schemas" means something here.
    assert built.tools

    def _find(needle: str) -> int:
        idx = next((i for i, m in enumerate(msgs) if needle in str(m.get("content", ""))), None)
        assert idx is not None, (
            f"no message contains {needle!r} — the block is missing from the assembled "
            f"array. Roles present: {[m['role'] for m in msgs]}"
        )
        return idx

    contract = _find("# Response contract")
    # Match the injected HEADERS, not the body headings: "# Integrations" also
    # appears in base-prompt prose about not reciting these blocks.
    skill = _find("[Injected skill context")
    integrations = _find("[Injected integrations context")
    startup = _find("[Injected startup context")
    convo = next(i for i, m in enumerate(msgs) if m.get("content") == "start the work")

    assert [i for i, m in enumerate(msgs) if m["role"] == "system"] == [0, contract]
    assert contract < skill < integrations < startup < convo
    assert (msgs[skill]["role"], msgs[integrations]["role"], msgs[startup]["role"]) == (
        "user",
        "user",
        "user",
    )


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


def test_identical_inputs_are_byte_identical(assembler, catalog) -> None:
    skills = [catalog.skills[0]]
    a = _messages(_build(assembler, _request(), skills))
    b = _messages(_build(assembler, _request(), skills))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True), (
        "the same input rendered differently twice — an unstable iteration order or "
        "timestamp is in the prompt"
    )


# ---------------------------------------------------------------------------
# The prompt-segment manifest. The layout above is gated deterministically, but
# in production the assembled array is opaque: only the tools region's bytes were
# ever logged, so a cache ratio that dropped could be attributed to nothing. The
# manifest is what turns "the cache seems worse" into a named layer.
# ---------------------------------------------------------------------------

_SEGMENT_IDS = frozenset(
    {
        "base",
        "response_contract",
        "tools",
        "skills",
        "integrations",
        "startup",
        "conversation",
        "tail",
    }
)


def _segments(assembler: PromptAssembler, request: RespondRequest, skills: list[Skill]) -> Any:
    return assembler.build_main_request_with_segments(
        request=request, skills=skills, upstream_model="deepseek-v4-flash"
    )[1]


def _digest(segments: Any, name: str) -> str:
    return next(s.sha256 for s in segments if s.id == name)


def test_the_manifest_emits_only_labels_counts_and_digests(assembler, catalog) -> None:
    """The whole point of a digest is describing a prompt without carrying one.

    Checked structurally rather than by searching the rendered text for each
    field: the ids are ordinary words ("base", "skills") that occur in the prose
    anyway, so a substring search would fail on a manifest that was perfectly
    content-free. What has to hold is that every field is a fixed label, an int,
    or a hex digest — nothing derived from the text except through a hash.
    """
    segments = _segments(assembler, _request(), [catalog.skills[0]])
    fields = segment_log_fields(segments)

    assert set(fields) == {"prompt_segments", "prompt_bytes"}
    assert isinstance(fields["prompt_bytes"], int)
    for segment_id, entry in fields["prompt_segments"].items():
        assert segment_id in _SEGMENT_IDS
        assert set(entry) == {"sha256", "bytes"}
        assert isinstance(entry["bytes"], int)
        assert re.fullmatch(r"[0-9a-f]{0,12}", entry["sha256"])


def test_no_canary_from_any_layer_survives_into_the_log_fields(assembler, catalog) -> None:
    """A distinctive string in every caller-supplied carrier, including the tools
    array the ORCHESTRATOR contributes — which the assembler never sees, and
    which nothing else here would cover."""
    canaries = {
        "startup": "CANARY-project-instructions-8f21",
        "turn": "CANARY-turn-goal-4b7c",
        "tools": "CANARY-tool-description-99ad",
    }
    request = RespondRequest.model_validate(
        respond_body(
            _MESSAGES,
            startup={"project_instructions": canaries["startup"]},
            turn={"goal": canaries["turn"]},
        )
    )
    segments = list(_segments(assembler, request, [catalog.skills[0]]))
    segments = [
        PromptSegment.of_wire(
            "tools",
            [{"type": "function", "function": {"name": "t", "description": canaries["tools"]}}],
        )
        if s.id == "tools"
        else s
        for s in segments
    ]

    flattened = json.dumps(segment_log_fields(segments))
    for name, canary in canaries.items():
        assert canary not in flattened, f"{name} leaked into the manifest"


def test_the_manifest_follows_the_assembled_array(assembler, catalog) -> None:
    """Order is the diagnostic. Reading down the list for the first changed
    digest is only meaningful if the list is in wire order, widest scope first —
    and the tools row belongs after the system messages, where the chat template
    serializes it."""
    skills = [catalog.skills[0]]
    built, segments = assembler.build_main_request_with_segments(
        request=_request(), skills=skills, upstream_model="deepseek-v4-flash"
    )
    ids = [s.id for s in segments]
    assert ids[:3] == ["base", "response_contract", "tools"]
    assert ids.index("skills") < ids.index("startup") < ids.index("conversation")
    assert ids[-1] == "tail"
    # One entry per LAYER, not per message: the conversation collapses to one
    # row, and `tools` is not a message at all.
    assert len(segments) == len(built.messages) - len(_MESSAGES) + 2


def test_the_tools_slot_is_left_empty_for_the_orchestrator(assembler, catalog) -> None:
    """The assembler's tools are PRE-projection and an enforced turn replaces
    them, so a digest taken here would describe an array nothing was asked to
    accept. It states the POSITION and nothing else."""
    segments = _segments(assembler, _request(), [])
    tools = next(s for s in segments if s.id == "tools")
    assert (tools.bytes, tools.sha256) == (0, "")


def test_each_segment_is_measured_over_the_bytes_that_go_upstream(
    assembler, catalog
) -> None:
    """Not over the `content` string.

    Content-only bytes would make `prompt_bytes` a sum of two different units —
    prose lengths plus JSON lengths — and would disagree with `tools_bytes` on
    the same tools array. The encoding is the provider's: compact separators,
    no key sorting, non-ASCII kept.
    """
    built, segments = assembler.build_main_request_with_segments(
        request=_request(), skills=[], upstream_model="deepseek-v4-flash"
    )
    expected = json.dumps(
        built.messages[0], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    base = next(s for s in segments if s.id == "base")
    assert base.bytes == len(expected)

    tools = [{"type": "function", "function": {"name": "t"}}]
    assert PromptSegment.of_wire("tools", tools).bytes == tools_bytes(tools)


def test_a_digest_is_not_canonicalized_away(assembler, catalog) -> None:
    """`sort_keys=True` is the obvious thing to do to a digest and the wrong
    thing to do to this one: prefix caching matches literal bytes, so two tool
    schemas with the same keys in different insertion order are a cache MISS,
    and a sorted digest would call them identical."""
    a = [{"type": "function", "function": {"name": "t", "description": "d"}}]
    b = [{"function": {"description": "d", "name": "t"}, "type": "function"}]
    assert PromptSegment.of_wire("tools", a).sha256 != PromptSegment.of_wire("tools", b).sha256


def test_a_cjk_conversation_is_not_measured_as_escape_sequences(assembler, catalog) -> None:
    """`json.dumps` defaults to `ensure_ascii=True`, which renders one CJK
    character as six. Asserted as an exact length rather than a ceiling: a loose
    bound passes on a short sample even with the escaping still in place."""
    text = "作業を開始してください"
    japanese = [{"role": "user", "content": text}]
    request = RespondRequest.model_validate(respond_body(japanese))
    segments = _segments(assembler, request, [])
    conversation = next(s for s in segments if s.id == "conversation")

    expected = json.dumps(
        [{"role": "user", "content": text}], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    assert conversation.bytes == len(expected)
    escaped = json.dumps([{"role": "user", "content": text}], separators=(",", ":"))
    assert conversation.bytes < len(escaped.encode("utf-8"))


def test_only_the_changed_layer_changes_its_digest(assembler, catalog) -> None:
    """The property the whole manifest exists for.

    A volatile-state change must move the tail's digest and NOTHING above it —
    the same claim `test_volatile_state_changes_only_the_trailing_tail` makes
    about bytes, restated as the thing a production log can check across two
    consecutive turns.
    """
    narrow = _segments(assembler, _request(runtime={"display": {"content_width": 40}}), [])
    wide = _segments(assembler, _request(runtime={"display": {"content_width": 100}}), [])

    assert [s.id for s in narrow] == [s.id for s in wide]
    changed = [n.id for n, w in zip(narrow, wide, strict=True) if n.sha256 != w.sha256]
    assert changed == ["tail"]


def test_an_appended_turn_changes_only_the_conversation_and_the_tail(
    assembler, catalog
) -> None:
    """The commonest transition there is, and the one a cache regression hides
    behind: everything above the conversation must hold."""
    longer = [*_MESSAGES, {"role": "assistant", "content": "on it"}]
    before = _segments(assembler, _request(), [])
    after = _segments(
        assembler, RespondRequest.model_validate(respond_body(longer)), []
    )
    changed = [n.id for n, w in zip(before, after, strict=True) if n.sha256 != w.sha256]
    assert changed == ["conversation"]


def test_a_skill_topic_shift_leaves_the_system_layers_untouched(assembler, catalog) -> None:
    """Issue #39's guarantee, expressed in the manifest a production log reads.

    Everything from the skills row down re-encodes; base and the response
    contract must not, because they sit above the ~21.5k tokens of schemas.
    """
    one = _segments(assembler, _request(), [catalog.skills[0]])
    two = _segments(assembler, _request(), [catalog.skills[1]])

    assert _digest(one, "base") == _digest(two, "base")
    assert _digest(one, "response_contract") == _digest(two, "response_contract")
    assert _digest(one, "skills") != _digest(two, "skills")


def test_an_optional_layer_appearing_renames_nothing_below_it(assembler, catalog) -> None:
    """The reason the ids are layer names and not message indices.

    A manifest keyed by position renames every row beneath an optional one the
    moment it appears, so two turns differing in ONE layer compare as though
    they differ in all of them — which is the opposite of an attribution.
    """
    without = _segments(assembler, _request(), [])
    with_skills = _segments(assembler, _request(), [catalog.skills[0]])

    assert "skills" not in {s.id for s in without}
    assert "skills" in {s.id for s in with_skills}
    for name in ("base", "response_contract", "startup", "conversation"):
        assert _digest(without, name) == _digest(with_skills, name)


def _subagent_segments(assembler: PromptAssembler, skills: list[Skill]) -> Any:
    body = respond_body(_MESSAGES)
    body["profile"] = "subagent"
    return assembler.build_main_request_with_segments(
        request=RespondRequest.model_validate(body),
        skills=skills,
        upstream_model="deepseek-v4-flash",
    )


def test_the_subagent_manifest_uses_the_same_layer_names(assembler, catalog) -> None:
    """It drops the integrations, runtime, footer, display and response-contract
    rows entirely, so it names fewer layers — but the ones it names are the same
    ones, in the same order. `profile` is already on the log record, so nothing
    needs a prefix to stay distinguishable."""
    built, segments = _subagent_segments(assembler, [])
    ids = [s.id for s in segments]
    assert ids[:2] == ["base", "tools"]
    assert set(ids) <= _SEGMENT_IDS
    assert not {"tail", "integrations", "response_contract"} & set(ids)
    assert len(segments) == len(built.messages) - len(_MESSAGES) + 2


def test_the_subagent_manifest_survives_a_shape_change(assembler, catalog) -> None:
    """One segment per indexed MESSAGE would make this comparison useless, and
    on a long transcript would emit one row per message — up to `cap_messages`
    of them — inside a single log record."""
    executable = list(catalog.skills[:1])
    _, without = _subagent_segments(assembler, [])
    _, with_skills = _subagent_segments(assembler, executable)

    assert _digest(without, "base") == _digest(with_skills, "base")
    assert _digest(without, "conversation") == _digest(with_skills, "conversation")
