"""State token codec tests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.state.codec import (
    REASON_EXPIRED,
    REASON_INVALID,
    REASON_SESSION_MISMATCH,
    REASON_UNSUPPORTED_VERSION,
    TOKEN_PREFIX,
    TOKEN_VERSION,
    StateCodec,
)


def _codec(clock_value: list[float], **kwargs: object) -> StateCodec:
    return StateCodec(
        current_key=b"current-key",
        current_kid="k1",
        salt=b"salt",
        ttl_seconds=100,
        clock=lambda: clock_value[0],
        **kwargs,  # type: ignore[arg-type]
    )


def test_issue_and_verify_roundtrip() -> None:
    clock = [1000.0]
    codec = _codec(clock)
    token = codec.issue(
        session_id="ses1",
        active_runbooks=["a", "b"],
        catalog_revision="sha256:rev",
        turn_id="t1",
        instruction_revision=2,
    )
    assert token.startswith("dst1.")
    assert len(token.split(".")) == 3

    result = codec.verify(token, session_id="ses1")
    assert result.valid is True
    assert result.payload is not None
    assert result.payload.active_ids() == ["a", "b"]
    assert result.payload.catalog_revision == "sha256:rev"
    assert result.payload.last_turn_id == "t1"
    assert result.payload.last_instruction_revision == 2


def test_no_token_is_not_an_error() -> None:
    codec = _codec([1000.0])
    result = codec.verify(None, session_id="ses1")
    assert result.valid is False
    assert result.reason is None


def test_session_mismatch() -> None:
    codec = _codec([1000.0])
    token = codec.issue(
        session_id="ses1", active_runbooks=[], catalog_revision="r", turn_id="t", instruction_revision=0
    )
    result = codec.verify(token, session_id="other")
    assert result.valid is False
    assert result.reason == REASON_SESSION_MISMATCH


def test_tampered_signature() -> None:
    codec = _codec([1000.0])
    token = codec.issue(
        session_id="s", active_runbooks=[], catalog_revision="r", turn_id="t", instruction_revision=0
    )
    tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    result = codec.verify(tampered, session_id="s")
    assert result.valid is False
    assert result.reason == REASON_INVALID


def test_expired_token() -> None:
    clock = [1000.0]
    codec = _codec(clock)
    token = codec.issue(
        session_id="s", active_runbooks=[], catalog_revision="r", turn_id="t", instruction_revision=0
    )
    clock[0] = 2000.0
    result = codec.verify(token, session_id="s")
    assert result.valid is False
    assert result.reason == REASON_EXPIRED


def test_unknown_kid() -> None:
    codec = _codec([1000.0])
    token = codec.issue(
        session_id="s", active_runbooks=[], catalog_revision="r", turn_id="t", instruction_revision=0
    )
    other = StateCodec(
        current_key=b"different", current_kid="kX", salt=b"salt", ttl_seconds=100,
        clock=lambda: 1000.0,
    )
    result = other.verify(token, session_id="s")
    assert result.valid is False
    assert result.reason == REASON_INVALID


def test_key_rotation_previous_key_verifies() -> None:
    old = StateCodec(current_key=b"old-key", current_kid="k0", salt=b"salt", ttl_seconds=100,
                     clock=lambda: 1000.0)
    old_token = old.issue(
        session_id="s", active_runbooks=["a"], catalog_revision="r", turn_id="t",
        instruction_revision=0,
    )
    rotated = StateCodec(
        current_key=b"new-key", current_kid="k1", salt=b"salt", ttl_seconds=100,
        previous_keys={"k0": b"old-key"}, clock=lambda: 1000.0,
    )
    result = rotated.verify(old_token, session_id="s")
    assert result.valid is True
    # New tokens are signed with the current key.
    new_token = rotated.issue(
        session_id="s", active_runbooks=[], catalog_revision="r", turn_id="t", instruction_revision=0
    )
    assert ".k1." not in new_token  # kid is in the payload, not the wire prefix
    assert old.verify(new_token, session_id="s").valid is False  # old ring can't verify k1


def test_malformed_token_shapes() -> None:
    codec = _codec([1000.0])
    assert codec.verify("garbage", session_id="s").reason == REASON_INVALID
    assert codec.verify("dst1.notbase64!.sig", session_id="s").reason == REASON_INVALID
    assert codec.verify("wrong.prefix.sig", session_id="s").reason == REASON_INVALID


def test_oversize_token_raises_413() -> None:
    codec = _codec([1000.0])
    huge = "dst1." + ("a" * 20000) + ".sig"
    with pytest.raises(DaintreeError) as exc:
        codec.verify(huge, session_id="s")
    assert exc.value.status_code == 413


def test_session_bound_token_replayed_without_session_is_mismatch() -> None:
    codec = _codec([1000.0])
    token = codec.issue(
        session_id="ses1", active_runbooks=["a"], catalog_revision="r", turn_id="t",
        instruction_revision=0,
    )
    # A session-bound token verified with no session_id must NOT validate.
    result = codec.verify(token, session_id=None)
    assert result.valid is False
    assert result.reason == REASON_SESSION_MISMATCH


def test_stateless_token_roundtrips_without_session() -> None:
    codec = _codec([1000.0])
    token = codec.issue(
        session_id=None, active_runbooks=[], catalog_revision="r", turn_id="t",
        instruction_revision=0,
    )
    assert codec.verify(token, session_id=None).valid is True


def test_codec_requires_current_key() -> None:
    with pytest.raises(ValueError):
        StateCodec(current_key=b"", current_kid="k1", salt=b"s", ttl_seconds=100)


def test_non_ascii_signature_is_ignored_not_an_error() -> None:
    """A malformed token is IGNORED, never a 500 — the documented contract.

    ``hmac.compare_digest`` raises TypeError on str operands holding non-ASCII, and
    ``.encode("utf-8")`` raises UnicodeEncodeError on an unpaired surrogate. Both
    are reachable from the caller-supplied token, so the base64url segments are
    checked for ASCII before either runs.
    """
    codec = _codec([1000.0])
    good = codec.issue(
        session_id="s1",
        active_runbooks=["a"],
        catalog_revision="rev",
        turn_id="t1",
        instruction_revision=0,
    )
    stem = good.rsplit(".", 1)[0]
    for bad_sig in ["\u00e9" * 8, "\ud800", "sig\u2028x"]:
        result = codec.verify(f"{stem}.{bad_sig}", session_id="s1")
        assert result.valid is False
        assert result.reason == REASON_INVALID


def test_non_ascii_body_is_ignored_not_an_error() -> None:
    codec = _codec([1000.0])
    result = codec.verify("dst1.b\u00f3dy.sig", session_id="s1")
    assert result.valid is False
    assert result.reason == REASON_INVALID


def test_session_hash_is_a_stable_salted_pseudonym() -> None:
    # The orchestrator derives the upstream cache-affinity key from this, and that
    # key leaves the deployment. Two properties carry that use: the raw client id
    # must not survive the hash, and two instances sharing the salt must agree —
    # a per-instance value would give one session a different key per instance and
    # defeat the affinity it exists to create.
    clock = [1000.0]
    digest = _codec(clock).session_hash("ses1")
    assert digest == _codec(clock).session_hash("ses1")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
    assert "ses1" not in digest


def test_session_hash_separates_sessions_and_salts() -> None:
    # Distinct sessions must not collide onto one key (that would be a shared
    # cohort, not affinity), and the salt must actually participate — an unsalted
    # digest of a client-supplied id is reversible by anyone who can guess it.
    clock = [1000.0]
    a = _codec(clock).session_hash("ses1")
    b = _codec(clock).session_hash("ses2")
    assert a != b
    other_salt = StateCodec(
        current_key=b"current-key",
        current_kid="k1",
        salt=b"different-salt",
        ttl_seconds=100,
        clock=lambda: clock[0],
    )
    assert other_salt.session_hash("ses1") != a


def test_session_hash_of_no_session_is_empty() -> None:
    # Empty rather than a digest of "": a sessionless caller must get NO affinity
    # key, not one shared pseudonym that would herd every such caller onto a
    # single endpoint.
    codec = _codec([1000.0])
    assert codec.session_hash(None) == ""
    assert codec.session_hash("") == ""


def _resign(codec: StateCodec, payload: dict[str, object]) -> str:
    """Re-sign an arbitrary payload with the codec's own key.

    The point is a token that is CRYPTOGRAPHICALLY VALID and semantically stale —
    the only shape that reaches the version check at all. A hand-mangled token is
    rejected by the MAC long before its `v` is ever read, so it proves nothing
    about version handling.
    """
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = hmac.new(b"current-key", f"{TOKEN_PREFIX}.{body}".encode(), hashlib.sha256)
    return f"{TOKEN_PREFIX}.{body}.{_b64(mac.digest())}"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _payload_at(codec: StateCodec, **overrides: object) -> dict[str, object]:
    token = codec.issue(
        session_id="ses1",
        active_runbooks=["a"],
        catalog_revision="sha256:rev",
        turn_id="t1",
        instruction_revision=0,
    )
    body = token.split(".")[1]
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload.update(overrides)
    return payload


def test_a_validly_signed_superseded_token_is_refused_for_its_version() -> None:
    """The 1 -> 2 bump is what retires a token, and it must be the stated reason.

    A v1 payload names `active_skills`, a field this codec no longer reads. If such
    a token were accepted, the turn would silently continue with an EMPTY active set
    while reporting that state was carried — worse than a fresh selection, because
    nothing would say the runbooks had been dropped. Rejecting on the version is
    what turns that into an ordinary re-selection.
    """
    clock = [1000.0]
    codec = _codec(clock)
    payload = _payload_at(codec, v=1)
    payload["active_skills"] = [{"id": "a"}]
    payload.pop("active_runbooks", None)

    result = codec.verify(_resign(codec, payload), session_id="ses1")

    assert result.valid is False
    assert result.reason == REASON_UNSUPPORTED_VERSION
    # And nothing from the stale token leaks into the turn.
    assert result.payload is None or result.payload.active_ids() == []


def test_a_validly_signed_future_token_is_refused_for_its_version() -> None:
    """The check is an equality, not a floor: a NEWER token is equally unusable."""
    clock = [1000.0]
    codec = _codec(clock)
    token = _resign(codec, _payload_at(codec, v=TOKEN_VERSION + 1))

    result = codec.verify(token, session_id="ses1")

    assert result.valid is False
    assert result.reason == REASON_UNSUPPORTED_VERSION


def test_the_current_version_is_what_issue_actually_stamps() -> None:
    """Guards the pair that the two tests above depend on being real."""
    clock = [1000.0]
    codec = _codec(clock)
    assert _payload_at(codec)["v"] == TOKEN_VERSION
    assert TOKEN_VERSION == 2, "token schema bumped; update this literal deliberately"


# ---- announced runbooks ---------------------------------------------------


def test_announced_runbooks_roundtrip() -> None:
    """The opener gate's memory. Without it every round re-announces."""
    codec = _codec([1000.0])
    token = codec.issue(
        session_id="ses1",
        active_runbooks=["a"],
        catalog_revision="sha256:rev",
        turn_id="t1",
        instruction_revision=1,
        announced_runbooks=["a", "b"],
    )
    result = codec.verify(token, session_id="ses1")
    assert result.payload is not None
    assert result.payload.announced_runbooks == ["a", "b"]


def test_a_token_that_announced_nothing_omits_the_key() -> None:
    """Most turns announce nothing; they should not pay a key to say so.

    Also pins that a token issued before this field existed still verifies — the
    default is the empty list, so an older token simply re-arms every opener,
    which is the correct reading of "we have no record".
    """
    codec = _codec([1000.0])
    token = codec.issue(
        session_id="ses1",
        active_runbooks=["a"],
        catalog_revision="sha256:rev",
        turn_id="t1",
        instruction_revision=1,
    )
    raw = token.split(".")[1]
    body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert "announced_runbooks" not in body
    result = codec.verify(token, session_id="ses1")
    assert result.payload is not None
    assert result.payload.announced_runbooks == []


def test_announced_runbooks_are_not_filtered_against_the_active_set() -> None:
    """It records what the USER was told, not what is loaded.

    A runbook that announced and then dropped out of the active set must stay
    recorded, or re-selecting it three turns later announces it a second time.
    """
    codec = _codec([1000.0])
    token = codec.issue(
        session_id="ses1",
        active_runbooks=["still-loaded"],
        catalog_revision="sha256:rev",
        turn_id="t1",
        instruction_revision=1,
        announced_runbooks=["still-loaded", "long-gone"],
    )
    result = codec.verify(token, session_id="ses1")
    assert result.payload is not None
    assert "long-gone" in result.payload.announced_runbooks
    assert result.payload.active_ids() == ["still-loaded"]
