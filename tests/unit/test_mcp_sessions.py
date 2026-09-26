"""The per-session record of supporting runbooks already sent: bounded, and forgetful
in the direction that only costs bytes."""

from __future__ import annotations

from daintree_assistant_server.mcp_sessions import MAX_SESSION_ID_CHARS, SessionMemory


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_issued_ids_are_distinct_and_start_empty() -> None:
    memory = SessionMemory()
    first, second = memory.open(), memory.open()
    assert first != second
    assert memory.delivered(first) == {}


def test_the_record_persists_across_requests_of_one_session() -> None:
    memory = SessionMemory()
    session = memory.open()
    record = memory.delivered(session)
    assert record is not None
    record["daintree.foundation"] = "sha256:a"
    assert memory.delivered(session) == {"daintree.foundation": "sha256:a"}


def test_no_id_or_a_malformed_one_is_no_session() -> None:
    memory = SessionMemory()
    for bad in (None, "", "has space", "x" * (MAX_SESSION_ID_CHARS + 1), "café"):
        assert memory.delivered(bad) is None, bad
    assert len(memory) == 0


def test_an_unknown_id_is_adopted_empty() -> None:
    memory = SessionMemory()
    assert memory.delivered("issued-by-a-sibling") == {}
    assert len(memory) == 1


def test_sessions_are_evicted_least_recently_used_past_the_cap() -> None:
    memory = SessionMemory(max_sessions=2)
    a, b = memory.open(), memory.open()
    record_a = memory.delivered(a)
    assert record_a is not None
    record_a["x"] = "h"  # touching `a` makes `b` the oldest
    memory.open()
    assert len(memory) == 2
    assert memory.delivered(a) == {"x": "h"}
    # `b` was evicted, so it comes back as a fresh session.
    assert memory.delivered(b) == {}


def test_an_idle_session_expires_and_starts_over() -> None:
    clock = Clock()
    memory = SessionMemory(idle_ttl_s=60, clock=clock)
    session = memory.open()
    record = memory.delivered(session)
    assert record is not None
    record["x"] = "h"
    clock.now = 59
    assert memory.delivered(session) == {"x": "h"}
    clock.now = 59 + 61
    assert memory.delivered(session) == {}


def test_expired_sessions_are_dropped_when_others_arrive() -> None:
    clock = Clock()
    memory = SessionMemory(idle_ttl_s=60, clock=clock)
    for _ in range(5):
        memory.open()
    clock.now = 120
    memory.open()
    assert len(memory) == 1
