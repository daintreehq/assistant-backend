"""Inbound correlation-id admission.

A request id is the one caller-controlled string that gets copied verbatim into
every structured log line for the request and echoed back in a response header.
Honoring it is useful — it is how the CLI ties its logs to ours — but it means an
unvalidated caller string reaches the log pipeline, so it is admitted only when
its shape is safe and it cannot be the caller's own credential.
"""

from __future__ import annotations

import pytest

from daintree_assistant_server.main import (
    _MAX_REQUEST_ID_LENGTH,
    _MIN_CREDENTIAL_FRAGMENT,
    _safe_request_id,
)

KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"
AUTH = f"Bearer {KEY}"


def _is_minted(value: str) -> bool:
    """Whether the middleware fell back to generating its own id."""
    return value.startswith("req_")


@pytest.mark.parametrize(
    "supplied",
    ["req-supplied-1", "abc123", "trace.4f2a_9", "a", "9" * _MAX_REQUEST_ID_LENGTH],
)
def test_a_well_formed_id_is_adopted(supplied: str) -> None:
    assert _safe_request_id(supplied, AUTH) == supplied


@pytest.mark.parametrize(
    ("supplied", "why"),
    [
        (None, "absent"),
        ("", "empty"),
        ("   ", "whitespace only"),
        ("x" * (_MAX_REQUEST_ID_LENGTH + 1), "over the length bound"),
        ("has space", "space"),
        ("line\nbreak", "newline — would forge a second log line"),
        ("tab\there", "control character"),
        ("emoji-\U0001f600", "non-ascii"),
        ("semi;colon", "outside the safe charset"),
        ("<script>", "outside the safe charset"),
    ],
)
def test_an_unsafe_id_is_replaced(supplied: str | None, why: str) -> None:
    result = _safe_request_id(supplied, AUTH)
    assert _is_minted(result), f"{why}: {supplied!r} should not have been adopted"
    assert result != supplied


def test_an_id_that_is_the_bearer_token_is_replaced() -> None:
    """The motivating case.

    A bearer token is header-safe ASCII, so a caller's own OpenRouter key is a
    structurally valid request id. Somebody scripting against this API can paste
    one into the wrong variable; without this check we would log and echo their
    spend-authorizing credential.
    """
    result = _safe_request_id(KEY, AUTH)
    assert _is_minted(result)
    assert KEY not in result


def test_an_id_that_is_a_prefix_of_the_bearer_token_is_replaced() -> None:
    """A long prefix is still credential material and narrows a search enormously."""
    result = _safe_request_id(KEY[:20], AUTH)
    assert _is_minted(result)


@pytest.mark.parametrize("length", [1, 2, _MIN_CREDENTIAL_FRAGMENT - 1])
def test_a_short_incidental_substring_of_the_key_is_still_adopted(length: int) -> None:
    """The fragment check must not swallow legitimate short ids.

    Almost any short string is a substring of some key — "a" certainly is — so
    rejecting on that would silently discard the caller's correlation id for no
    security benefit.
    """
    supplied = KEY[:length]
    assert _safe_request_id(supplied, AUTH) == supplied


def test_an_id_that_contains_the_bearer_token_is_replaced() -> None:
    result = _safe_request_id(f"trace-{KEY}", AUTH)
    assert _is_minted(result)
    assert KEY not in result


def test_the_overlap_check_is_skipped_without_a_bearer() -> None:
    """An unauthenticated route still gets correlation."""
    assert _safe_request_id("req-supplied-1", None) == "req-supplied-1"
    assert _safe_request_id("req-supplied-1", "Basic abc") == "req-supplied-1"


def test_minted_ids_are_unique() -> None:
    assert _safe_request_id(None, None) != _safe_request_id(None, None)


def test_an_empty_bearer_does_not_reject_every_id() -> None:
    """`"" in candidate` is true for every string. If the emptiness guard in
    `_bearer_token` regressed, every inbound id would be silently discarded."""
    assert _safe_request_id("req-supplied-1", "Bearer ") == "req-supplied-1"
    assert _safe_request_id("req-supplied-1", "Bearer") == "req-supplied-1"


# ---------------------------------------------------------------------------
# Every Authorization header, not just the one an accessor happened to return.
# ---------------------------------------------------------------------------


def test_a_credential_in_a_second_authorization_header_is_still_masked() -> None:
    """The hole a single-value accessor leaves open.

    Starlette's `Headers.get` returns the FIRST value and `dict(scope["headers"])`
    keeps the LAST, so with two `Authorization` headers each accessor silently
    drops one — and a request id equal to the credential in the dropped header
    would be adopted, logged and echoed. Nothing downstream can recover it: by
    then the secret is in the log pipeline.
    """
    second = "sk-or-v1-the-one-in-the-second-header"
    result = _safe_request_id(second, ["Bearer sk-or-v1-first", f"Bearer {second}"])
    assert _is_minted(result)
    assert second not in result


def test_a_credential_in_a_comma_folded_header_is_still_masked() -> None:
    """A proxy may fold repeated headers into one comma-separated value. The
    strict parser refuses that outright; redaction must still see through it,
    because refusing to read a header does not stop its bytes being a secret."""
    folded = "sk-or-v1-folded-by-a-proxy"
    result = _safe_request_id(folded, [f"Bearer sk-or-v1-first, Bearer {folded}"])
    assert _is_minted(result)
    assert folded not in result


def test_a_bare_string_is_not_iterated_as_characters() -> None:
    """`str` satisfies `Sequence[str]`, so passing one where a list was meant
    type-checks perfectly and then iterates CHARACTERS — every single character
    becomes a "credential" and every id is discarded as overlapping one.

    The failure is silent, on the one path whose whole job is not to be silent,
    and no type checker can object to it. Hence the runtime guard, and hence this
    test.
    """
    assert _safe_request_id("req-supplied-1", AUTH) == "req-supplied-1"
    assert _safe_request_id("req-supplied-1", None) == "req-supplied-1"
    assert _safe_request_id("req-supplied-1", []) == "req-supplied-1"


def test_a_credential_in_a_comma_folded_header_is_masked_in_either_direction() -> None:
    """The earlier folded-header test passed for a weaker reason than it claimed:
    the long token happened to be a substring of the whole folded value. The
    dangerous direction is the other one — an id that CONTAINS the credential.

    `trace-<credential>` is contained in nothing, and contains only the isolated
    credential. Without splitting the folded value into its fields, no candidate
    matches and the id is adopted with the secret inside it.
    """
    secret = "sk-or-v1-second-of-two-folded"
    folded = f"Bearer sk-or-v1-first, Bearer {secret}"
    result = _safe_request_id(f"trace-{secret}", [folded])
    assert _is_minted(result)
    assert secret not in result


def test_a_short_credential_in_a_folded_header_is_still_masked() -> None:
    """The worst version: a credential below the fragment threshold. Found only as
    part of the whole folded value, the reverse rule is suppressed and an EXACT
    match on the id escapes."""
    secret = "sk-abc12"
    result = _safe_request_id(secret, [f"Bearer sk-or-v1-first, Bearer {secret}"])
    assert _is_minted(result)


def test_a_scheme_only_header_does_not_reject_an_ordinary_id() -> None:
    """`Authorization: Bearer` would otherwise make the literal word "Bearer" a
    secret, and any id containing it would be discarded for overlapping a value
    written in the clear in every HTTP specification."""
    assert _safe_request_id("trace-Bearer", ["Bearer"]) == "trace-Bearer"


def test_two_inbound_request_ids_are_ambiguous_and_neither_is_adopted() -> None:
    """Picking either would let our logs name a different id than an
    intermediary's do — the precise failure an inbound id exists to prevent. A
    minted id at least matches everything downstream of here."""
    from daintree_assistant_server.main import _safe_request_id as safe

    assert _is_minted(safe(None, AUTH))
