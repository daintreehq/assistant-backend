"""State token codec tests."""

from __future__ import annotations

import pytest

from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.state.codec import (
    REASON_EXPIRED,
    REASON_INVALID,
    REASON_SESSION_MISMATCH,
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
        active_skills=["a", "b"],
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
        session_id="ses1", active_skills=[], catalog_revision="r", turn_id="t", instruction_revision=0
    )
    result = codec.verify(token, session_id="other")
    assert result.valid is False
    assert result.reason == REASON_SESSION_MISMATCH


def test_tampered_signature() -> None:
    codec = _codec([1000.0])
    token = codec.issue(
        session_id="s", active_skills=[], catalog_revision="r", turn_id="t", instruction_revision=0
    )
    tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    result = codec.verify(tampered, session_id="s")
    assert result.valid is False
    assert result.reason == REASON_INVALID


def test_expired_token() -> None:
    clock = [1000.0]
    codec = _codec(clock)
    token = codec.issue(
        session_id="s", active_skills=[], catalog_revision="r", turn_id="t", instruction_revision=0
    )
    clock[0] = 2000.0
    result = codec.verify(token, session_id="s")
    assert result.valid is False
    assert result.reason == REASON_EXPIRED


def test_unknown_kid() -> None:
    codec = _codec([1000.0])
    token = codec.issue(
        session_id="s", active_skills=[], catalog_revision="r", turn_id="t", instruction_revision=0
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
        session_id="s", active_skills=["a"], catalog_revision="r", turn_id="t",
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
        session_id="s", active_skills=[], catalog_revision="r", turn_id="t", instruction_revision=0
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
        session_id="ses1", active_skills=["a"], catalog_revision="r", turn_id="t",
        instruction_revision=0,
    )
    # A session-bound token verified with no session_id must NOT validate.
    result = codec.verify(token, session_id=None)
    assert result.valid is False
    assert result.reason == REASON_SESSION_MISMATCH


def test_stateless_token_roundtrips_without_session() -> None:
    codec = _codec([1000.0])
    token = codec.issue(
        session_id=None, active_skills=[], catalog_revision="r", turn_id="t",
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
        active_skills=["a"],
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
