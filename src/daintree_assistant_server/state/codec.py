"""Signed, opaque skill-state token codec.

The token is portable across Cloud Run instances: it carries the active skill
set and selector cadence markers so request correctness never depends on
instance memory. It is signed (HMAC-SHA256) with a dedicated key — not the
public API key or the upstream key — and is *not* encrypted (it contains only
IDs and hashes, no bodies or conversation content). It remains opaque so the
format can evolve.

Wire format::

    dst1.<base64url(canonical-json)>.<base64url(hmac-sha256)>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Iterable

from ..errors import request_too_large
from ..text_utils import utf8_len
from .models import StatePayload, StateVerification

TOKEN_VERSION = 1
TOKEN_PREFIX = "dst1"
# Defensive cap so a junk token never triggers expensive parsing.
MAX_TOKEN_BYTES = 16 * 1024

# Warning codes surfaced when a supplied token is unusable.
REASON_INVALID = "invalid_skill_state"
REASON_EXPIRED = "expired_skill_state"
REASON_SESSION_MISMATCH = "skill_state_session_mismatch"
REASON_UNSUPPORTED_VERSION = "unsupported_skill_state_version"


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class StateCodec:
    """Issues and verifies skill-state tokens using a small key ring."""

    def __init__(
        self,
        *,
        current_key: bytes,
        current_kid: str,
        salt: bytes,
        ttl_seconds: int,
        previous_keys: dict[str, bytes] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not current_key:
            raise ValueError("current signing key must be non-empty")
        self._current_key = current_key
        self._current_kid = current_kid
        self._keys: dict[str, bytes] = {current_kid: current_key}
        if previous_keys:
            for kid, key in previous_keys.items():
                if kid and key:
                    self._keys.setdefault(kid, key)
        self._salt = salt
        self._ttl = ttl_seconds
        self._clock = clock

    # ---- helpers --------------------------------------------------------

    def _session_hash(self, session_id: str | None) -> str:
        if not session_id:
            return ""
        return hashlib.sha256(session_id.encode("utf-8") + self._salt).hexdigest()

    def _signature(self, key: bytes, body: str) -> str:
        mac = hmac.new(key, f"{TOKEN_PREFIX}.{body}".encode(), hashlib.sha256)
        return _b64url_encode(mac.digest())

    # ---- issue ----------------------------------------------------------

    def issue(
        self,
        *,
        session_id: str | None,
        active_skills: Iterable[str],
        catalog_revision: str,
        turn_id: str | None,
        instruction_revision: int,
        docs_query: str | None = None,
    ) -> str:
        now = int(self._clock())
        payload = {
            "v": TOKEN_VERSION,
            "kid": self._current_kid,
            "session_hash": self._session_hash(session_id),
            "active_skills": [{"id": sid} for sid in active_skills],
            "catalog_revision": catalog_revision,
            "last_turn_id": turn_id,
            "last_instruction_revision": instruction_revision,
            "issued_at": now,
            "expires_at": now + self._ttl,
        }
        # Omitted when absent rather than written as null: the overwhelming
        # majority of turns are not documentation questions, and a key present on
        # every token would grow every token for the rare case.
        if docs_query:
            payload["docs_query"] = docs_query
        body = _b64url_encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = self._signature(self._current_key, body)
        return f"{TOKEN_PREFIX}.{body}.{signature}"

    # ---- verify ---------------------------------------------------------

    def verify(self, token: str | None, *, session_id: str | None) -> StateVerification:
        if not token:
            return StateVerification(valid=False, payload=None, reason=None)
        if utf8_len(token) > MAX_TOKEN_BYTES:
            raise request_too_large(
                "Skill-state token exceeds the size limit.", param="daintree.state"
            )

        parts = token.split(".")
        if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
            return StateVerification(valid=False, payload=None, reason=REASON_INVALID)
        _, body, signature = parts
        # Both segments are base64url — pure ASCII by construction. Rejecting anything
        # else here keeps every downstream operation total: `.encode("utf-8")` on the
        # signature raises UnicodeEncodeError for an unpaired surrogate, which would be
        # a 500 for what must simply be an ignored token.
        if not body.isascii() or not signature.isascii():
            return StateVerification(valid=False, payload=None, reason=REASON_INVALID)

        try:
            payload_bytes = _b64url_decode(body)
            data = json.loads(payload_bytes)
        except (ValueError, json.JSONDecodeError):
            return StateVerification(valid=False, payload=None, reason=REASON_INVALID)
        if not isinstance(data, dict):
            return StateVerification(valid=False, payload=None, reason=REASON_INVALID)

        kid = data.get("kid")
        key = self._keys.get(kid) if isinstance(kid, str) else None
        if key is None:
            return StateVerification(valid=False, payload=None, reason=REASON_INVALID)

        # Compare BYTES: hmac.compare_digest raises TypeError on str operands holding
        # non-ASCII, and the signature here is attacker-controlled — a token carrying
        # one non-ASCII byte would 500 instead of being ignored as invalid, which the
        # API contract and CLAUDE.md both promise it is.
        expected = self._signature(key, body)
        if not hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8")):
            return StateVerification(valid=False, payload=None, reason=REASON_INVALID)

        try:
            payload = StatePayload.model_validate(data)
        except Exception:
            return StateVerification(valid=False, payload=None, reason=REASON_INVALID)

        if payload.v != TOKEN_VERSION:
            return StateVerification(
                valid=False, payload=payload, reason=REASON_UNSUPPORTED_VERSION
            )

        now = int(self._clock())
        if payload.expires_at <= now:
            return StateVerification(valid=False, payload=payload, reason=REASON_EXPIRED)

        # Strict session binding: a token's recorded session hash must match the
        # hash for the supplied session_id. A session-bound token replayed with a
        # missing/different session (expected hash "") is a mismatch, so it cannot
        # carry one session's active skills into another.
        if payload.session_hash != self._session_hash(session_id):
            return StateVerification(valid=False, payload=payload, reason=REASON_SESSION_MISMATCH)

        return StateVerification(valid=True, payload=payload, reason=None)
