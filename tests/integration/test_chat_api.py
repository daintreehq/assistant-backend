"""Integration tests for /v1/daintree/respond and the skill-orchestration
scenarios from specification section 19.5 (native protocol)."""

from __future__ import annotations

import json

import pytest
from conftest import meta_of, parse_sse, respond_body
from fakes import FakeProvider, make_result, make_text_chunks, selection_result

from daintree_assistant_server.errors import upstream_error

URL = "/v1/daintree/respond"
SKILL_PREP = "daintree.workflow.prep-branch-for-review"
SKILL_RENAME = "daintree.terminals.rename"


def _main_request(provider: FakeProvider):
    return provider.requests[-1]


def _loaded_skills_message(provider: FakeProvider) -> str:
    # Skill bodies now follow the conversation (cache layout), so find by content, not
    # position. Match "## Skill 1:" — the base prompt mentions "# Loaded skills" in prose
    # (the anti-recitation rule), so that header alone no longer uniquely marks the block.
    return next(
        m["content"]
        for m in _main_request(provider).messages
        if "## Skill 1:" in (m.get("content") or "")
    )


def test_scenario1_new_session_skill_selected(client, auth, fake_provider, app) -> None:
    fake_provider.selection_result = selection_result([SKILL_PREP])
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "prep this branch for review"}],
        session_id="s1", turn_id="t1",
    ))
    assert r.status_code == 200
    meta = meta_of(r.json())
    assert [s["id"] for s in meta["active_skills"]] == [SKILL_PREP]
    assert [s["id"] for s in meta["newly_loaded_skills"]] == [SKILL_PREP]
    assert len(meta["prelude"]["tool_executions"]) == 1
    assert meta["prelude"]["tool_executions"][0]["display_name"] == "skill.load"
    upstream = _main_request(fake_provider)
    # The CLI sees a skill.load prelude (above), but the model must NOT — skills
    # reach it as plain context, with no synthetic skill__load call to imitate.
    assert not any(
        m.get("tool_calls") and m["tool_calls"][0]["function"]["name"] == "skill__load"
        for m in upstream.messages
    )
    skill = app.state.container.catalog.get(SKILL_PREP)
    assert skill.title in _loaded_skills_message(fake_provider)


def test_scenario2_no_relevant_skill_no_prelude(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "hello"}], session_id="s2", turn_id="t1",
    ))
    meta = meta_of(r.json())
    assert meta["active_skills"] == []
    assert meta["prelude"]["tool_executions"] == []


def test_scenario3_same_turn_round_does_not_rerun(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([SKILL_PREP])
    r1 = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "prep branch"}], session_id="s3", turn_id="t1", round=0,
    ))
    state = r1.json()["state"]
    fake_provider.selection_requests.clear()

    r2 = client.post(URL, headers=auth, json=respond_body(
        [
            {"role": "user", "content": "prep branch"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "git__status", "arguments": "{}"}}
            ]},
            {"role": "tool", "content": "done", "tool_call_id": "c1"},
        ],
        session_id="s3", turn_id="t1", round=1, state=state,
    ))
    meta = meta_of(r2.json())
    assert len(fake_provider.selection_requests) == 0  # selector NOT rerun
    assert meta["selector"]["ran"] is False
    assert [s["id"] for s in meta["active_skills"]] == [SKILL_PREP]
    assert meta["newly_loaded_skills"] == []


def test_scenario4_new_turn_reruns(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([SKILL_PREP])
    r1 = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "prep branch"}], session_id="s4", turn_id="t1",
    ))
    state = r1.json()["state"]
    fake_provider.selection_requests.clear()
    fake_provider.selection_result = selection_result([SKILL_RENAME])

    r2 = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "rename terminals"}], session_id="s4", turn_id="t2", state=state,
    ))
    meta = meta_of(r2.json())
    assert len(fake_provider.selection_requests) == 1
    assert meta["selector"]["ran"] is True
    assert SKILL_RENAME in [s["id"] for s in meta["active_skills"]]


def test_scenario5_interjection_reruns_once(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([SKILL_PREP])
    r1 = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "prep branch"}], session_id="s5", turn_id="t1", instruction_revision=0,
    ))
    state = r1.json()["state"]
    fake_provider.selection_requests.clear()

    r2 = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "actually rename terminals"}],
        session_id="s5", turn_id="t1", instruction_revision=1, state=state,
    ))
    assert len(fake_provider.selection_requests) == 1
    assert meta_of(r2.json())["selector"]["ran"] is True


def test_scenario6_selector_run_caps_at_three(client, auth, fake_provider, app) -> None:
    # A single authoritative selector run is capped: 4 returned ids → the first 3 load.
    ids = sorted(app.state.container.catalog.known_ids)
    fake_provider.selection_result = selection_result([ids[0], ids[1], ids[2], ids[3]])
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "a big multi-workflow task"}], session_id="s6", turn_id="t1",
    ))
    meta = meta_of(r.json())
    active = [s["id"] for s in meta["active_skills"]]
    assert active == [ids[0], ids[1], ids[2]]
    assert ids[3] not in active


def test_scenario6b_new_instruction_replaces_prior(client, auth, fake_provider, app) -> None:
    # A successful selector run on a NEW turn is authoritative: it REPLACES the prior
    # active set (topic shift), rather than accumulating additively.
    ids = sorted(app.state.container.catalog.known_ids)
    fake_provider.selection_result = selection_result([ids[0]])
    r1 = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "task a"}], session_id="s6b", turn_id="t1",
    ))
    state = r1.json()["state"]
    fake_provider.selection_requests.clear()
    fake_provider.selection_result = selection_result([ids[1]])
    r2 = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "a totally different task b"}],
        session_id="s6b", turn_id="t2", state=state,
    ))
    meta = meta_of(r2.json())
    active = [s["id"] for s in meta["active_skills"]]
    assert active == [ids[1]]  # prior ids[0] evicted — authoritative replacement
    assert ids[0] not in active
    assert [s["id"] for s in meta["newly_loaded_skills"]] == [ids[1]]


def test_scenario7_unknown_and_duplicate_ids(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([SKILL_PREP, "nonexistent", SKILL_PREP])
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "prep"}], session_id="s7", turn_id="t1",
    ))
    meta = meta_of(r.json())
    assert [s["id"] for s in meta["active_skills"]] == [SKILL_PREP]


def test_scenario8_selector_fails_retains_prior(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([SKILL_PREP])
    r1 = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "prep"}], session_id="s8", turn_id="t1",
    ))
    state = r1.json()["state"]
    fake_provider.selection_error = upstream_error("selector down")

    r2 = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "next task"}], session_id="s8", turn_id="t2", state=state,
    ))
    assert r2.status_code == 200
    meta = meta_of(r2.json())
    assert meta["selector"]["degraded"] is True
    assert "skill_selector_failed_open" in meta["warnings"]
    assert [s["id"] for s in meta["active_skills"]] == [SKILL_PREP]


def test_scenario9_catalog_revision_change_reruns(client, auth, fake_provider, app) -> None:
    codec = app.state.container.state_codec
    stale = codec.issue(
        session_id="s9", active_skills=[SKILL_PREP],
        catalog_revision="sha256:stale", turn_id="t1", instruction_revision=0,
    )
    fake_provider.selection_result = selection_result([SKILL_RENAME])
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "rename"}], session_id="s9", turn_id="t1", state=stale,
    ))
    meta = meta_of(r.json())
    assert meta["selector"]["ran"] is True
    assert meta["catalog_revision"] == app.state.container.catalog.revision


def test_scenario10_replayed_state_other_session(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([SKILL_PREP])
    r1 = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "prep"}], session_id="sessionA", turn_id="t1",
    ))
    state = r1.json()["state"]
    fake_provider.selection_result = selection_result([])
    r2 = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "prep"}], session_id="sessionB", turn_id="t1", state=state,
    ))
    meta = meta_of(r2.json())
    assert "skill_state_session_mismatch" in meta["warnings"]
    assert meta["selector"]["ran"] is True
    assert meta["active_skills"] == []


def test_streaming_meta_event_first_then_deltas(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([SKILL_PREP])
    fake_provider.stream_chunks = make_text_chunks("hello there")
    body = respond_body(
        [{"role": "user", "content": "prep"}], session_id="ss", turn_id="t1",
        generation={"stream": True},
    )
    with client.stream("POST", URL, headers=auth, json=body) as s:
        assert s.status_code == 200
        text = "".join(s.iter_text())
    events = parse_sse(text)
    kinds = [e for e, _ in events]
    assert kinds[0] == "meta"  # metadata before any token
    assert kinds[-1] == "done"
    assert "delta" in kinds
    assert "status" not in kinds  # no thinking-phase status event: the fake stream emits no reasoning_content
    meta = meta_of(events[0][1])
    assert [sk["id"] for sk in meta["active_skills"]] == [SKILL_PREP]
    streamed = "".join(d.get("content", "") for e, d in events if e == "delta")
    assert "hello" in streamed


def test_nonstream_surfaces_reasoning_content(client, auth, fake_provider) -> None:
    # When the upstream returns a thinking-mode chain-of-thought, it is delivered
    # to the client on the assistant message so it can be replayed next turn.
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content="done", reasoning_content="my cot")
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "hi"}], session_id="rc1", turn_id="t1",
    ))
    assert r.status_code == 200
    assert r.json()["message"]["reasoning_content"] == "my cot"


def test_nonstream_omits_reasoning_content_when_absent(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content="done")
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "hi"}], session_id="rc1b", turn_id="t1",
    ))
    assert "reasoning_content" not in r.json()["message"]


def test_stream_surfaces_reasoning_content(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"reasoning_content": "cot "}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "answer"}, "finish_reason": None}]},
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ]
    body = respond_body(
        [{"role": "user", "content": "hi"}], session_id="rc2", turn_id="t1",
        generation={"stream": True},
    )
    with client.stream("POST", URL, headers=auth, json=body) as s:
        text = "".join(s.iter_text())
    events = parse_sse(text)
    kinds = [e for e, _ in events]
    # A status:thinking event fires once, before the first (reasoning) delta.
    assert "status" in kinds
    status_idx = kinds.index("status")
    assert events[status_idx][1]["phase"] == "thinking"
    assert status_idx < kinds.index("delta")
    reasoning = "".join(d.get("reasoning_content", "") for e, d in events if e == "delta")
    content = "".join(d.get("content", "") for e, d in events if e == "delta")
    assert "cot" in reasoning
    assert "answer" in content


def test_missing_session_rejected_422(client, auth) -> None:
    # The native protocol has no stateless mode: session is required by schema.
    r = client.post(URL, headers=auth, json={
        "protocol_version": 1, "input": {"messages": [{"role": "user", "content": "hi"}]},
    })
    assert r.status_code == 400  # RequestValidationError -> invalid_request_error


@pytest.mark.parametrize(
    "messages,tools,code",
    [
        ([{"role": "system", "content": "x"}, {"role": "user", "content": "y"}], None, "system_messages_not_allowed"),
        ([{"role": "user", "content": "x"}], [{"type": "function", "function": {"name": "skill__load"}}], "reserved_tool_name"),
    ],
)
def test_error_envelopes(client, auth, messages, tools, code) -> None:
    r = client.post(URL, headers=auth, json=respond_body(messages, tools=tools))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == code


def test_missing_auth_401(client) -> None:
    r = client.post(URL, json=respond_body([{"role": "user", "content": "x"}]))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


def test_unsupported_protocol_426(client, auth) -> None:
    r = client.post(URL, headers={**auth, "X-Daintree-Protocol": "99"},
                    json=respond_body([{"role": "user", "content": "x"}]))
    assert r.status_code == 426


# ---- tool-call argument repair (schema-guided, server-side) ------------------

# The real terminal.extract shape from the CLI's registry projection: the batch
# field is `terminalIds` (array), while most sibling terminal tools take a
# single `terminalId` string — the near miss the repair layer exists for.
EXTRACT_TOOL = {
    "type": "function",
    "function": {
        "name": "terminal__extract",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["terminalIds"],
            "properties": {
                "instruction": {"type": "string"},
                "terminalIds": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}


def test_nonstream_tool_call_arguments_repaired(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(
        tool_calls=[{
            "id": "call_0", "type": "function", "index": 0,
            "function": {
                "name": "terminal__extract",
                "arguments": '{"terminalId": "terminal-1", "instruction": "vote?"}',
            },
        }],
        finish_reason="tool_calls",
    )
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "read the vote"}], session_id="tr1", turn_id="t1",
        tools=[EXTRACT_TOOL],
    ))
    assert r.status_code == 200
    calls = r.json()["message"]["tool_calls"]
    assert json.loads(calls[0]["function"]["arguments"]) == {
        "terminalIds": ["terminal-1"], "instruction": "vote?",
    }
    assert "index" not in calls[0]  # provider-specific field still dropped


def test_stream_tool_calls_consolidated_and_repaired(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "Reading votes."}, "finish_reason": None}]},
        # call 0 arrives fragmented, with the singular near-miss field.
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_0", "type": "function",
             "function": {"name": "terminal__extract", "arguments": '{"terminalId": "term'}},
        ]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": 'inal-1", "instruction": "vote?"}'}},
        ]}, "finish_reason": None}]},
        # call 1 is already valid — and its opening delta ALSO carries content,
        # which must still stream out immediately (never held with the calls).
        {"choices": [{"index": 0, "delta": {"content": " Now call 1.", "tool_calls": [
            {"index": 1, "id": "call_1", "type": "function",
             "function": {"name": "terminal__extract",
                          "arguments": '{"terminalIds": ["terminal-2"], "instruction": "vote?"}'}},
        ]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
    ]
    body = respond_body(
        [{"role": "user", "content": "read the votes"}], session_id="tr2", turn_id="t1",
        generation={"stream": True}, tools=[EXTRACT_TOOL],
    )
    with client.stream("POST", URL, headers=auth, json=body) as s:
        text = "".join(s.iter_text())
    events = parse_sse(text)
    assert [e for e, _ in events][-1] == "done"
    # Both calls flush together at stream end, in ONE delta placed after every
    # content delta and before done — one consolidated fragment per call.
    groups = [d["tool_calls"] for e, d in events if e == "delta" and d.get("tool_calls")]
    assert len(groups) == 1
    fragments = groups[0]
    assert [f["index"] for f in fragments] == [0, 1]
    assert json.loads(fragments[0]["function"]["arguments"]) == {
        "terminalIds": ["terminal-1"], "instruction": "vote?",
    }
    assert fragments[1]["function"]["arguments"] == (
        '{"terminalIds": ["terminal-2"], "instruction": "vote?"}'
    )
    content = "".join(d.get("content", "") for e, d in events if e == "delta")
    assert content == "Reading votes. Now call 1."
    # The content that rode alongside call 1's fragment streamed BEFORE the calls.
    kinds_with_calls = [
        "calls" if d.get("tool_calls") else "content"
        for e, d in events
        if e == "delta"
    ]
    assert kinds_with_calls[-1] == "calls"


def test_stream_tool_calls_passthrough_without_tools(client, auth, fake_provider) -> None:
    # No input.tools declared -> empty parameter index -> arguments untouched,
    # but calls still arrive consolidated.
    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_0", "type": "function",
             "function": {"name": "terminal__extract",
                          "arguments": '{"terminalId": "terminal-1"}'}},
        ]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
    ]
    body = respond_body(
        [{"role": "user", "content": "read"}], session_id="tr3", turn_id="t1",
        generation={"stream": True},
    )
    with client.stream("POST", URL, headers=auth, json=body) as s:
        text = "".join(s.iter_text())
    events = parse_sse(text)
    fragments = [c for e, d in events if e == "delta" for c in d.get("tool_calls", [])]
    assert len(fragments) == 1
    assert fragments[0]["function"]["arguments"] == '{"terminalId": "terminal-1"}'
