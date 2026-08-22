"""The deterministic no-task fast path.

Two kinds of test here, and the second is the one that matters.

The unit cases pin individual judgements. The **corpus** test replays every case
in `benchmarks/skill_selection/cases/` — the same battery the live selector
benchmark grades against — and asserts the fast path never fires on a case that
declares a required skill. That is a real offline recall gate over ~100 realistic
turns, not a hand-picked sample, so it catches a widened vocabulary that happens
to swallow a genuine request.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from daintree_assistant_server.contracts.openai import ChatMessage
from daintree_assistant_server.skills.fast_path import (
    MAX_FAST_PATH_CHARS,
    can_skip_selection,
    is_no_task_utterance,
)

CASES_DIR = (
    Path(__file__).resolve().parents[2] / "benchmarks" / "skill_selection" / "cases"
)


def _user(text: str) -> list[ChatMessage]:
    return [ChatMessage(role="user", content=text)]


# ---------------------------------------------------------------------------
# Fires on things that are obviously nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "hey",
        "hi there",
        "hello",
        "yo",
        "thanks",
        "thanks!",
        "thank you",
        "perfect, thanks!",
        "ok cool",
        "great, thanks",
        "nice",
        "awesome",
        "sounds good",
        "got it",
        "understood",
        "no worries",
        "ok",
        "you there",
        "all good",
        "cheers",
    ],
)
def test_fires_on_a_bare_greeting_or_acknowledgement(text: str) -> None:
    assert is_no_task_utterance(_user(text)) is True


# ---------------------------------------------------------------------------
# Refuses everything it cannot rule out.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("thanks — now rename the terminals", "an ack with a task attached"),
        ("ok, close them", "an ack with an imperative"),
        ("great, start work on issue 42", "an ack with a task"),
        ("what model are you running on?", "a question: chitchat and requests look alike"),
        ("what terminals are open?", "a question that IS a request"),
        ("hey, can you check the build?", "a greeting with a task attached"),
        ("yes", "bare consent — may be answering a pending question"),
        ("yep", "bare consent"),
        ("sure", "bare consent"),
        ("no", "a bare refusal is an answer, not an acknowledgement"),
        ("rename my terminals", "a plain request"),
        ("status", "a one-word request"),
        ("👍", "no words: no positive evidence either way"),
        # Filler-only deictics. Every one points at a target the user is
        # selecting — an answer, not an acknowledgement.
        ("it", "a bare deictic"),
        ("just this", "filler-only: points at a target"),
        ("so this", "filler-only"),
        ("all of them", "filler-only: a selection"),
        ("the first", "filler-only: a selection"),
        ("that one", "filler-only: a selection"),
        ("do it", "an imperative built from allowed words"),
        # Unicode bypasses. An ASCII word matcher extracted only "ok" from each
        # of these and declared the turn task-free — recognising FRAGMENTS is
        # not recognising the INPUT.
        ("ok ｃｌｏｓｅ ａｌｌ ｔｅｒｍｉｎａｌｓ", "fullwidth latin request"),
        ("ok — 关闭所有终端", "chinese request"),
        ("ok, закрой терминалы", "cyrillic request"),
        ("ok, 2+2", "digits and symbols"),
        ("ok？", "fullwidth question mark"),
        ("hey 👋 close them", "emoji plus a request"),
        # Compositional imperatives: every word was in the old vocabulary, and
        # the combination is a command.
        ("right all this", "composed imperative"),
        ("no good, right this", "composed imperative"),
        ("thank roger", "composed nonsense that passed a token bag"),
        ("no thanks", "a refusal, not an acknowledgement"),
        ("continue", "an imperative"),
        ("keep going", "an imperative"),
        ("retry", "an imperative"),
        ("", "empty"),
        ("   ", "whitespace"),
    ],
)
def test_refuses_anything_it_cannot_rule_out(text: str, why: str) -> None:
    assert is_no_task_utterance(_user(text)) is False, why


def test_refuses_a_long_utterance_even_in_vocabulary() -> None:
    """Past a handful of words, the odds that a closed vocabulary happens to
    cover a real request rise faster than the saving."""
    padded = " ".join(["thanks"] * 40)
    assert len(padded) > MAX_FAST_PATH_CHARS
    assert is_no_task_utterance(_user(padded)) is False


def test_refuses_an_ack_that_answers_a_pending_question() -> None:
    """"ok" to "shall I close them?" is CONSENT to a workflow. Clearing that
    workflow's skill is precisely backwards, so a preceding assistant question
    disqualifies the whole path."""
    messages = [
        ChatMessage(role="user", content="tidy up please"),
        ChatMessage(role="assistant", content="Shall I close the three idle terminals?"),
        ChatMessage(role="user", content="ok"),
    ]
    assert is_no_task_utterance(messages) is False

    # The same ack after a STATEMENT is an ordinary acknowledgement.
    messages[1] = ChatMessage(role="assistant", content="Closed the three idle terminals.")
    assert is_no_task_utterance(messages) is True


def test_refuses_when_the_newest_message_is_not_the_user() -> None:
    """A tool result or assistant turn newest means a CONTINUATION round, which
    has its own cadence rule and must not be short-circuited here."""
    messages = [
        ChatMessage(role="user", content="thanks"),
        ChatMessage(role="assistant", content="Sure."),
    ]
    assert is_no_task_utterance(messages) is False


def test_refuses_an_empty_conversation() -> None:
    assert is_no_task_utterance([]) is False


# ---------------------------------------------------------------------------
# THE gate: the whole selector benchmark corpus.
# ---------------------------------------------------------------------------


def _corpus() -> list[tuple[str, dict]]:
    cases: list[tuple[str, dict]] = []
    for path in sorted(CASES_DIR.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for case in doc.get("cases", []):
            cases.append((path.name, case))
    return cases


def test_the_corpus_is_actually_loaded() -> None:
    """Guard against the gate below silently passing on a truncated corpus.

    Pinned near the real count rather than at a token threshold: `> 50` would
    have stayed green while nearly half the cases vanished.
    """
    cases = _corpus()
    assert len(cases) >= 100, f"only {len(cases)} cases found — is CASES_DIR right?"
    with_required = [c for _, c in cases if (c.get("expect") or {}).get("required")]
    assert len(with_required) >= 90, (
        f"only {len(with_required)} cases declare a required skill; the gate has "
        "little left to grade"
    )


@pytest.mark.parametrize(
    ("source", "case"),
    [(src, case) for src, case in _corpus()],
    ids=[f"{src}:{case['id']}" for src, case in _corpus()],
)
def test_the_fast_path_never_skips_a_case_that_needs_a_skill(
    source: str, case: dict
) -> None:
    """The recall gate.

    A false skip is the expensive failure: the assistant runs the whole turn
    without the runbook it needed, and nothing in the response says so. Every
    case here declares the skills it requires, so any hit on a case with a
    non-empty `required` is a proven regression rather than a suspicion.
    """
    messages = [
        ChatMessage(role=m["role"], content=m.get("content"))
        for m in case.get("messages", [])
        if m["role"] in ("user", "assistant")
    ]
    if not messages:
        pytest.skip("case has no replayable messages")

    # A case with no `expect.required` key at all is a malformed case, not a
    # no-task case. Conflating them let a whole file lose its assertions to a
    # typo.
    expect = case.get("expect")
    assert isinstance(expect, dict) and "required" in expect, (
        f"{source}:{case['id']} has no expect.required — the gate cannot grade it"
    )
    required = expect["required"] or []

    fired = is_no_task_utterance(messages)
    if required:
        assert not fired, (
            f"{source}:{case['id']} requires {required} but the fast path skipped "
            "selection — the turn would run without its runbook"
        )


# ---------------------------------------------------------------------------
# Wiring: the fast path must CLEAR, and must yield to an explicit request.
# ---------------------------------------------------------------------------


def _request(text: str, **extra: object):  # type: ignore[no-untyped-def]
    from daintree_assistant_server.contracts.daintree_api import RespondRequest

    return RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": [{"role": "user", "content": text}]},
            "startup": {},
            **extra,
        }
    )


# ---------------------------------------------------------------------------
# The context gate: only when nothing is in progress.
# ---------------------------------------------------------------------------


def test_the_gate_fires_on_a_cold_greeting() -> None:
    assert can_skip_selection(_request("hey"), prior_active_ids=[]) is True


def test_the_gate_refuses_when_a_skill_is_already_active() -> None:
    """An acknowledgement is no evidence the work is over — the user may be
    thanking the assistant while three agents keep running."""
    assert can_skip_selection(_request("thanks"), prior_active_ids=["daintree.foundation"]) is False


@pytest.mark.parametrize(
    ("turn", "why"),
    [
        ({"is_wake": True}, "a background wake has no new user message"),
        ({"goal": "finish the refactor"}, "a stated goal is live work"),
        (
            {"workflow_state": [{"id": "wf", "goal": "g", "status": "active"}]},
            "a live workflow digest",
        ),
        ({"workflow_runs": ["run_1"]}, "an active workflow run"),
        ({"async_operations": ["asy_1 build"]}, "async work in flight"),
        ({"resumed_watchers": ["w_1"]}, "a resumed watcher"),
    ],
)
def test_the_gate_refuses_while_anything_is_in_flight(turn: dict, why: str) -> None:
    assert can_skip_selection(_request("thanks", turn=turn), prior_active_ids=[]) is False, why


def test_the_gate_refuses_when_selection_is_forced() -> None:
    forced = _request("hey", selection={"force": True})
    assert can_skip_selection(forced, prior_active_ids=[]) is False
    always = _request("hey", selection={"policy": "always"})
    assert can_skip_selection(always, prior_active_ids=[]) is False


@pytest.mark.asyncio
async def test_a_greeting_skips_the_selector_and_clears_the_active_set(
    settings, fake_provider
) -> None:  # type: ignore[no-untyped-def]
    """The whole point: no model call, and a finished workflow does not follow
    the user into an unrelated conversation."""
    from pydantic import SecretStr

    from daintree_assistant_server.main import build_container

    container = build_container(settings, fake_provider)
    outcome = await container.orchestrator.complete(
        _request("thanks!"),

        api_key=SecretStr("k"),
        request_id="req_fp",
    )
    assert outcome.body is not None
    assert outcome.body["skills"]["active"] == []
    # No selector call was made — only the main completion.
    assert fake_provider.selection_requests == []
    await container.aclose()


def test_an_authoritative_clear_actually_clears() -> None:
    """The bug this disposition exists to fix.

    `resolve_active_skills` treated a decision as authoritative only when
    `ran` was true, so the fast path's empty result silently REUSED the prior
    set — the exact opposite of the clear it advertised, and invisible because
    the earlier wiring test supplied no prior set to reuse.
    """
    from daintree_assistant_server.services.chat_orchestrator import resolve_active_skills
    from daintree_assistant_server.skills.catalog import load_catalog
    from daintree_assistant_server.skills.selector import SelectionDecision

    catalog = load_catalog()
    prior = [sorted(catalog.known_ids)[0]]

    cleared = resolve_active_skills(SelectionDecision.cleared(), prior, catalog, 3)
    assert cleared.active == [], "an authoritative clear reused the prior set"

    # A plain non-run decision still reuses — that is the continuation-round rule.
    reused = resolve_active_skills(SelectionDecision.reuse(prior), prior, catalog, 3)
    assert reused.active == prior


def test_a_cleared_decision_does_not_claim_a_selector_call() -> None:
    """Telemetry stays honest: no call ran, so `ran` must stay False."""
    from daintree_assistant_server.skills.selector import SelectionDecision

    decision = SelectionDecision.cleared()
    assert decision.ran is False
    assert decision.degraded is False
    assert decision.authoritative is True
    assert decision.calls == 0


@pytest.mark.asyncio
async def test_selection_force_still_runs_the_selector(settings, fake_provider) -> None:  # type: ignore[no-untyped-def]
    """A client asking for selection gets selection, whatever the text looks like.

    Selection is LOCAL now, so the observable is the selector itself — the
    provider sees nothing on this path by design (that is the whole point of
    the local classifier), and asserting on provider traffic would test the
    architecture this replaced.
    """
    from pydantic import SecretStr

    from daintree_assistant_server.contracts.daintree_api import RespondRequest
    from daintree_assistant_server.main import build_container

    container = build_container(settings, fake_provider)
    calls: list[list[str]] = []
    real_select = container.orchestrator.selector.select

    async def spying_select(messages, active_ids, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(active_ids))
        return await real_select(messages, active_ids, **kwargs)

    container.orchestrator.selector.select = spying_select  # type: ignore[method-assign]
    body = RespondRequest.model_validate(
        {
            "protocol_version": 2,
            "session": {"id": "s", "turn_id": "t"},
            "input": {"messages": [{"role": "user", "content": "thanks!"}]},
            "startup": {},
            "selection": {"force": True},
        }
    )
    await container.orchestrator.complete(
        body, api_key=SecretStr("k"), request_id="req_fp2"
    )
    assert calls, "selection.force was ignored"
    await container.aclose()
