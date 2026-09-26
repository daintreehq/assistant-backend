"""What each MCP session has already been sent in full, so it is not sent again.

A 2025-era client opens with ``initialize``; the transport answers it with an
``Mcp-Session-Id``, and the client echoes that id on every later request. The only
thing remembered under it is which supporting runbooks (the foundation, above all)
this session has already received in full, by content hash: the model keeps them
in its context, so a later search names them instead of resending ~5 KB each.

This is an optimisation, never a dependency, and every gap in it errs toward
sending too much:

- An id this process never issued (a sibling Cloud Run instance issued it, or this
  one restarted) is adopted as a fresh session, so its first search sends
  everything. No request is ever refused for its session id.
- A 2026-07-28 client has no handshake and no session, so it always gets
  everything.
- Memory is bounded: sessions are evicted least-recently-used past
  ``max_sessions``, and dropped after ``idle_ttl_s`` without a request. An evicted
  session starts over, which again only means resending.
- A caller that lost the text anyway (compaction, a subagent sharing the
  connection) is told in the entry itself to fetch it with ``get_runbook``.
"""

from __future__ import annotations

import secrets
import time
from collections import OrderedDict
from collections.abc import Callable

#: The spec limits a session id to visible ASCII; anything longer than this is
#: not one we would have issued, and it is caller-controlled memory.
MAX_SESSION_ID_CHARS = 128
DEFAULT_MAX_SESSIONS = 4096
#: Longer than any one sitting with the assistant, short enough that an idle
#: client's entry does not outlive its process by days.
DEFAULT_IDLE_TTL_S = 12 * 60 * 60

#: runbook id -> content hash of the version this session was sent in full.
Delivered = dict[str, str]


def _valid(session_id: str) -> bool:
    return 0 < len(session_id) <= MAX_SESSION_ID_CHARS and all(
        "\x21" <= ch <= "\x7e" for ch in session_id
    )


class SessionMemory:
    def __init__(
        self,
        *,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        idle_ttl_s: float = DEFAULT_IDLE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_sessions = max_sessions
        self._idle_ttl_s = idle_ttl_s
        self._clock = clock
        self._sessions: OrderedDict[str, tuple[float, Delivered]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._sessions)

    def open(self) -> str:
        """Issue a new session id: every ``initialize`` starts a session afresh."""
        session_id = secrets.token_urlsafe(24)
        self._store(session_id, {})
        return session_id

    def delivered(self, session_id: str | None) -> Delivered | None:
        """The session's record, to read and extend in place; None for no session.

        An unknown but well-formed id is adopted rather than refused (see the
        module docstring); a malformed one is treated as no session at all.
        """
        if session_id is None or not _valid(session_id):
            return None
        now = self._clock()
        entry = self._sessions.get(session_id)
        record: Delivered = {} if entry is None or now - entry[0] > self._idle_ttl_s else entry[1]
        self._store(session_id, record, now)
        return record

    def _store(self, session_id: str, record: Delivered, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        self._sessions[session_id] = (now, record)
        self._sessions.move_to_end(session_id)
        # Oldest first: expired entries, then anything over the cap.
        while self._sessions:
            oldest_id, (seen, _) = next(iter(self._sessions.items()))
            if len(self._sessions) <= self._max_sessions and now - seen <= self._idle_ttl_s:
                break
            del self._sessions[oldest_id]
