"""The account response model's own rules.

The entitlement client validates the WEBSITE's contract; this validates OURS. The
brief asks for exactly that separation — strict limits on the response even though
the data came from a dependency we already checked — because the two boundaries
protect against different mistakes. The client guards against the website changing
under us; this guards against a future edit here assembling a body that is
internally incoherent.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from daintree_assistant_server.contracts.account import AccountResponse

_HASH = "0123456789abcdef"


def test_the_identity_only_shape_is_valid() -> None:
    body = AccountResponse(subject_hash=_HASH, access="unverified")
    assert body.version == 1
    assert body.plan_id is None


def test_unverified_cannot_carry_a_lookup_s_results() -> None:
    """``unverified`` means no lookup happened, so it cannot report one's answer.

    A client reading `access: unverified` beside `plan_id: standard` has been told
    two contradictory things about whether anybody asked.
    """
    with pytest.raises(ValidationError, match="cannot carry entitlement fields"):
        AccountResponse(subject_hash=_HASH, access="unverified", plan_id="standard")


def test_granted_requires_a_named_plan() -> None:
    """Access to something unnamed cannot later be attributed or capped."""
    with pytest.raises(ValidationError, match="requires a plan_id"):
        AccountResponse(subject_hash=_HASH, access="granted")


def test_a_naive_timestamp_is_refused() -> None:
    """It would serialize without an offset and be read in whatever zone the
    client assumes — silently shifting how fresh a billing answer looks."""
    with pytest.raises(ValidationError):
        AccountResponse(
            subject_hash=_HASH,
            access="granted",
            plan_id="standard",
            checked_at=datetime(2026, 8, 25, 12, 0, 0),  # noqa: DTZ001 - the point
        )


def test_the_subject_hash_shape_is_enforced() -> None:
    """It is the one identifier that travels, and the CLI's format is 16 lowercase
    hex characters. A raw subject would not match it — which is the point."""
    for bad in ("", "not-hex", "ABCDEF0123456789", "0123456789abcde"):
        with pytest.raises(ValidationError):
            AccountResponse(subject_hash=bad, access="unverified")


def test_an_unknown_access_value_is_refused() -> None:
    with pytest.raises(ValidationError):
        AccountResponse(subject_hash=_HASH, access="probably")  # type: ignore[arg-type]


def test_an_unknown_plan_is_refused() -> None:
    with pytest.raises(ValidationError):
        AccountResponse(
            subject_hash=_HASH,
            access="granted",
            plan_id="enterprise",  # type: ignore[arg-type]
        )


def test_an_unexpected_field_is_a_construction_error() -> None:
    """`extra="forbid"`: a field arriving here by accident should fail here, not
    surprise somebody's terminal."""
    with pytest.raises(ValidationError):
        AccountResponse(subject_hash=_HASH, access="unverified", seats=4)  # type: ignore[call-arg]


def test_the_full_granted_shape_round_trips() -> None:
    body = AccountResponse(
        email="person@example.com",
        subject_hash=_HASH,
        access="granted",
        plan_id="pro",
        subscription_status="active",
        entitlement_source="cache",
        entitlement_stale=True,
        checked_at=datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC),
    )
    dumped = body.model_dump(mode="json", exclude_none=True)
    assert dumped["checked_at"] == "2026-08-25T12:00:00Z"
    # `False` is not `None`; a stale flag must survive `exclude_none`.
    assert AccountResponse(
        subject_hash=_HASH, access="granted", plan_id="pro", entitlement_stale=False
    ).model_dump(exclude_none=True)["entitlement_stale"] is False
