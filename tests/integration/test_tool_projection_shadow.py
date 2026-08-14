"""The shadow tool projection, end to end through /respond.

The unit tests prove the projection COMPUTES the right set. They say nothing
about whether the orchestrator carries it to the response paths and grades what
the model actually called — delete either grading call and every one of them
stays green. These tests close that gap by asserting on the emitted log record.

They also pin the invariant that makes shadow mode shadow: the full inventory
still reaches the provider. The moment that stops being true, this is no longer a
measurement, and the measurement is the only thing authorizing the change.
"""

from __future__ import annotations

from typing import Any

import structlog.testing
from conftest import respond_body
from fakes import make_result, selection_result

URL = "/v1/daintree/respond"

# A skill-less turn proposes only the always-on core, so `forge__getPR` is a tool
# the projection would withhold and `context__snapshot` is one it would keep.
CORE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "context__snapshot",
        "description": "Snapshot the workspace.",
        "parameters": {"type": "object", "properties": {}},
    },
}
WITHHELD_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "forge__getPR",
        "description": "Read a PR.",
        "parameters": {
            "type": "object",
            "properties": {"prNumber": {"type": "integer"}},
            "required": ["prNumber"],
        },
    },
}


def _shadow_records(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in logs if r.get("event") == "tool_projection_shadow"]


def _call(name: str, index: int = 0) -> dict[str, Any]:
    return {
        "id": f"call_{index}",
        "type": "function",
        "index": index,
        "function": {"name": name, "arguments": "{}"},
    }


def test_nonstream_grades_a_call_outside_the_projection(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(
        tool_calls=[_call("forge__getPR")], finish_reason="tool_calls"
    )
    with structlog.testing.capture_logs() as logs:
        r = client.post(
            URL,
            headers=auth,
            json=respond_body(
                [{"role": "user", "content": "check the PR"}],
                session_id="tps1",
                turn_id="t1",
                tools=[CORE_TOOL, WITHHELD_TOOL],
            ),
        )
    assert r.status_code == 200
    (record,) = _shadow_records(logs)
    assert record["outside_proposed"] == ["forge__getPR"]
    assert record["measured"] is True
    assert record["offered"] == 2


def test_nonstream_grades_a_call_inside_the_projection(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(
        tool_calls=[_call("context__snapshot")], finish_reason="tool_calls"
    )
    with structlog.testing.capture_logs() as logs:
        r = client.post(
            URL,
            headers=auth,
            json=respond_body(
                [{"role": "user", "content": "what's open?"}],
                session_id="tps2",
                turn_id="t1",
                tools=[CORE_TOOL, WITHHELD_TOOL],
            ),
        )
    assert r.status_code == 200
    (record,) = _shadow_records(logs)
    assert record["outside_proposed"] == []
    assert record["measured"] is True


def test_shadow_mode_still_sends_the_whole_inventory(client, auth, fake_provider) -> None:
    """The invariant that makes this a measurement rather than a change.

    `forge__getPR` is outside the projection. It must still be offered upstream —
    otherwise the shadow log is grading a filter that is already live, and the
    evidence it produces is about a decision that was never made deliberately.
    """
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content="nothing to do")
    r = client.post(
        URL,
        headers=auth,
        json=respond_body(
            [{"role": "user", "content": "hi"}],
            session_id="tps3",
            turn_id="t1",
            tools=[CORE_TOOL, WITHHELD_TOOL],
        ),
    )
    assert r.status_code == 200
    sent = fake_provider.requests[-1]
    names = {t["function"]["name"] for t in (sent.tools or [])}
    assert names == {"context__snapshot", "forge__getPR"}


def test_streaming_grades_a_call_whose_name_arrives_split(client, auth, fake_provider) -> None:
    """The streaming half, with the fragmentation that broke the first version.

    Reading names off raw delta fragments saw "forge" and "__getPR" as two calls,
    neither of which is a tool — so both landed in `outside_proposed` and a
    correct projection looked unsafe to enable.
    """
    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_0",
                                "type": "function",
                                "function": {"name": "forge"},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"tool_calls": [{"index": 0, "function": {"name": "__getPR"}}]},
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [{"index": 0, "function": {"arguments": '{"prNumber": 1}'}}]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ]
    with (
        structlog.testing.capture_logs() as logs,
        client.stream(
            "POST",
            URL,
            headers=auth,
            json=respond_body(
                [{"role": "user", "content": "check the PR"}],
                session_id="tps4",
                turn_id="t1",
                tools=[CORE_TOOL, WITHHELD_TOOL],
                generation={"stream": True},
            ),
        ) as s,
    ):
        body = "".join(s.iter_text())
    assert "event: done" in body
    (record,) = _shadow_records(logs)
    # The consolidated name, once — not two fragments.
    assert record["outside_proposed"] == ["forge__getPR"]
    assert record["called"] == 1
    assert record["measured"] is True


def test_an_unreadable_round_is_logged_indeterminate_not_clean(client, auth, fake_provider) -> None:
    """A call the grader could not read must NOT read as zero.

    A fragment with no usable `index` puts the buffer into passthrough: it is
    forwarded to the client but never consolidated, so its name is never
    recorded. Reporting that round as "nothing outside the projection" would be a
    false all-clear on the one number that authorizes withholding tools.
    """
    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {  # no "index" — the passthrough trigger
                                "id": "call_0",
                                "type": "function",
                                "function": {"name": "forge__getPR", "arguments": "{}"},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ]
    with (
        structlog.testing.capture_logs() as logs,
        client.stream(
            "POST",
            URL,
            headers=auth,
            json=respond_body(
                [{"role": "user", "content": "check the PR"}],
                session_id="tps5",
                turn_id="t1",
                tools=[CORE_TOOL, WITHHELD_TOOL],
                generation={"stream": True},
            ),
        ) as s,
    ):
        body = "".join(s.iter_text())
    assert "event: done" in body
    # The call still reached the client — passthrough is fail-open by design.
    assert "forge__getPR" in body
    (record,) = _shadow_records(logs)
    assert record["measured"] is False, "an unread call must not be graded as measured"
    assert record["called"] == 0
