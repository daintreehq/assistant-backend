"""Strip server-controlled identity and credentials BEFORE anything is stored.

This runs at the capture boundary rather than at export, and the difference is
deliberate. Redaction (``redaction.py``) is a best-effort scan for secrets that
happen to appear inside user CONTENT, and it runs on the way OUT so the raw trace
stays a faithful record. What this module removes is different: three fields the
SERVER put there, whose presence in a trace is a defect rather than evidence.

**It scrubs exact paths, not key names anywhere.** That is the whole design, and
the reason is that both directions of the alternative are wrong:

* Dropping every key called ``state`` at any depth would delete a tool argument
  named ``state``, a workflow node's ``state``, a terminal's ``state`` — ordinary
  content, silently corrupted, in a trace whose only purpose is to be exact.
* Masking every ``id`` would gut a transcript full of legitimate ids.

**And it is not a guarantee about user content.** If a user pastes their own
session id into a message, it is captured, because the message is the record. The
raw trace is restricted for exactly that reason; ``redaction.py`` is what runs
before anything becomes a training derivative. The claim here is narrower and
checkable: the three fields the SERVER attached to this request never reach
storage. ``docs`` section 7.29 lists them.
"""

from __future__ import annotations

from typing import Any

#: Dropped outright from the TOP LEVEL of a request. The signed state token is a
#: replayable credential and must never be written down.
_DROP_TOP_LEVEL = ("state",)

#: Masked under ``session`` only. Their PRESENCE is analytically useful; their
#: VALUE is exactly what the header's HMAC pseudonym exists to hide.
_MASK_UNDER_SESSION = ("id", "turn_id")

MASKED = "<pseudonymised>"


def scrub_request(value: Any) -> Any:
    """Return a copy of a dumped ``RespondRequest`` without server identity.

    Deliberately NOT recursive over the whole tree: it touches three known paths
    and copies everything else through untouched.
    """
    if not isinstance(value, dict):
        return value

    out = {k: v for k, v in value.items() if k not in _DROP_TOP_LEVEL}

    session = out.get("session")
    if isinstance(session, dict):
        out["session"] = {
            k: (MASKED if k in _MASK_UNDER_SESSION else v) for k, v in session.items()
        }
    return out
