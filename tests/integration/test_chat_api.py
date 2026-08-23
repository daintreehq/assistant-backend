"""Integration tests for /v1/daintree/respond and the runbook-orchestration
scenarios from specification section 19.5 (native protocol)."""

from __future__ import annotations

import json

import pytest
from conftest import meta_of, parse_sse, respond_body
from fakes import FakeProvider, make_result, make_text_chunks, selection_result

from daintree_assistant_server.errors import upstream_error
from daintree_assistant_server.runbooks.decision import SelectionDecision
from daintree_assistant_server.services.chat_orchestrator import resolve_active_runbooks

URL = "/v1/daintree/respond"
RUNBOOK_PREP = "daintree.workflow.prep-branch-for-review"
RUNBOOK_RENAME = "daintree.terminals.rename"


def _paired(app, *ids: str) -> list[str]:
    """The active set the orchestrator produces for these selector picks.

    A successful selector run admits each pick as a specific+domain-foundation UNIT
    (``_admit_with_domain_foundations``). Delegated to the real resolver rather than
    re-implemented here, so these tests survive catalog churn AND can never drift
    from the admission order the orchestrator actually issues.
    """
    container = app.state.container
    return resolve_active_runbooks(
        SelectionDecision(runbook_ids=list(ids), ran=True, degraded=False),
        [],
        container.catalog,
        container.settings.max_active_runbooks,
    ).active


def _main_request(provider: FakeProvider):
    return provider.requests[-1]


def _loaded_runbooks_message(provider: FakeProvider) -> str:
    # Find by content, not a brittle fixed position: runbook bodies lead the injected
    # user-role rows, with optional integrations, the stable startup block and the
    # visible conversation following them (issue #39).
    # Match "## Runbook 1:" because the base prompt mentions "# Loaded runbooks" in prose.
    return next(
        m["content"]
        for m in _main_request(provider).messages
        if "## Runbook 1:" in (m.get("content") or "")
    )


def test_scenario1_new_session_runbook_selected(client, auth, fake_provider, app) -> None:
    fake_provider.selection_result = selection_result([RUNBOOK_PREP])
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "prep this branch for review"}],
        session_id="s1", turn_id="t1",
    ))
    assert r.status_code == 200
    meta = meta_of(r.json())
    assert [s["id"] for s in meta["active_runbooks"]] == _paired(app, RUNBOOK_PREP)
    assert [s["id"] for s in meta["newly_loaded_runbooks"]] == _paired(app, RUNBOOK_PREP)
    # One synthetic runbook.load per newly-loaded runbook (the pick + its foundation).
    assert len(meta["prelude"]["tool_executions"]) == len(meta["newly_loaded_runbooks"])
    assert all(t["display_name"] == "runbook.load" for t in meta["prelude"]["tool_executions"])
    upstream = _main_request(fake_provider)
    # The CLI sees a runbook.load prelude (above), but the model must NOT — runbooks
    # reach it as plain context, with no synthetic runbook__load call to imitate.
    assert not any(
        m.get("tool_calls") and m["tool_calls"][0]["function"]["name"] == "runbook__load"
        for m in upstream.messages
    )
    runbook = app.state.container.catalog.get(RUNBOOK_PREP)
    assert runbook.title in _loaded_runbooks_message(fake_provider)


def test_structured_startup_reaches_main_model_but_not_selector(
    client, auth, fake_provider
) -> None:
    marker = "STARTUP-NOT-A-USER-UTTERANCE-2d91"
    visible = "VISIBLE-SELECTOR-INSTRUCTION-8a42"
    fake_provider.selection_result = selection_result([])
    response = client.post(
        URL,
        headers=auth,
        json=respond_body(
            [{"role": "user", "content": visible}],
            session_id="startup-isolation",
            startup={
                "project": {"id": marker, "name": marker, "path": f"/{marker}"},
                "agent_roster": {
                    "agents": [{"id": marker, "source": "plugin"}],
                    "complete": True,
                    "total_count": 1,
                },
                "project_instructions": marker,
            },
        ),
    )
    assert response.status_code == 200

    # The selector sees the sanitized CONVERSATION and nothing else. It used to
    # be asserted against the rendered selector prompt; selection is local now,
    # so there is no prompt — the evidence is the argument list, which is a
    # stronger place to check it because every selector implementation takes it.
    # EVERY argument selection was given, not just the conversation. Reading one
    # of them would let a future leak through `turn_context` — the other free-text
    # channel into selection — pass unnoticed, and that is the argument a bounded
    # "hint" is most likely to be widened into.
    recorded = fake_provider.selection_requests[-1]
    selector_evidence = json.dumps(
        {
            "conversation": [
                m.model_dump() if hasattr(m, "model_dump") else m for m in recorded.conversation
            ],
            "active_ids": recorded.active_ids,
            "turn_context": recorded.turn_context,
            "available_tools": recorded.available_tools,
        }
    )
    assert visible in selector_evidence
    assert marker not in selector_evidence, (
        "startup context reached the selector — it is required to be blind to it"
    )

    main = _main_request(fake_provider).messages
    startup_idx = next(
        i
        for i, message in enumerate(main)
        if str(message.get("content", "")).startswith("[Injected startup context")
    )
    visible_idx = next(i for i, message in enumerate(main) if message.get("content") == visible)
    assert marker in str(main[startup_idx]["content"])
    assert main[startup_idx]["role"] == "user"
    assert startup_idx < visible_idx


def test_scenario2_no_relevant_runbook_no_prelude(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "rename my terminals"}], session_id="s2", turn_id="t1",
    ))
    meta = meta_of(r.json())
    assert meta["active_runbooks"] == []
    assert meta["prelude"]["tool_executions"] == []


def test_scenario3_same_turn_round_does_not_rerun(client, auth, fake_provider, app) -> None:
    fake_provider.selection_result = selection_result([RUNBOOK_PREP])
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
    assert [s["id"] for s in meta["active_runbooks"]] == _paired(app, RUNBOOK_PREP)
    assert meta["newly_loaded_runbooks"] == []


def test_scenario4_new_turn_reruns(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([RUNBOOK_PREP])
    r1 = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "prep branch"}], session_id="s4", turn_id="t1",
    ))
    state = r1.json()["state"]
    fake_provider.selection_requests.clear()
    fake_provider.selection_result = selection_result([RUNBOOK_RENAME])

    r2 = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "rename terminals"}], session_id="s4", turn_id="t2", state=state,
    ))
    meta = meta_of(r2.json())
    assert len(fake_provider.selection_requests) == 1
    assert meta["selector"]["ran"] is True
    assert RUNBOOK_RENAME in [s["id"] for s in meta["active_runbooks"]]


def test_scenario5_interjection_reruns_once(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([RUNBOOK_PREP])
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
    # A single authoritative selector run is capped: 4 returned ids → 3 runbooks load.
    # What fills those 3 slots is unit admission — the top-ranked pick arrives with
    # its domain foundation, and the MARGINAL specific is what the cap costs.
    catalog = app.state.container.catalog
    ids = sorted(catalog.known_ids)
    fake_provider.selection_result = selection_result([ids[0], ids[1], ids[2], ids[3]])
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "a big multi-workflow task"}], session_id="s6", turn_id="t1",
    ))
    meta = meta_of(r.json())
    active = [s["id"] for s in meta["active_runbooks"]]
    assert len(active) == 3  # the budget is fully spent, never left short
    assert active[0] == ids[0]  # rank order leads
    for sid in active:  # …and every loaded runbook's domain foundation is loaded too
        foundation = catalog.domain_foundation(sid)
        assert foundation is None or foundation in active


def test_scenario6b_new_instruction_replaces_prior(client, auth, fake_provider, app) -> None:
    # A successful selector run on a NEW turn is authoritative: it REPLACES the prior
    # active set (topic shift), rather than accumulating additively.
    catalog = app.state.container.catalog
    # Pick two SPECIFICS: a foundation would be re-added by the pairing step, so it
    # could never be shown as evicted.
    ids = [sid for sid in sorted(catalog.known_ids) if not catalog.get(sid).meta.foundation]
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
    active = [s["id"] for s in meta["active_runbooks"]]
    assert active == _paired(app, ids[1])  # prior ids[0] evicted — authoritative replacement
    assert ids[0] not in active
    assert [s["id"] for s in meta["newly_loaded_runbooks"]] == [ids[1]]


def test_scenario7_unknown_and_duplicate_ids(client, auth, fake_provider, app) -> None:
    fake_provider.selection_result = selection_result([RUNBOOK_PREP, "nonexistent", RUNBOOK_PREP])
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "prep"}], session_id="s7", turn_id="t1",
    ))
    meta = meta_of(r.json())
    assert [s["id"] for s in meta["active_runbooks"]] == _paired(app, RUNBOOK_PREP)


def test_scenario8_selector_fails_retains_prior(client, auth, fake_provider, app) -> None:
    fake_provider.selection_result = selection_result([RUNBOOK_PREP])
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
    assert "runbook_selector_failed_open" in meta["warnings"]
    assert [s["id"] for s in meta["active_runbooks"]] == _paired(app, RUNBOOK_PREP)


def test_scenario9_catalog_revision_change_reruns(client, auth, fake_provider, app) -> None:
    codec = app.state.container.state_codec
    stale = codec.issue(
        session_id="s9", active_runbooks=[RUNBOOK_PREP],
        catalog_revision="sha256:stale", turn_id="t1", instruction_revision=0,
    )
    fake_provider.selection_result = selection_result([RUNBOOK_RENAME])
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "rename"}], session_id="s9", turn_id="t1", state=stale,
    ))
    meta = meta_of(r.json())
    assert meta["selector"]["ran"] is True
    assert meta["catalog_revision"] == app.state.container.catalog.revision


def test_scenario10_replayed_state_other_session(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([RUNBOOK_PREP])
    r1 = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "prep"}], session_id="sessionA", turn_id="t1",
    ))
    state = r1.json()["state"]
    fake_provider.selection_result = selection_result([])
    r2 = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "prep"}], session_id="sessionB", turn_id="t1", state=state,
    ))
    meta = meta_of(r2.json())
    assert "runbook_state_session_mismatch" in meta["warnings"]
    assert meta["selector"]["ran"] is True
    assert meta["active_runbooks"] == []


def test_streaming_meta_event_first_then_deltas(client, auth, fake_provider, app) -> None:
    fake_provider.selection_result = selection_result([RUNBOOK_PREP])
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
    assert [sk["id"] for sk in meta["active_runbooks"]] == _paired(app, RUNBOOK_PREP)
    streamed = "".join(d.get("content", "") for e, d in events if e == "delta")
    assert "hello" in streamed


def test_nonstream_surfaces_reasoning_content(client, auth, fake_provider) -> None:
    # When the upstream returns a thinking-mode chain-of-thought, it is delivered
    # to the client on the assistant message so it can be replayed next turn.
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content="done", reasoning_content="my cot")
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "rename my terminals"}], session_id="rc1", turn_id="t1",
    ))
    assert r.status_code == 200
    assert r.json()["message"]["reasoning_content"] == "my cot"


def test_nonstream_omits_reasoning_content_when_absent(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content="done")
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "rename my terminals"}], session_id="rc1b", turn_id="t1",
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
        [{"role": "user", "content": "rename my terminals"}], session_id="rc2", turn_id="t1",
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
        "protocol_version": 3, "input": {"messages": [{"role": "user", "content": "rename my terminals"}]},
    })
    assert r.status_code == 400  # RequestValidationError -> invalid_request_error


@pytest.mark.parametrize(
    "messages,tools,code",
    [
        ([{"role": "system", "content": "x"}, {"role": "user", "content": "y"}], None, "system_messages_not_allowed"),
        ([{"role": "user", "content": "x"}], [{"type": "function", "function": {"name": "runbook__load"}}], "reserved_tool_name"),
    ],
)
def test_error_envelopes(client, auth, messages, tools, code) -> None:
    r = client.post(URL, headers=auth, json=respond_body(messages, tools=tools))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == code


def test_missing_auth_is_served_anonymously(client) -> None:
    """The door is open, and this is where that is visible on the wire.

    The server holds the upstream credential, so a request with no Authorization
    header has one to spend. This assertion is the one that must break loudly
    when account authentication lands — it is the stage, not an oversight.
    """
    r = client.post(URL, json=respond_body([{"role": "user", "content": "x"}]))
    assert r.status_code == 200


def test_a_stale_openrouter_bearer_is_ignored_not_forwarded(client, fake_provider) -> None:
    """The migration case, end to end.

    A CLI that has not been updated still sends the OpenRouter key it used to
    authenticate with. If the door read it, that key would be threaded to the
    provider and forwarded to Cerebras — which rejects a credential it never
    issued — and the caller would see `provider_invalid_api_key`: their key
    blamed for a header they were right to still be sending.
    """
    r = client.post(
        URL,
        headers={"Authorization": "Bearer sk-or-v1-a-stale-openrouter-key"},
        json=respond_body([{"role": "user", "content": "x"}]),
    )
    assert r.status_code == 200
    # And nothing carried the stale value upstream. The selector is not in this
    # list because it makes no upstream call at all any more — which is the
    # strongest possible version of "it did not forward the header".
    assert fake_provider.requests, "no upstream call was made, so nothing was proved"
    for req in fake_provider.requests:
        assert req.api_key is None


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


# ---- bare tool intent: a round that announced a call and emitted none (#25) ----

# Captured verbatim from session ses_659b0fc9: the model said it would read the
# votes, emitted no call, and ended the turn having done nothing.
DEAD_ROUND = "All five votes are in. Let me read them, one read per terminal, batched."
NUDGE_MARKER = "previous draft was discarded"


def _repair_tool_chunks() -> list[dict]:
    """A re-roll that does what the dead round only talked about."""
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_0", "type": "function",
             "function": {"name": "terminal__extract",
                          "arguments": '{"terminalIds": ["terminal-1"], "instruction": "vote?"}'}},
        ]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 7, "completion_tokens": 11, "total_tokens": 18}},
    ]


def _was_nudged(request) -> bool:
    return any(NUDGE_MARKER in (m.get("content") or "") for m in request.messages)


def test_nonstream_bare_intent_is_rerolled_before_the_response(
    client, auth, fake_provider
) -> None:
    """The dead draft never reaches the client; the re-roll's calls do."""
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = [
        make_result(content=DEAD_ROUND, finish_reason="stop"),
        make_result(
            tool_calls=[{
                "id": "call_0", "type": "function", "index": 0,
                "function": {"name": "terminal__extract",
                             "arguments": '{"terminalIds": ["terminal-1"], "instruction": "vote?"}'},
            }],
            finish_reason="tool_calls",
        ),
    ]
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "read the votes"}], session_id="bi1", turn_id="t1",
        tools=[EXTRACT_TOOL],
    ))
    assert r.status_code == 200
    message = r.json()["message"]
    assert message["tool_calls"][0]["function"]["name"] == "terminal__extract"
    assert DEAD_ROUND not in json.dumps(r.json())
    # Exactly one re-roll, and only IT carries the corrective message — the dead
    # draft itself is never replayed into the transcript.
    assert len(fake_provider.requests) == 2
    assert not _was_nudged(fake_provider.requests[0])
    assert _was_nudged(fake_provider.requests[1])
    assert DEAD_ROUND not in json.dumps(fake_provider.requests[1].messages)


def test_nonstream_bare_intent_reroll_happens_exactly_once(client, auth, fake_provider) -> None:
    """A re-roll that repeats the shape is still final — never a third attempt."""
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = [
        make_result(content=DEAD_ROUND, finish_reason="stop"),
        make_result(content="Let me read them now.", finish_reason="stop"),
    ]
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "read the votes"}], session_id="bi2", turn_id="t1",
        tools=[EXTRACT_TOOL],
    ))
    assert r.status_code == 200
    assert len(fake_provider.requests) == 2
    assert r.json()["message"]["content"] == "Let me read them now."


def test_nonstream_bare_intent_reroll_failure_fails_open(client, auth, fake_provider) -> None:
    """A weak reply the user is already owed beats losing the turn."""
    fake_provider.selection_result = selection_result([])
    calls: list[int] = []

    def complete(request):
        calls.append(1)
        if len(calls) == 1:
            return make_result(content=DEAD_ROUND, finish_reason="stop")
        raise upstream_error("re-roll unavailable")

    fake_provider.complete_result = complete
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "read the votes"}], session_id="bi3", turn_id="t1",
        tools=[EXTRACT_TOOL],
    ))
    assert r.status_code == 200
    assert r.json()["message"]["content"] == DEAD_ROUND
    # Returning the dead round is ALSO what happens with no repair at all, so pin
    # that a re-roll was genuinely attempted and only then fell back.
    assert len(fake_provider.requests) == 2
    assert _was_nudged(fake_provider.requests[1])


def test_nonstream_bare_intent_sums_usage_across_both_attempts(
    client, auth, fake_provider
) -> None:
    """The caller's key funded the discarded generation too."""
    from daintree_assistant_server.providers.base import ProviderUsage

    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = [
        make_result(
            content=DEAD_ROUND, finish_reason="stop",
            usage=ProviderUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
        ),
        make_result(
            content="Done.", finish_reason="stop",
            usage=ProviderUsage(prompt_tokens=130, completion_tokens=5, total_tokens=135),
        ),
    ]
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "read the votes"}], session_id="bi4", turn_id="t1",
        tools=[EXTRACT_TOOL],
    ))
    usage = r.json()["usage"]
    assert usage["prompt_tokens"] == 230
    assert usage["completion_tokens"] == 25
    assert usage["total_tokens"] == 255


def test_nonstream_legitimate_short_reply_is_not_rerolled(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(
        content="All five agents finished and their votes are recorded.", finish_reason="stop"
    )
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "status?"}], session_id="bi5", turn_id="t1",
        tools=[EXTRACT_TOOL],
    ))
    assert r.status_code == 200
    assert len(fake_provider.requests) == 1


def test_nonstream_bare_intent_without_tools_is_not_rerolled(client, auth, fake_provider) -> None:
    """With nothing callable, the sentence dropped no action."""
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content=DEAD_ROUND, finish_reason="stop")
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "read the votes"}], session_id="bi6", turn_id="t1",
    ))
    assert r.status_code == 200
    assert len(fake_provider.requests) == 1
    assert r.json()["message"]["content"] == DEAD_ROUND


def test_stream_bare_intent_reroll_is_invisible_to_the_sse_contract(
    client, auth, fake_provider
) -> None:
    """One meta, one done, no dead text — the CLI cannot tell a re-roll happened."""
    fake_provider.selection_result = selection_result([])

    def chunks(request):
        return _repair_tool_chunks() if _was_nudged(request) else make_text_chunks(DEAD_ROUND)

    fake_provider.stream_chunks = chunks
    body = respond_body(
        [{"role": "user", "content": "read the votes"}], session_id="bi7", turn_id="t1",
        generation={"stream": True}, tools=[EXTRACT_TOOL],
    )
    with client.stream("POST", URL, headers=auth, json=body) as s:
        text = "".join(s.iter_text())
    events = parse_sse(text)
    names = [e for e, _ in events]
    assert names.count("meta") == 1
    assert names.count("done") == 1
    assert names[-1] == "done"
    assert "error" not in names
    # The announcement never reached the client, and the calls it promised did.
    content = "".join(d.get("content", "") for e, d in events if e == "delta")
    assert content == ""
    assert "Let me read them" not in text
    fragments = [c for e, d in events if e == "delta" for c in d.get("tool_calls", [])]
    assert [f["function"]["name"] for f in fragments] == ["terminal__extract"]
    # Exactly two upstream opens; only the second carries the corrective message.
    assert len(fake_provider.stream_requests) == 2
    assert not _was_nudged(fake_provider.stream_requests[0])
    assert _was_nudged(fake_provider.stream_requests[1])


def test_stream_bare_intent_sums_usage_across_both_streams(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])

    def chunks(request):
        return _repair_tool_chunks() if _was_nudged(request) else make_text_chunks(DEAD_ROUND)

    fake_provider.stream_chunks = chunks
    body = respond_body(
        [{"role": "user", "content": "read the votes"}], session_id="bi8", turn_id="t1",
        generation={"stream": True}, tools=[EXTRACT_TOOL],
    )
    with client.stream("POST", URL, headers=auth, json=body) as s:
        text = "".join(s.iter_text())
    done = next(d for e, d in parse_sse(text) if e == "done")
    # make_text_chunks reports 12/8/20; the repair stream reports 7/11/18.
    assert done["usage"]["prompt_tokens"] == 19
    assert done["usage"]["completion_tokens"] == 19
    assert done["usage"]["total_tokens"] == 38
    assert done["finish_reason"] == "tool_calls"


def test_stream_bare_intent_reroll_open_failure_fails_open(client, auth, fake_provider) -> None:
    """Nothing of the re-roll is public yet, so the held draft is still deliverable."""
    fake_provider.selection_result = selection_result([])

    def chunks(request):
        if _was_nudged(request):
            raise upstream_error("re-roll unavailable")
        return make_text_chunks(DEAD_ROUND)

    fake_provider.stream_chunks = chunks
    body = respond_body(
        [{"role": "user", "content": "read the votes"}], session_id="bi9", turn_id="t1",
        generation={"stream": True}, tools=[EXTRACT_TOOL],
    )
    with client.stream("POST", URL, headers=auth, json=body) as s:
        text = "".join(s.iter_text())
    events = parse_sse(text)
    names = [e for e, _ in events]
    assert "error" not in names
    assert names[-1] == "done"
    content = "".join(d.get("content", "") for e, d in events if e == "delta")
    assert content.strip() == DEAD_ROUND
    assert next(d for e, d in events if e == "done")["finish_reason"] == "stop"
    # Delivering the dead round is ALSO what happens with no repair at all, so pin
    # that the re-roll was genuinely attempted and only then fell back.
    assert len(fake_provider.stream_requests) == 2
    assert _was_nudged(fake_provider.stream_requests[1])


def test_stream_long_reply_is_released_at_the_gate_bound(client, auth, fake_provider) -> None:
    """A legitimate long answer streams in full, and is never re-rolled."""
    from daintree_assistant_server.services.bare_intent import BARE_INTENT_MAX_CHARS

    long_answer = "word " * 200
    assert len(long_answer) > BARE_INTENT_MAX_CHARS
    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = make_text_chunks(long_answer.strip())
    body = respond_body(
        [{"role": "user", "content": "explain"}], session_id="bi10", turn_id="t1",
        generation={"stream": True}, tools=[EXTRACT_TOOL],
    )
    with client.stream("POST", URL, headers=auth, json=body) as s:
        text = "".join(s.iter_text())
    events = parse_sse(text)
    content = "".join(d.get("content", "") for e, d in events if e == "delta")
    assert content.strip() == long_answer.strip()
    assert len(fake_provider.stream_requests) == 1
    # Deltas are forwarded in their original boundaries, not coalesced into one.
    assert len([1 for e, d in events if e == "delta" and d.get("content")]) > 1


def test_stream_short_legitimate_reply_survives_the_gate(client, auth, fake_provider) -> None:
    """Held content is released at stream end when the shape does not match."""
    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = make_text_chunks("All five votes are recorded.")
    body = respond_body(
        [{"role": "user", "content": "status"}], session_id="bi11", turn_id="t1",
        generation={"stream": True}, tools=[EXTRACT_TOOL],
    )
    with client.stream("POST", URL, headers=auth, json=body) as s:
        text = "".join(s.iter_text())
    events = parse_sse(text)
    content = "".join(d.get("content", "") for e, d in events if e == "delta")
    assert content.strip() == "All five votes are recorded."
    assert len(fake_provider.stream_requests) == 1
    assert [e for e, _ in events][-1] == "done"


def test_nonstream_truncated_reply_is_not_rerolled(client, auth, fake_provider) -> None:
    """``length`` is truncation — a different failure with a different remedy."""
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content=DEAD_ROUND, finish_reason="length")
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "read the votes"}], session_id="bi12", turn_id="t1",
        tools=[EXTRACT_TOOL],
    ))
    assert r.status_code == 200
    assert len(fake_provider.requests) == 1


def test_nonstream_reroll_request_matches_the_original_except_the_nudge(
    client, auth, fake_provider
) -> None:
    """The re-roll must not quietly drop tools, the caller's key, or the transcript."""
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = [
        make_result(content=DEAD_ROUND, finish_reason="stop"),
        make_result(content="Done.", finish_reason="stop"),
    ]
    client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "read the votes"}], session_id="bi13", turn_id="t1",
        tools=[EXTRACT_TOOL],
    ))
    first, retry = fake_provider.requests
    assert retry.messages[:-1] == first.messages
    assert retry.messages[-1]["role"] == "user"
    assert NUDGE_MARKER in retry.messages[-1]["content"]
    for field in ("model", "tools", "tool_choice", "temperature", "max_tokens",
                  "response_format", "stream", "thinking_disabled", "timeout", "api_key"):
        assert getattr(retry, field) == getattr(first, field), field


def test_nonstream_usage_summing_tolerates_unreported_fields(
    client, auth, fake_provider
) -> None:
    """``None`` means "not reported" and must not poison the total."""
    from daintree_assistant_server.providers.base import ProviderUsage

    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = [
        make_result(
            content=DEAD_ROUND, finish_reason="stop",
            usage=ProviderUsage(prompt_tokens=100, completion_tokens=None, cached_tokens=40),
        ),
        make_result(
            content="Done.", finish_reason="stop",
            usage=ProviderUsage(prompt_tokens=None, completion_tokens=5, total_tokens=None),
        ),
    ]
    usage = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "read the votes"}], session_id="bi14", turn_id="t1",
        tools=[EXTRACT_TOOL],
    )).json()["usage"]
    assert usage["prompt_tokens"] == 100   # only one side reported it
    assert usage["completion_tokens"] == 5
    assert usage["cached_tokens"] == 40
    assert usage["total_tokens"] == 0      # neither side reported it


def test_stream_reasoning_disarms_the_gate(client, auth, fake_provider) -> None:
    """Reasoning is public model output, so the attempt can no longer be discarded."""
    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "All five votes are in."},
                      "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"reasoning_content": "thinking"},
                      "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": " Let me read them now."},
                      "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
    ]
    body = respond_body(
        [{"role": "user", "content": "read the votes"}], session_id="bi16", turn_id="t1",
        generation={"stream": True}, tools=[EXTRACT_TOOL],
    )
    with client.stream("POST", URL, headers=auth, json=body) as s:
        text = "".join(s.iter_text())
    events = parse_sse(text)
    names = [e for e, _ in events]
    assert len(fake_provider.stream_requests) == 1  # no re-roll once reasoning is public
    assert "status" in names
    content = "".join(d.get("content", "") for e, d in events if e == "delta")
    assert content == "All five votes are in. Let me read them now."
    # Content held before the reasoning arrived is released BEFORE the status event,
    # preserving the order the model generated it in.
    assert names.index("delta") < names.index("status")


def test_stream_retry_failing_after_public_content_is_terminal(
    client, auth, fake_provider
) -> None:
    """Once the re-roll's own content is on the wire it cannot be unsent.

    ``providers/retry.py``'s invariant: a mid-generation failure stays terminal
    because conversation content has already been delivered. No fabricated ``done``.
    """
    from fakes import FakeStream

    streams: list[FakeStream] = []

    def stream_factory(request):
        if _was_nudged(request):
            stream = FakeStream(
                [
                    {"choices": [{"index": 0, "delta": {"role": "assistant"},
                                  "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {"content": "Reading now."},
                                  "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                ],
                error_at=2,
                error=upstream_error("died mid-stream"),
            )
        else:
            stream = FakeStream(make_text_chunks(DEAD_ROUND))
        streams.append(stream)
        return stream

    fake_provider.selection_result = selection_result([])
    fake_provider.stream_factory = stream_factory
    body = respond_body(
        [{"role": "user", "content": "read the votes"}], session_id="bi17", turn_id="t1",
        generation={"stream": True}, tools=[EXTRACT_TOOL],
    )
    with client.stream("POST", URL, headers=auth, json=body) as s:
        text = "".join(s.iter_text())
    events = parse_sse(text)
    names = [e for e, _ in events]
    assert names[-1] == "error"
    assert "done" not in names
    content = "".join(d.get("content", "") for e, d in events if e == "delta")
    assert content == "Reading now."      # the accepted attempt's public content
    assert DEAD_ROUND not in text          # the discarded draft still never appears
    assert len(streams) == 2 and all(s.closed for s in streams)


def test_stream_close_failure_on_discard_does_not_cost_the_turn(
    client, auth, fake_provider, app, monkeypatch
) -> None:
    """A teardown error must never displace a reply that is complete and in hand."""
    from fakes import FakeStream

    class ExplodingCloseStream(FakeStream):
        async def aclose(self) -> None:
            self.closed = True
            raise RuntimeError("close failed")

    def stream_factory(request):
        if _was_nudged(request):
            return FakeStream(_repair_tool_chunks())
        return ExplodingCloseStream(make_text_chunks(DEAD_ROUND))

    fake_provider.selection_result = selection_result([])
    fake_provider.stream_factory = stream_factory
    body = respond_body(
        [{"role": "user", "content": "read the votes"}], session_id="bi18", turn_id="t1",
        generation={"stream": True}, tools=[EXTRACT_TOOL],
    )
    with client.stream("POST", URL, headers=auth, json=body) as s:
        text = "".join(s.iter_text())
    events = parse_sse(text)
    names = [e for e, _ in events]
    assert "error" not in names
    assert names[-1] == "done"
    fragments = [c for e, d in events if e == "delta" for c in d.get("tool_calls", [])]
    assert [f["function"]["name"] for f in fragments] == ["terminal__extract"]


@pytest.mark.parametrize("stream", [False, True])
def test_a_detector_failure_never_costs_the_turn(
    client, auth, fake_provider, monkeypatch, stream
) -> None:
    """The repair is an optional improvement to a reply already owed to the user.

    Same posture as ``_lint_final_output``: a bug in it degrades to "no repair",
    never to a lost turn.
    """
    def explode(*args, **kwargs):
        raise RuntimeError("detector bug")

    monkeypatch.setattr(
        "daintree_assistant_server.services.chat_orchestrator.is_bare_tool_intent", explode
    )
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content=DEAD_ROUND, finish_reason="stop")
    fake_provider.stream_chunks = make_text_chunks(DEAD_ROUND)
    body = respond_body(
        [{"role": "user", "content": "read the votes"}], session_id="bi19", turn_id="t1",
        generation={"stream": True} if stream else None, tools=[EXTRACT_TOOL],
    )
    if not stream:
        r = client.post(URL, headers=auth, json=body)
        assert r.status_code == 200
        assert r.json()["message"]["content"] == DEAD_ROUND
        return
    with client.stream("POST", URL, headers=auth, json=body) as s:
        text = "".join(s.iter_text())
    events = parse_sse(text)
    names = [e for e, _ in events]
    assert "error" not in names and names[-1] == "done"
    content = "".join(d.get("content", "") for e, d in events if e == "delta")
    assert content.strip() == DEAD_ROUND


def test_request_id_is_unified_across_header_and_meta(client, auth, fake_provider) -> None:
    """One request identity: middleware, response header, and SSE meta agree."""
    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = make_text_chunks("hi")
    body = respond_body(
        [{"role": "user", "content": "rename my terminals"}], session_id="rid1", turn_id="t1",
        generation={"stream": True},
    )
    # No client-supplied id: the middleware mints one and everything uses it.
    with client.stream("POST", URL, headers=auth, json=body) as s:
        header_id = s.headers["X-Request-Id"]
        text = "".join(s.iter_text())
    events = parse_sse(text)
    assert events[0][0] == "meta"
    assert events[0][1]["request_id"] == header_id

    # Client-supplied id: echoed everywhere.
    with client.stream(
        "POST", URL, headers={**auth, "x-request-id": "req-supplied-1"}, json=body
    ) as s:
        assert s.headers["X-Request-Id"] == "req-supplied-1"
        text = "".join(s.iter_text())
    assert parse_sse(text)[0][1]["request_id"] == "req-supplied-1"


# ---------------------------------------------------------------------------
# selection.pinned_runbook_ids — the caller's own pin (issue #54).
#
# End-to-end rather than resolver-level: the unit tests in
# tests/unit/test_orchestrator.py own the admission algebra, so what these assert
# is the wire — that the field is accepted at all, that a pinned body actually
# reaches the model, and that every refusal is REPORTED on both response shapes.
# A pin that silently does nothing is the one outcome this feature cannot have.
# ---------------------------------------------------------------------------


def _non_foundation_ids(app, count: int) -> list[str]:
    catalog = app.state.container.catalog
    return [s.id for s in catalog.runbooks if not s.meta.foundation][:count]


def test_a_pinned_runbook_loads_even_when_the_selector_wanted_something_else(
    client, auth, fake_provider, app
) -> None:
    """The headline. The classifier picks the rename runbook; the caller pinned the
    prep-branch one, and the pinned body is what the model is handed."""
    fake_provider.selection_result = selection_result([RUNBOOK_RENAME])
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "rename my terminals"}],
        session_id="pin1", turn_id="t1",
        selection={"pinned_runbook_ids": [RUNBOOK_PREP]},
    ))
    assert r.status_code == 200, r.text
    body = r.json()
    active = [s["id"] for s in body["runbooks"]["active"]]
    assert active[0] == RUNBOOK_PREP, f"the pin did not outrank the selection; {active}"
    # Against the real body text, not the id. The loaded-runbooks template prints
    # "Runbook id: …" for every entry, so asserting the id would pass even if the
    # runbook itself were rendered empty — which is the failure that matters.
    loaded = _loaded_runbooks_message(fake_provider)
    prep_body = app.state.container.catalog.get(RUNBOOK_PREP).body
    assert prep_body.strip().splitlines()[0] in loaded, (
        "the pinned runbook's BODY never reached the model"
    )
    assert body["warnings"] == []


def test_a_pinned_runbook_survives_a_continuation_round_without_reselection(
    client, auth, fake_provider, app
) -> None:
    """The selector does not run on a tool-continuation round, so if pins only
    applied to an authoritative round a pin sent mid-turn would silently vanish.
    The resolver runs every round, and the pin rides the state token after that."""
    fake_provider.selection_result = selection_result([])
    first = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "hello"}],
        session_id="pin2", turn_id="t1",
        selection={"pinned_runbook_ids": [RUNBOOK_PREP]},
    ))
    assert first.status_code == 200, first.text
    state = first.json()["state"]
    assert RUNBOOK_PREP in [s["id"] for s in first.json()["runbooks"]["active"]]

    second = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"},
         {"role": "user", "content": "and now?"}],
        session_id="pin2", turn_id="t1", round=1, state=state,
        selection={"pinned_runbook_ids": [RUNBOOK_PREP]},
    ))
    assert second.status_code == 200, second.text
    active = [s["id"] for s in second.json()["runbooks"]["active"]]
    assert RUNBOOK_PREP in active, f"the pin was lost on continuation; {active}"
    assert second.json()["runbooks"]["newly_loaded"] == [], (
        "a runbook already active must not be announced as a fresh load a second time"
    )


def test_an_unknown_pinned_id_warns_instead_of_silently_doing_nothing(
    client, auth, fake_provider
) -> None:
    """The failure this feature would otherwise be debugged into: a typo'd id, a
    turn that looks completely normal, and no signal that the pin missed."""
    fake_provider.selection_result = selection_result([])
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "hello"}],
        session_id="pin3", turn_id="t1",
        selection={"pinned_runbook_ids": ["daintree.does.not-exist"]},
    ))
    assert r.status_code == 200, "an unknown pin degrades the turn, it never fails it"
    body = r.json()
    assert "unknown_runbook_id_ignored" in body["warnings"]
    assert body["runbooks"]["active"] == []


def test_the_unknown_pin_warning_reaches_the_stream_meta_event(
    client, auth, fake_provider
) -> None:
    """Streaming clients learn about it at the same moment — `meta` is emitted
    before the model opens, so the warning arrives ahead of the reply it explains
    rather than after the user has read it."""
    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = make_text_chunks("ok")
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "hello"}],
        session_id="pin4", turn_id="t1",
        selection={"pinned_runbook_ids": ["daintree.does.not-exist"]},
        generation={"stream": True},
    ))
    assert r.status_code == 200
    events = {e: d for e, d in parse_sse(r.text) if e == "meta"}
    assert "unknown_runbook_id_ignored" in events["meta"]["warnings"]


def test_pins_that_overflow_the_cap_are_reported(client, auth, fake_provider, app) -> None:
    """Pins are admitted first, so reaching the cap means the pins alone did not
    fit. Not a 422: whether they fit depends on the live catalog's foundation
    topology and the configured cap, none of which a request validator can see —
    so the turn proceeds with what fits and says what it refused."""
    pins = _non_foundation_ids(app, 4)
    assert len(pins) == 4, "precondition: the catalog must hold four specifics"
    fake_provider.selection_result = selection_result([])
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "hello"}],
        session_id="pin5", turn_id="t1",
        selection={"pinned_runbook_ids": pins},
    ))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "pinned_runbook_over_cap" in body["warnings"]
    assert len(body["runbooks"]["active"]) <= app.state.container.settings.max_active_runbooks


def test_an_ordinary_turn_sends_no_pins_and_is_unchanged(client, auth, fake_provider, app) -> None:
    """The field is opt-in and inert. An ordinary turn must resolve exactly as it
    did before the tier existed — the prompt cache depends on it."""
    fake_provider.selection_result = selection_result([RUNBOOK_PREP])
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "prep this branch for review"}],
        session_id="pin6", turn_id="t1",
    ))
    assert r.status_code == 200
    assert [s["id"] for s in r.json()["runbooks"]["active"]] == _paired(app, RUNBOOK_PREP)
    assert r.json()["warnings"] == []


def test_a_large_pin_list_is_refused_at_the_boundary(client, auth, fake_provider) -> None:
    """Originally a regression test for quadratic de-duplication: the field was
    client-supplied and UNCAPPED, so `if sid not in seen_list` inside the loop was
    O(n^2) work on an n the caller picks — synchronous, on the request path, where
    no timeout could interrupt it.

    The de-duplication is still linear, but the n is no longer the caller's to
    choose: `cap_pinned_runbook_ids` refuses the list before any of that code runs.
    The test asserts the CAP now, because that is what makes the quadratic path
    structurally unreachable — a fix at the boundary rather than one downstream of
    it. Below the cap the resolver is exercised by the two tests that follow."""
    fake_provider.selection_result = selection_result([])
    pins = [f"daintree.bogus.runbook-{i}" for i in range(20_000)]
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "hello"}],
        session_id="pin7", turn_id="t1",
        selection={"pinned_runbook_ids": pins},
    ))
    assert r.status_code == 413, r.text
    error = r.json()["error"]
    assert error["code"] == "request_too_large"
    # Names the field, so a client author is not left guessing which list was
    # the oversized one.
    assert error["param"] == "selection.pinned_runbook_ids"
    # Refused at the boundary means the turn never ran.
    assert not fake_provider.requests


def test_duplicate_and_unknown_pins_report_one_warning_each(
    client, auth, fake_provider, app
) -> None:
    """Duplicates are not an error and unknowns are not fatal; mixing them must not
    multiply either signal. First mention of a valid id still wins."""
    fake_provider.selection_result = selection_result([])
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "hello"}],
        session_id="pin8", turn_id="t1",
        selection={"pinned_runbook_ids": [
            RUNBOOK_PREP, "daintree.nope.one", RUNBOOK_PREP, "daintree.nope.two", "",
        ]},
    ))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["warnings"].count("unknown_runbook_id_ignored") == 1
    active = [s["id"] for s in body["runbooks"]["active"]]
    assert active.count(RUNBOOK_PREP) == 1, f"a repeated pin loaded twice; {active}"


# --------------------------------------------------------------------------
# The aggregate context budget, at the seam where it actually runs.
#
# `tests/unit/test_context_budget.py` pins the arithmetic. These pin that it is
# WIRED — measured after tool projection has settled the inventory, refusing
# before the provider is called rather than after — because the arithmetic being
# right is worth nothing if nothing reaches it.
# --------------------------------------------------------------------------


def test_an_oversized_turn_is_refused_before_the_upstream_is_called(
    client, auth, fake_provider
) -> None:
    """Not merely "an error happens": the provider must be untouched. The whole
    value of catching this locally is the call that never gets paid for."""
    fake_provider.selection_result = selection_result([])
    # Well under the 8 MiB body cap and the 1 MiB per-message cap, and still far
    # past any context window — which is exactly the gap this check closes.
    huge = "word " * 200_000
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": huge}], session_id="ctx1", turn_id="t1",
    ))
    assert r.status_code == 413, r.text
    body = r.json()
    assert body["error"]["code"] == "context_window_exceeded"
    assert body["error"]["param"] == "input.messages"
    # The dimensions a caller can act on.
    assert "context window" in body["error"]["message"]
    assert not fake_provider.requests, "the upstream was called for a turn that cannot fit"


def test_an_ordinary_turn_is_not_touched_by_the_budget(client, auth, fake_provider) -> None:
    """The regression that matters most. A budget that fires on normal traffic is
    worse than no budget: it fails turns the model would have answered."""
    fake_provider.selection_result = selection_result([])
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "hello"}], session_id="ctx2", turn_id="t1",
    ))
    assert r.status_code == 200, r.text


def test_a_failed_measurement_does_not_fail_the_turn(
    client, auth, fake_provider, monkeypatch
) -> None:
    """The guard around the measurement, exercised.

    A lost estimate is a lost NUMBER; the raise beside it is deliberate and sits
    outside the guard. Those two live one line apart, so a future edit that
    widened the `try` to cover the refusal — or narrowed it away from the
    measurement — would be invisible without this. Nothing else in the suite can
    tell the two apart, because a healthy turn takes neither path.
    """
    from daintree_assistant_server.services import chat_orchestrator

    def boom(_request: object) -> None:
        raise RuntimeError("estimator exploded")

    monkeypatch.setattr(chat_orchestrator, "measure_context", boom)
    fake_provider.selection_result = selection_result([])
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "hello"}], session_id="ctx4", turn_id="t1",
    ))
    assert r.status_code == 200, r.text
    assert fake_provider.requests, "the turn should have reached the upstream"


def test_an_oversized_streaming_turn_ends_with_a_terminal_error_event(
    client, auth, fake_provider
) -> None:
    """Streaming commits HTTP 200 before preparation finishes, so the refusal can
    only be reported in band — as a terminal `error`, never a fabricated `done`."""
    fake_provider.selection_result = selection_result([])
    r = client.post(URL, headers=auth, json=respond_body(
        [{"role": "user", "content": "word " * 200_000}],
        session_id="ctx3", turn_id="t1", generation={"stream": True},
    ))
    events = parse_sse(r.text)
    names = [name for name, _ in events]
    assert "done" not in names, f"a turn that never ran must not report done; {names}"
    assert names[-1] == "error", names
    payload = dict(events[-1][1])
    assert payload["error"]["code"] == "context_window_exceeded"
    assert not fake_provider.requests
