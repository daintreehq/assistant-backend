"""What may be written down about a paying account, and what may not.

This object exists to travel INTO the paid path and end up beside a cost figure in
a log line, so the interesting property is not what it carries but what its
default projections REFUSE to carry. The raw subject is present — durable
accounting will need a real key to join on, and a truncated hash is a support
correlation id rather than a primary key — and every path that writes something
out has to reach past it deliberately.
"""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from uuid import UUID

from daintree_assistant_server.auth import subject_hash
from daintree_assistant_server.services.account_context import AccountContext

_SUBJECT = UUID("8f14e45f-ea8b-4c1d-9b2a-0000feedface")


def _context(**overrides: object) -> AccountContext:
    base: dict[str, object] = {
        "subject_hash": subject_hash(str(_SUBJECT)),
        "request_id": "req-1",
        "oauth_client_id": "daintree-assistant",
        "plan_id": "standard",
        "entitlement_source": "polar",
        "entitlement_stale": False,
    }
    base.update(overrides)
    return AccountContext(**base)  # type: ignore[arg-type]


def test_the_log_projection_carries_only_pseudonymous_fields() -> None:
    context = _context()
    assert context.log_fields() == {
        "account_subject_hash": subject_hash(str(_SUBJECT)),
        "account_client_id": "daintree-assistant",
        "account_plan_id": "standard",
        "account_entitlement_source": "polar",
        "account_entitlement_stale": False,
    }


def test_the_raw_subject_never_reaches_the_log_projection() -> None:
    """The rule the whole class is shaped around."""
    rendered = json.dumps(_context().log_fields())
    assert str(_SUBJECT) not in rendered
    assert "user_id" not in rendered


def test_the_raw_subject_never_reaches_repr() -> None:
    """`repr` is what a traceback, a debugger and a naive log call all reach for."""
    assert str(_SUBJECT) not in repr(_context())
    assert "user_id" not in repr(_context())


def test_even_a_full_serialization_cannot_produce_an_identifier() -> None:
    """The guarantee stated as a property of the TYPE, not of its ``repr``.

    A ``repr=False`` field looks like a safety measure and is not one:
    ``dataclasses.asdict``, ``vars`` and FastAPI's own dataclass encoder all walk
    straight past it. So the raw subject is not carried at all, and this asserts
    that through the most permissive serializer available rather than through the
    projection that was designed to be safe.
    """
    for rendered in (
        json.dumps(asdict(_context()), default=str),
        json.dumps(vars(_context()), default=str),
        repr(_context()),
        str(_context()),
    ):
        assert str(_SUBJECT) not in rendered


def test_there_is_nowhere_to_put_an_identifier_a_credential_or_an_email() -> None:
    """Not an assertion about a value — an assertion about the SHAPE.

    A future caller cannot attach a raw subject, a bearer or an email to this
    object by accident, because the type simply has no field for one. When
    durable accounting needs a real key it gets an identity object of its own,
    designed so serializers refuse it — and whoever builds it has to say so.
    """
    names = {f.name for f in fields(AccountContext)}
    assert not names & {
        "user_id",
        "subject",
        "token",
        "bearer",
        "access_token",
        "email",
        "session_id",
    }


def test_a_new_field_is_not_logged_until_somebody_adds_it_to_the_projection() -> None:
    """A schema-change tripwire.

    ``log_fields`` is hand-written rather than a filtered dump precisely so that
    adding a field does not add a log key. This fails when the class grows,
    forcing the question rather than answering it silently.
    """
    assert {f.name for f in fields(AccountContext)} == {
        "subject_hash",
        "request_id",
        "oauth_client_id",
        "plan_id",
        "entitlement_source",
        "entitlement_stale",
    }, "a field was added; decide deliberately whether it may be logged"


def test_it_is_immutable() -> None:
    context = _context()
    try:
        context.plan_id = "pro"  # type: ignore[misc]
    except Exception as exc:
        assert type(exc).__name__ == "FrozenInstanceError"
    else:  # pragma: no cover - the dataclass is frozen
        raise AssertionError("AccountContext should be frozen")


def test_an_unattributed_shape_is_expressible() -> None:
    """Under entitlement `off` there is a caller and no plan.

    None means "not looked up" and is deliberately not the same as a plan named
    "none" — a cost line must not imply a verdict nobody asked for.
    """
    context = AccountContext(subject_hash="a" * 16, request_id="req-1")
    assert context.plan_id is None
    assert context.log_fields()["account_plan_id"] is None
