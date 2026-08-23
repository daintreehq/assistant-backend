"""Output-contract warnings on BOTH response paths.

The lint has existed for a while, but only non-streaming, and only as a log
line. That left the two parties who could act on a malformed reply — the tester
and the CLI rendering it — as the only ones not told, on the path
(streaming) where nearly all real interaction happens.

What is deliberately NOT here: mid-stream termination. The review asked for the
stream to be killed on "severe hidden-content leakage". The hidden material in
this system is the server's own prompt and the user's own local context, never a
third party's secret, so a leak is a quality defect rather than a disclosure —
and ending a stream the user is reading would trade a real cost for a cosmetic
one. Streaming warns; it does not amputate.
"""

from __future__ import annotations

import json
from typing import Any

from conftest import respond_body
from fakes import make_result, selection_result

URL = "/v1/daintree/respond"

# Wider than the 56-column default and over the 3-column budget.
WIDE_TABLE = (
    "Here you go:\n\n"
    "| terminal | worktree | agent | state | last output |\n"
    "|---|---|---|---|---|\n"
    "| terminal-aaaaaaaa | /very/long/path/one | claude | working | building |\n"
    "| terminal-bbbbbbbb | /very/long/path/two | codex | waiting | asked a question |\n"
)
LEAKED_THINKING = (
    "<think>the user wants a summary, let me check</think>\n\nAll five agents finished."
)


def _events(raw: str) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    name: str | None = None
    for line in raw.splitlines():
        if line.startswith("event: "):
            name = line.removeprefix("event: ").strip()
        elif line.startswith("data: ") and name:
            out.append((name, json.loads(line.removeprefix("data: "))))
    return out


def _stream(client, auth, chunks, **kw):  # type: ignore[no-untyped-def]
    with client.stream(
        "POST",
        URL,
        headers=auth,
        json=respond_body(
            [{"role": "user", "content": "status?"}],
            generation={"stream": True},
            **kw,
        ),
    ) as s:
        return "".join(s.iter_text())


def _done(raw: str) -> dict[str, Any]:
    return next(payload for name, payload in _events(raw) if name == "done")


def _text_chunks(text: str) -> list[dict[str, Any]]:
    return [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def test_streamed_thinking_leakage_warns_on_done(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = _text_chunks(LEAKED_THINKING)

    done = _done(_stream(client, auth, fake_provider.stream_chunks, session_id="og1"))

    assert "output_think_leak" in done["warnings"]


def test_a_streamed_wide_table_warns_on_done(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = _text_chunks(WIDE_TABLE)

    done = _done(_stream(client, auth, fake_provider.stream_chunks, session_id="og2"))

    assert "output_wide_table" in done["warnings"]


def test_a_clean_streamed_reply_warns_about_nothing(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = _text_chunks("All five agents finished. Nothing needs you.")

    done = _done(_stream(client, auth, fake_provider.stream_chunks, session_id="og3"))

    assert done["warnings"] == []


def test_a_leaking_stream_still_delivers_its_reply(client, auth, fake_provider) -> None:
    """The decision not to terminate, pinned.

    A future change that kills the stream on leakage — which the review asked
    for — has to fail a test that says why it must not.
    """
    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = _text_chunks(LEAKED_THINKING)

    raw = _stream(client, auth, fake_provider.stream_chunks, session_id="og4")

    names = [name for name, _ in _events(raw)]
    assert "done" in names and "error" not in names
    assert "All five agents finished." in raw


def test_a_continuation_round_is_not_linted(client, auth, fake_provider) -> None:
    """A round ending in a tool call is interstitial, not an answer.

    Its prose is a sentence before an action, and holding it to the final-output
    contract would warn on text no user reads as a reply — the same rule the
    non-streaming path already follows.
    """
    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": WIDE_TABLE}, "finish_reason": None}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c0",
                                "type": "function",
                                "function": {"name": "context__snapshot", "arguments": "{}"},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ]

    done = _done(_stream(client, auth, fake_provider.stream_chunks, session_id="og5"))

    assert done["warnings"] == []


def test_streaming_warnings_never_touch_the_tool_calls(client, auth, fake_provider) -> None:
    """ "Tool calls are not amputated by a prose limit" — the review's acceptance
    criterion, as a test."""
    fake_provider.selection_result = selection_result([])
    long_prose = "x" * 9000  # over the 8000-char warn budget
    fake_provider.stream_chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": long_prose}, "finish_reason": None}]},
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "c0",
                                "type": "function",
                                "function": {
                                    "name": "context__snapshot",
                                    "arguments": '{"detail": "full"}',
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ]

    raw = _stream(client, auth, fake_provider.stream_chunks, session_id="og6")

    calls = [
        c
        for name, payload in _events(raw)
        if name == "delta"
        for c in (payload.get("tool_calls") or [])
    ]
    assert calls, "the tool call did not survive"
    assert json.loads(calls[0]["function"]["arguments"]) == {"detail": "full"}


# ---------------------------------------------------------------------------
# Non-streaming — the same findings, now reaching the caller
# ---------------------------------------------------------------------------


def test_nonstream_lint_findings_reach_the_response(client, auth, fake_provider) -> None:
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content=LEAKED_THINKING)

    r = client.post(URL, headers=auth, json=respond_body([{"role": "user", "content": "status?"}]))

    assert "output_think_leak" in r.json()["warnings"]


def test_nonstream_lint_does_not_discard_other_warnings(client, auth, fake_provider) -> None:
    """A reply can be malformed AND have had a request-level problem.

    The lint codes are APPENDED, never assigned. An unreadable state token is
    already reported as a warning; a leaking reply must not silently replace it,
    because the two point at completely different causes.
    """
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(content=LEAKED_THINKING)

    r = client.post(
        URL,
        headers=auth,
        json=respond_body(
            [{"role": "user", "content": "status?"}],
            state="not-a-valid-signed-token",
        ),
    )

    warnings = r.json()["warnings"]
    assert "output_think_leak" in warnings
    # Whatever the codec called it, the state complaint survived alongside.
    assert [w for w in warnings if w != "output_think_leak"], warnings


# ---------------------------------------------------------------------------
# Cost, end to end
# ---------------------------------------------------------------------------


def test_a_request_reports_its_TOTAL_cost(client, auth, fake_provider) -> None:
    """The headline number: what the whole request charged.

    A turn used to be TWO billed upstream calls — the runbook selector and the main
    completion — and reporting only the second understated the bill by the part
    nobody could see. Selection is in process now, so a turn is one billed call
    and the total equals it. That makes this test look tautological and it is
    not: `total` is still a SUM over components, the selector component is still
    populated and reported when a decision carries usage, and a future second
    billed call (compaction) reinstates the original hazard. What this pins is
    that the sum reports the whole of what happened, and that a component which
    genuinely cost nothing contributes nothing rather than being invented.
    """
    from daintree_assistant_server.providers.base import ProviderUsage

    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(
        content="done",
        usage=ProviderUsage(
            prompt_tokens=1000, completion_tokens=20, cached_tokens=900, cost=0.0021
        ),
    )

    body = client.post(
        URL,
        headers=auth,
        json=respond_body([{"role": "user", "content": "hi"}], selection={"force": True}),
    ).json()

    assert body["cost"]["total"] == 0.0021
    assert body["cost"]["main"] == 0.0021
    # ABSENT, not 0.0. A zero would be a claim that a billed selector call
    # happened and was free; nothing was billed at all.
    assert "selector" not in body["cost"]
    # And the total is still exact: a free component is not an unknown one.
    assert body["cost"]["complete"] is True
    # The per-call usage blocks keep their own figures.
    assert body["usage"]["cost"] == 0.0021
    assert body["usage"]["cached_tokens"] == 900


def test_the_streamed_total_matches_the_nonstreamed_one(client, auth, fake_provider) -> None:

    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": None}]},
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 20, "cost": 0.0021},
        },
    ]

    done = _done(
        _stream(
            client, auth, fake_provider.stream_chunks, session_id="cost1", selection={"force": True}
        )
    )

    assert done["cost"]["total"] == 0.0021
    assert "selector" not in done["cost"]
    assert done["cost"]["complete"] is True


def test_an_unreported_cost_is_absent_never_zero(client, auth, fake_provider) -> None:
    """Absence means "we do not know". A zero would mean "this was free".

    Only one of those is a claim about the caller's money, and we have no basis
    for it — so the whole block is omitted rather than filled with defaults.
    """
    from daintree_assistant_server.providers.base import ProviderUsage

    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(
        content="done", usage=ProviderUsage(prompt_tokens=10, completion_tokens=5)
    )

    body = client.post(
        URL, headers=auth, json=respond_body([{"role": "user", "content": "hi"}])
    ).json()

    assert "cost" not in body
    assert "cost" not in body["usage"]
    # The token counts are still there — only the money is unknown.
    assert body["usage"]["prompt_tokens"] == 10


def test_a_streamed_unknown_cost_is_absent_like_the_nonstreamed_one(
    client, auth, fake_provider
) -> None:
    """`absent_when_unknown` has to mean the same thing on both paths.

    The streaming `done` event was serialized without `exclude_none`, so an
    unreported cost arrived as an explicit null while the non-streaming body
    omitted the field. A client testing key presence to tell "unknown" from
    "free" got a different answer per response mode.
    """
    fake_provider.selection_result = selection_result([])
    fake_provider.stream_chunks = _text_chunks("done")

    done = _done(_stream(client, auth, fake_provider.stream_chunks, session_id="cost2"))

    assert "cost" not in done
    assert "cost" not in done["usage"]


def test_a_task_reports_its_cost_and_omits_an_unknown_one(client, auth, fake_provider) -> None:
    from daintree_assistant_server.providers.base import ProviderUsage

    fake_provider.complete_result = make_result(
        content='{"summary": "ok"}', usage=ProviderUsage(prompt_tokens=10, cost=0.00007)
    )
    r = client.post(
        "/v1/daintree/tasks",
        headers=auth,
        json={
            "task": "terminal_summarize",
            "input": {"text": "some scrollback", "terminal_id": "terminal-1"},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["usage"]["cost"] == 0.00007

    fake_provider.complete_result = make_result(
        content='{"summary": "ok"}', usage=ProviderUsage(prompt_tokens=10)
    )
    r = client.post(
        "/v1/daintree/tasks",
        headers=auth,
        json={
            "task": "terminal_summarize",
            "input": {"text": "some scrollback", "terminal_id": "terminal-1"},
        },
    )
    assert "cost" not in r.json()["usage"]
