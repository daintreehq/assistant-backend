"""Integration tests for the chat endpoint and the skill-orchestration scenarios
from specification section 19.5."""

from __future__ import annotations

from typing import Any

import pytest
from conftest import decode_meta
from fakes import FakeProvider, make_text_chunks, tool_call_for

from daintree_assistant_server.errors import upstream_error

SKILL_PREP = "daintree.workflow.prep-branch-for-review"
SKILL_RENAME = "daintree.terminals.rename"


def _body(messages: list[dict], **daintree: Any) -> dict:
    return {
        "model": "daintree-assistant",
        "messages": messages,
        "daintree": {"protocol_version": 1, **daintree},
    }


def _main_request(provider: FakeProvider):
    return provider.requests[-1]


def _loaded_skills_message(provider: FakeProvider) -> str:
    # base, runtime, loaded_skills are the first three system messages.
    return _main_request(provider).messages[2]["content"]


def test_scenario1_new_session_skill_selected(client, auth, fake_provider, app) -> None:
    fake_provider.tool_call_result = tool_call_for([SKILL_PREP])
    r = client.post("/v1/chat/completions", headers=auth, json=_body(
        [{"role": "user", "content": "prep this branch for review"}],
        session_id="s1", turn_id="t1",
    ))
    assert r.status_code == 200
    meta = decode_meta(r.headers)
    assert [s["id"] for s in meta["active_skills"]] == [SKILL_PREP]
    assert [s["id"] for s in meta["newly_loaded_skills"]] == [SKILL_PREP]
    assert len(meta["prelude"]["tool_executions"]) == 1
    assert meta["prelude"]["tool_executions"][0]["display_name"] == "skill.load"
    # synthetic skill__load present in upstream transcript
    upstream = _main_request(fake_provider)
    assert any(
        m.get("tool_calls") and m["tool_calls"][0]["function"]["name"] == "skill__load"
        for m in upstream.messages
    )
    # skill body injected
    skill = app.state.container.catalog.get(SKILL_PREP)
    assert skill.title in _loaded_skills_message(fake_provider)


def test_scenario2_no_relevant_skill_no_prelude(client, auth, fake_provider) -> None:
    fake_provider.tool_call_result = tool_call_for([])
    r = client.post("/v1/chat/completions", headers=auth, json=_body(
        [{"role": "user", "content": "hello"}], session_id="s2", turn_id="t1",
    ))
    meta = decode_meta(r.headers)
    assert meta["active_skills"] == []
    assert meta["prelude"]["tool_executions"] == []


def test_scenario3_same_turn_round_does_not_rerun(client, auth, fake_provider) -> None:
    fake_provider.tool_call_result = tool_call_for([SKILL_PREP])
    r1 = client.post("/v1/chat/completions", headers=auth, json=_body(
        [{"role": "user", "content": "prep branch"}], session_id="s3", turn_id="t1", round=0,
    ))
    state = decode_meta(r1.headers)["state"]
    fake_provider.tool_call_requests.clear()

    r2 = client.post("/v1/chat/completions", headers=auth, json=_body(
        [
            {"role": "user", "content": "prep branch"},
            {"role": "assistant", "content": "ok"},
            {"role": "tool", "content": "done", "tool_call_id": "c1"},
        ],
        session_id="s3", turn_id="t1", round=1, state=state,
    ))
    meta = decode_meta(r2.headers)
    assert len(fake_provider.tool_call_requests) == 0  # selector NOT rerun
    assert meta["selector"]["ran"] is False
    assert [s["id"] for s in meta["active_skills"]] == [SKILL_PREP]  # retained
    assert meta["newly_loaded_skills"] == []


def test_scenario4_new_turn_reruns(client, auth, fake_provider) -> None:
    fake_provider.tool_call_result = tool_call_for([SKILL_PREP])
    r1 = client.post("/v1/chat/completions", headers=auth, json=_body(
        [{"role": "user", "content": "prep branch"}], session_id="s4", turn_id="t1",
    ))
    state = decode_meta(r1.headers)["state"]
    fake_provider.tool_call_requests.clear()
    fake_provider.tool_call_result = tool_call_for([SKILL_RENAME])

    r2 = client.post("/v1/chat/completions", headers=auth, json=_body(
        [{"role": "user", "content": "rename terminals"}], session_id="s4", turn_id="t2", state=state,
    ))
    meta = decode_meta(r2.headers)
    assert len(fake_provider.tool_call_requests) == 1  # reran
    assert meta["selector"]["ran"] is True
    assert SKILL_RENAME in [s["id"] for s in meta["active_skills"]]


def test_scenario5_interjection_reruns_once(client, auth, fake_provider) -> None:
    fake_provider.tool_call_result = tool_call_for([SKILL_PREP])
    r1 = client.post("/v1/chat/completions", headers=auth, json=_body(
        [{"role": "user", "content": "prep branch"}], session_id="s5", turn_id="t1", instruction_revision=0,
    ))
    state = decode_meta(r1.headers)["state"]
    fake_provider.tool_call_requests.clear()

    r2 = client.post("/v1/chat/completions", headers=auth, json=_body(
        [{"role": "user", "content": "actually rename terminals"}],
        session_id="s5", turn_id="t1", instruction_revision=1, state=state,
    ))
    assert len(fake_provider.tool_call_requests) == 1
    assert decode_meta(r2.headers)["selector"]["ran"] is True


def test_scenario6_cap_three_pushes_oldest_out(client, auth, fake_provider, app) -> None:
    ids = sorted(app.state.container.catalog.known_ids)
    state = None
    for i in range(4):
        fake_provider.tool_call_result = tool_call_for([ids[i]])
        r = client.post("/v1/chat/completions", headers=auth, json=_body(
            [{"role": "user", "content": f"task {i}"}], session_id="s6", turn_id=f"t{i}", state=state,
        ))
        state = decode_meta(r.headers)["state"]
    meta = decode_meta(r.headers)
    active = [s["id"] for s in meta["active_skills"]]
    assert len(active) == 3
    assert ids[3] == active[0]  # newest first
    assert ids[0] not in active  # oldest evicted


def test_scenario7_unknown_and_duplicate_ids(client, auth, fake_provider) -> None:
    fake_provider.tool_call_result = tool_call_for([SKILL_PREP, "nonexistent", SKILL_PREP])
    r = client.post("/v1/chat/completions", headers=auth, json=_body(
        [{"role": "user", "content": "prep"}], session_id="s7", turn_id="t1",
    ))
    meta = decode_meta(r.headers)
    assert [s["id"] for s in meta["active_skills"]] == [SKILL_PREP]


def test_scenario8_selector_fails_retains_prior(client, auth, fake_provider) -> None:
    fake_provider.tool_call_result = tool_call_for([SKILL_PREP])
    r1 = client.post("/v1/chat/completions", headers=auth, json=_body(
        [{"role": "user", "content": "prep"}], session_id="s8", turn_id="t1",
    ))
    state = decode_meta(r1.headers)["state"]
    fake_provider.tool_call_error = upstream_error("selector down")

    r2 = client.post("/v1/chat/completions", headers=auth, json=_body(
        [{"role": "user", "content": "next task"}], session_id="s8", turn_id="t2", state=state,
    ))
    assert r2.status_code == 200  # main response not blocked
    meta = decode_meta(r2.headers)
    assert meta["selector"]["degraded"] is True
    assert "skill_selector_failed_open" in meta["warnings"]
    assert [s["id"] for s in meta["active_skills"]] == [SKILL_PREP]  # retained


def test_scenario9_catalog_revision_change_reruns(client, auth, fake_provider, app) -> None:
    codec = app.state.container.state_codec
    stale = codec.issue(
        session_id="s9", active_skills=[(SKILL_PREP, "9.9.9")],
        catalog_revision="sha256:stale", turn_id="t1", instruction_revision=0,
    )
    fake_provider.tool_call_result = tool_call_for([SKILL_RENAME])
    r = client.post("/v1/chat/completions", headers=auth, json=_body(
        [{"role": "user", "content": "rename"}], session_id="s9", turn_id="t1", state=stale,
    ))
    meta = decode_meta(r.headers)
    assert meta["selector"]["ran"] is True
    assert meta["catalog_revision"] == app.state.container.catalog.revision


def test_scenario10_replayed_state_other_session(client, auth, fake_provider) -> None:
    fake_provider.tool_call_result = tool_call_for([SKILL_PREP])
    r1 = client.post("/v1/chat/completions", headers=auth, json=_body(
        [{"role": "user", "content": "prep"}], session_id="sessionA", turn_id="t1",
    ))
    state = decode_meta(r1.headers)["state"]
    fake_provider.tool_call_result = tool_call_for([])
    r2 = client.post("/v1/chat/completions", headers=auth, json=_body(
        [{"role": "user", "content": "prep"}], session_id="sessionB", turn_id="t1", state=state,
    ))
    meta = decode_meta(r2.headers)
    assert "skill_state_session_mismatch" in meta["warnings"]
    assert meta["selector"]["ran"] is True
    # The other session's active skills must NOT carry over.
    assert meta["active_skills"] == []


def test_streaming_meta_before_first_token(client, auth, fake_provider) -> None:
    fake_provider.tool_call_result = tool_call_for([SKILL_PREP])
    fake_provider.stream_chunks = make_text_chunks("hello there")
    body = _body([{"role": "user", "content": "prep"}], session_id="ss", turn_id="t1")
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    with client.stream("POST", "/v1/chat/completions", headers=auth, json=body) as s:
        assert s.status_code == 200
        assert "x-daintree-meta" in {k.lower() for k in s.headers}
        text = "".join(s.iter_text())
    assert "data: [DONE]" in text
    assert "daintree-assistant" in text  # model alias rewritten


def test_stateless_mode_runs_selector(client, auth, fake_provider) -> None:
    fake_provider.tool_call_result = tool_call_for([SKILL_PREP])
    # No daintree extension at all.
    r = client.post("/v1/chat/completions", headers=auth, json={
        "model": "daintree-assistant", "messages": [{"role": "user", "content": "prep"}],
    })
    assert r.status_code == 200
    meta = decode_meta(r.headers)
    assert meta["selector"]["ran"] is True
    assert "x-daintree-meta" in {k.lower() for k in r.headers}


@pytest.mark.parametrize(
    "payload,code,status",
    [
        ({"model": "daintree-assistant", "messages": [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]}, "system_messages_not_allowed", 400),
        ({"model": "daintree-assistant", "messages": [{"role": "user", "content": "x"}], "tools": [{"type": "function", "function": {"name": "skill__load"}}]}, "reserved_tool_name", 400),
        ({"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]}, "model_not_found", 404),
    ],
)
def test_error_envelopes(client, auth, payload, code, status) -> None:
    r = client.post("/v1/chat/completions", headers=auth, json=payload)
    assert r.status_code == status
    assert r.json()["error"]["code"] == code


def test_missing_auth_401(client) -> None:
    r = client.post("/v1/chat/completions", json={"model": "daintree-assistant", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


def test_unsupported_protocol_426(client, auth) -> None:
    r = client.post("/v1/chat/completions", headers={**auth, "X-Daintree-Protocol": "99"},
                    json={"model": "daintree-assistant", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 426
