"""The runbook confirmation on a real stream: what a client actually receives.

The gate's rules are unit-tested in ``tests/unit/test_runbook_confirmations.py``. What
is tested HERE is the half that only exists on the wire: that the line reaches
the client as a ``preamble`` event ahead of any model output, that the announce-
once rule survives a round trip through the SIGNED STATE TOKEN (the only place
the memory lives — there is no server-side session), and that the profiles and
cadences that must stay silent do.
"""

from __future__ import annotations

from typing import Any

from conftest import parse_sse, respond_body
from fakes import selection_result

#: A real packaged runbook with a real confirmation, so this suite grades the shipped
#: catalog rather than a fixture. If its confirmation is edited, this still passes —
#: the assertion reads the catalog.
RUNBOOK = "daintree.explore.repository"
#: A real packaged runbook with NO confirmation: it is the domain foundation, which
#: admission pairs in without a score. It is the natural negative case and it
#: cannot rot into a positive one without `test_runbook_confirmations.py` failing.
SILENT_RUNBOOK = "daintree.foundation"
#: A second scored runbook, used to displace the first. A topic shift replaces
#: the active set, so selecting this one evicts RUNBOOK — which is what makes a
#: later re-selection of RUNBOOK genuinely NEWLY loaded again.
OTHER_RUNBOOK = "daintree.terminals.rename"


def _confirmation_text(client: Any, runbook_id: str = RUNBOOK) -> str:
    runbook = client.app.state.container.catalog_manager.catalog.get(runbook_id)
    assert runbook is not None and runbook.confirmation
    return runbook.confirmation


def _turn(
    client: Any,
    auth: dict[str, str],
    **body_kwargs: Any,
) -> tuple[list[tuple[str | None, dict[str, Any]]], list[str]]:
    with client.stream(
        "POST",
        "/v1/daintree/respond",
        headers=auth,
        json=respond_body(
            [{"role": "user", "content": "how does the selector work"}],
            generation={"stream": True},
            **body_kwargs,
        ),
    ) as response:
        assert response.status_code == 200
        events = parse_sse("".join(response.iter_text()))
    return events, [name or "" for name, _ in events]


def test_a_confident_first_load_types_its_confirmation_before_any_output(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The whole feature, on the wire.

    Order is the point: the confirmation must precede every ``delta``, because its only
    value is filling the gap before the answer starts. It rides ``preamble`` so it
    inherits that event's display-only contract in the CLI — see the long comment
    at the emission site for why a second event type would be worse.
    """
    fake_provider.selection_result = selection_result([RUNBOOK], confidence=0.99)
    events, names = _turn(client, auth)

    assert names[0] == "meta", "meta carries the state the client needs first"
    assert names[-1] == "done"

    # PACED: the confirmation arrives as a run of `preamble` events typed out
    # word by word, and every one of them precedes the executor's first delta.
    # That ordering is the feature — the words fill the gap rather than
    # interleaving with the answer.
    frames = [data["content"] for name, data in events if name == "preamble"]
    assert len(frames) > 1, "the confirmation should be typed, not dumped"
    first_delta = names.index("delta")
    assert all(i < first_delta for i, n in enumerate(names) if n == "preamble")

    # CUMULATIVE, because the CLI REPLACES rather than appends. Word-deltas into
    # a replacing consumer would leave the reader looking at the final word alone.
    full = _confirmation_text(client)
    assert frames[-1] == full
    assert frames == sorted(frames, key=len), "frames must grow monotonically"
    for frame in frames:
        assert full.startswith(frame), f"{frame!r} is not a prefix of the text"


def test_a_load_below_the_floor_stays_quiet(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """Recall-biased admission is not confidence.

    A runbook joins the active set at p>=0.65 because a missing one costs more
    than a spare one. Announcing at that bar would put a confident sentence in
    front of the user on the strength of a coin flip.
    """
    fake_provider.selection_result = selection_result([RUNBOOK], confidence=0.70)
    _events, names = _turn(client, auth)

    assert "preamble" not in names
    assert names[-1] == "done"


def test_a_runbook_with_no_opener_stays_quiet(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    fake_provider.selection_result = selection_result([SILENT_RUNBOOK], confidence=1.0)
    _events, names = _turn(client, auth)

    assert "preamble" not in names


def test_the_same_runbook_announces_once_across_a_conversation(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The announce-once rule, carried by the signed state token.

    **The turn in the middle is load-bearing, and an earlier version of this test
    without it passed against a build with the memory disabled.** While a runbook
    stays in the active set, `newly_loaded_ids` alone keeps it quiet — nothing is
    newly loaded, so the gate is never consulted and any assertion here would be
    measuring the wrong mechanism. The announced set only earns its keep across
    EVICTION AND RETURN: turn two shifts topic and displaces the runbook, so turn
    three loads it fresh and would announce it a second time if the token had not
    remembered.

    Driving it through the endpoint is the only way to exercise that, because the
    memory is not in this process: Cloud Run holds no session state, so the third
    turn stays quiet only if the first turn's token recorded the announcement and
    each request handed the token back.
    """
    fake_provider.selection_result = selection_result([RUNBOOK], confidence=0.99)
    first_events, first_names = _turn(client, auth, turn_id="t1")
    assert "preamble" in first_names
    state = next(data for name, data in first_events if name == "meta")["state"]

    # Topic shift. A successful selection is authoritative and REPLACES the
    # active set, so RUNBOOK is evicted here.
    fake_provider.selection_result = selection_result([OTHER_RUNBOOK], confidence=0.99)
    second_events, _second_names = _turn(
        client, auth, turn_id="t2", instruction_revision=1, state=state
    )
    second_meta = next(data for name, data in second_events if name == "meta")
    assert RUNBOOK not in [r["id"] for r in second_meta["runbooks"]["active"]]
    state = second_meta["state"]

    # Back to the original topic: genuinely newly loaded, and still silent.
    fake_provider.selection_result = selection_result([RUNBOOK], confidence=0.99)
    third_events, third_names = _turn(
        client, auth, turn_id="t3", instruction_revision=2, state=state
    )
    third_meta = next(data for name, data in third_events if name == "meta")
    assert RUNBOOK in [r["id"] for r in third_meta["runbooks"]["newly_loaded"]], (
        "the runbook must actually reload, or this test proves nothing"
    )
    assert "preamble" not in third_names


def test_a_runbook_still_active_is_not_re_announced(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The cheaper half of the same rule, pinned separately because it is a
    DIFFERENT mechanism.

    A runbook carried over in the active set is not newly loaded, so the gate is
    never reached — no token memory required. Worth its own test so the one above
    cannot quietly start passing on this behaviour instead of the one it names.
    """
    fake_provider.selection_result = selection_result([RUNBOOK], confidence=0.99)
    first_events, first_names = _turn(client, auth, turn_id="t1")
    assert "preamble" in first_names
    state = next(data for name, data in first_events if name == "meta")["state"]

    second_events, second_names = _turn(
        client, auth, turn_id="t2", instruction_revision=1, state=state
    )
    second_meta = next(data for name, data in second_events if name == "meta")
    assert second_meta["runbooks"]["newly_loaded"] == []
    assert "preamble" not in second_names


def test_a_token_that_is_not_handed_back_re_announces(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The converse, so the test above cannot pass for the wrong reason.

    Without this, a bug that suppressed EVERY confirmation after the first turn — a
    stuck flag, a mis-scoped guard — would look identical to the announce-once
    rule working.
    """
    fake_provider.selection_result = selection_result([RUNBOOK], confidence=0.99)

    _first, first_names = _turn(client, auth, turn_id="t1")
    _second, second_names = _turn(client, auth, turn_id="t2", instruction_revision=1)

    assert "preamble" in first_names
    assert "preamble" in second_names


def test_a_sub_agent_turn_never_announces(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """Nobody is reading it.

    A sub-agent's output is consumed by the orchestrator, so there is no person
    for the line to reassure — and its narrowed menu means whatever it loads is a
    research procedure the user never asked for by name.
    """
    fake_provider.selection_result = selection_result([RUNBOOK], confidence=0.99)
    with client.stream(
        "POST",
        "/v1/daintree/respond",
        headers=auth,
        json={
            **respond_body(
                [{"role": "user", "content": "how does the selector work"}],
                generation={"stream": True},
            ),
            "profile": "subagent",
        },
    ) as response:
        assert response.status_code == 200
        names = [n or "" for n, _ in parse_sse("".join(response.iter_text()))]

    assert "preamble" not in names


def test_the_non_streaming_path_announces_nothing(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """There is no gap to fill.

    A non-streaming turn delivers one body when the answer is finished, so an
    confirmation would arrive at the same instant as the thing it was covering for.
    The body must carry no trace of one — the field does not exist on the wire
    contract, and this pins that it stays that way.
    """
    fake_provider.selection_result = selection_result([RUNBOOK], confidence=0.99)
    response = client.post(
        "/v1/daintree/respond",
        headers=auth,
        json=respond_body([{"role": "user", "content": "how does the selector work"}]),
    )
    assert response.status_code == 200
    assert "preamble" not in response.text
    assert _confirmation_text(client) not in response.text


def test_a_non_streaming_turn_does_not_SPEND_the_caption(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """Silent is not the same as spent, and conflating them loses the caption.

    The gate's decision is folded into the state token during preparation, which
    both response paths share. A non-streaming turn emits nothing — so if it
    still recorded the runbook as announced, the caption would be burned where
    nobody could see it, and the next STREAMING turn would stay silent about a
    runbook the user had never been told about. "Shown once" would quietly mean
    "shown at most once".

    Asserting the absence in the body (the test above) cannot catch this: both
    the correct and the broken build return byte-identical JSON. Only the token
    it hands back differs.
    """
    fake_provider.selection_result = selection_result([RUNBOOK], confidence=0.99)
    first = client.post(
        "/v1/daintree/respond",
        headers=auth,
        json=respond_body(
            [{"role": "user", "content": "how does the selector work"}], turn_id="t1"
        ),
    )
    assert first.status_code == 200
    state = first.json()["state"]
    assert state

    _events, names = _turn(
        client, auth, turn_id="t2", instruction_revision=1, state=state
    )
    assert "preamble" in names, (
        "a non-streaming turn spent the caption without showing it"
    )


def test_resolve_reissues_the_token_without_forgetting_what_was_announced(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """`/runbooks/resolve` shows no caption, but it REISSUES the token.

    That is the whole trap, and it shipped broken until a review caught it: the
    endpoint mints a fresh state token, so a reissue that dropped
    ``announced_runbooks`` would forget every caption the conversation had shown.
    The announce-once rule would then hold everywhere except behind an endpoint
    that never announces anything — and the symptom, a runbook re-announcing
    minutes later, points at the confirmation gate rather than at the endpoint that
    actually erased its memory.

    The action is ``clear``, and it has to be. A ``load`` leaves the runbook in
    the active set, so the following turn does not newly load it and stays quiet
    whatever the announced set says — a version of this test using ``load``
    passed with the fix reverted. Clearing forces the runbook to load fresh, so
    the announced set is the only thing that can keep it silent.
    """
    fake_provider.selection_result = selection_result([RUNBOOK], confidence=0.99)

    first_events, first_names = _turn(client, auth, turn_id="t1")
    assert "preamble" in first_names
    state = next(data for name, data in first_events if name == "meta")["state"]

    resolved = client.post(
        "/v1/daintree/runbooks/resolve",
        headers=auth,
        json={
            "protocol_version": 3,
            "session_id": "s",
            "action": "clear",
            "state": state,
        },
    )
    assert resolved.status_code == 200
    body = resolved.json()
    assert body["runbooks"]["active"] == [], "clear must empty the active set"
    reissued = body["state"]
    assert reissued and reissued != state

    # The runbook is no longer active, so this turn loads it FRESH — and must
    # still not re-announce it.
    second_events, second_names = _turn(
        client, auth, turn_id="t2", instruction_revision=1, state=reissued
    )
    newly = [
        r["id"]
        for name, data in second_events
        if name == "meta"
        for r in data["runbooks"]["newly_loaded"]
    ]
    assert RUNBOOK in newly, "the runbook must actually reload for this to test anything"
    assert "preamble" not in second_names


def test_a_near_miss_recovers_on_a_later_confident_turn(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """A runbook admitted below the floor keeps its caption for a later turn.

    This is the documented near-miss rule, and it was broken end-to-end: the
    orchestrator handed the gate the turn's NEWLY LOADED ids, and a runbook
    admitted at 0.80 stays active — so the later, confident turn found it absent
    from that list and the caption was suppressed permanently. The gate's own
    unit test could not see it, because the gate was doing the right thing with
    the wrong input.

    Turn one must not spend the caption either: the floor is a statement about
    certainty, so one vague mention of a topic may not burn the announcement for
    the turn that actually commits to it.
    """
    fake_provider.selection_result = selection_result([RUNBOOK], confidence=0.80)
    first_events, first_names = _turn(client, auth, turn_id="t1")
    assert "preamble" not in first_names
    state = next(data for name, data in first_events if name == "meta")["state"]

    # Same runbook, now confident. It is still ACTIVE, so it is not newly loaded.
    fake_provider.selection_result = selection_result([RUNBOOK], confidence=0.99)
    second_events, second_names = _turn(
        client, auth, turn_id="t2", instruction_revision=1, state=state
    )
    second_meta = next(data for name, data in second_events if name == "meta")
    assert second_meta["runbooks"]["newly_loaded"] == [], (
        "the runbook must still be active, or this tests the ordinary path instead"
    )
    assert "preamble" in second_names


def test_a_suppressed_runner_up_is_spent_and_does_not_announce_later(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """When two runbooks clear the floor, one speaks and the OTHER is spent.

    The unit test proves `choose_confirmation` reports both ids in `announced`; this
    proves those ids actually reach the signed token. A regression that stored
    only the WINNER would look identical on the turn itself — one caption, right
    text — and surface much later, when the runner-up is selected alone and
    announces work the user was already told about.
    """
    fake_provider.selection_result = selection_result(
        [RUNBOOK, OTHER_RUNBOOK], confidence=0.99
    )
    first_events, first_names = _turn(client, auth, turn_id="t1")
    assert "preamble" in first_names
    # The LAST frame is the whole text; the earlier ones are prefixes of it.
    shown = [d["content"] for n, d in first_events if n == "preamble"][-1]
    first_meta = next(d for n, d in first_events if n == "meta")
    active = [r["id"] for r in first_meta["runbooks"]["active"]]
    assert RUNBOOK in active and OTHER_RUNBOOK in active
    state = first_meta["state"]

    # Whichever of the two did NOT speak is the runner-up, and it was spent.
    quiet = OTHER_RUNBOOK if shown == _confirmation_text(client, RUNBOOK) else RUNBOOK

    # Clear, so the runner-up loads FRESH on the next turn rather than being
    # carried over — the only way its silence can be attributed to the token.
    resolved = client.post(
        "/v1/daintree/runbooks/resolve",
        headers=auth,
        json={"protocol_version": 3, "session_id": "s", "action": "clear", "state": state},
    )
    assert resolved.status_code == 200
    state = resolved.json()["state"]

    fake_provider.selection_result = selection_result([quiet], confidence=0.99)
    second_events, second_names = _turn(
        client, auth, turn_id="t2", instruction_revision=1, state=state
    )
    newly = [
        r["id"]
        for n, d in second_events
        if n == "meta"
        for r in d["runbooks"]["newly_loaded"]
    ]
    assert quiet in newly, "the runner-up must reload for this to test anything"
    assert "preamble" not in second_names


def test_typing_the_confirmation_does_not_delay_the_executor(
    client: Any, auth: dict[str, str], fake_provider: Any
) -> None:
    """The trap this design exists around, guarded by the clock.

    Pacing costs wall-clock. If the confirmation were emitted BEFORE the
    executor's open was started — which is where it originally sat, immediately
    after `meta` — every turn that showed one would be delayed by the whole
    pacing budget. The two must overlap: the open goes first, the words fill the
    gap it opens.

    Against a fake provider the open completes at once, so the flush path fires
    and the turn finishes far inside the budget. Move the emission back above
    `open_task` and there is no task to check, so the full budget is spent
    serially and this fails — which is exactly the regression it is here for.
    """
    import time

    from daintree_assistant_server.config import CONFIRMATION_PACING_SECONDS

    fake_provider.selection_result = selection_result([RUNBOOK], confidence=0.99)

    started = time.perf_counter()
    _events, names = _turn(client, auth)
    elapsed = time.perf_counter() - started

    assert "preamble" in names, "the confirmation must actually have been shown"
    assert elapsed < CONFIRMATION_PACING_SECONDS, (
        f"the turn took {elapsed:.2f}s against a fake provider — pacing is running "
        "ahead of the executor open instead of alongside it"
    )
