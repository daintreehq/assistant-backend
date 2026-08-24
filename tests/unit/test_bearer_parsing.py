"""The strict bearer parser, and the lenient extractor beside it.

Two functions with opposite postures over the same header. The parser decides
whether a credential was presented and refuses anything it cannot read exactly;
the extractor decides what might be a secret and refuses nothing. Testing them
together is deliberate — the interesting property is that they DISAGREE about the
same input, and disagree in the safe direction each time.
"""

from __future__ import annotations

import pytest

from daintree_assistant_server.services.bearer import (
    MAX_AUTHORIZATION_VALUE_LENGTH,
    BearerOutcome,
    authorization_values,
    credential_candidates,
    parse_bearer,
)

_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"


# ---- Reading the raw header list -------------------------------------------


def test_every_authorization_header_is_returned_in_order() -> None:
    """Order is preserved because "more than one" is itself the finding: which of
    two headers a proxy forwards is not knowable here."""
    raw = [
        (b"content-type", b"application/json"),
        (b"Authorization", b"Bearer one"),
        (b"authorization", b"Bearer two"),
    ]
    assert authorization_values(raw) == ["Bearer one", "Bearer two"]


def test_the_header_name_is_matched_case_insensitively() -> None:
    assert authorization_values([(b"AUTHORIZATION", b"Bearer x")]) == ["Bearer x"]


def test_every_byte_decodes_so_no_header_is_ever_lost() -> None:
    """ISO-8859-1 is a total mapping — every byte decodes, so `errors="replace"` is
    belt and braces rather than the mechanism. That totality is the point: a token
    with a strange byte must reach the parser (which refuses it on the character
    rule) and reach REDACTION (which masks it anyway). A decode that dropped the
    header would lose it to both, and the credential would then be free to appear
    in a request id.
    """
    values = authorization_values([(b"authorization", b"Bearer \xff\xfe")])
    # Round-trips exactly: nothing was replaced, so nothing was lost.
    assert values == ["Bearer \xff\xfe"]
    assert values[0].encode("iso-8859-1") == b"Bearer \xff\xfe"
    assert parse_bearer(values).outcome is BearerOutcome.MALFORMED
    assert any("\xff\xfe" in c for c in credential_candidates(values))


# ---- The strict parser ------------------------------------------------------


def test_an_ordinary_bearer_is_accepted() -> None:
    result = parse_bearer([f"Bearer {_JWT}"])
    assert result.well_formed
    assert result.token == _JWT


def test_the_scheme_is_case_insensitive_and_the_credential_is_not() -> None:
    """RFC 7235 says auth schemes are case-insensitive. The credential is opaque
    and a case change would be a different token."""
    for scheme in ("Bearer", "bearer", "BEARER", "BeArEr"):
        assert parse_bearer([f"{scheme} {_JWT}"]).token == _JWT


def test_no_header_is_absent_rather_than_malformed() -> None:
    """Different outcomes because they have different remedies: there is nothing
    to refresh when nothing was sent, so the only move is a sign-in."""
    result = parse_bearer([])
    assert result.outcome is BearerOutcome.ABSENT
    assert result.token is None


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        ([f"Bearer {_JWT}", f"Bearer {_JWT}"], "multiple_authorization_headers"),
        ([""], "empty_authorization_header"),
        (["   "], "empty_authorization_header"),
        ([f"Bearer {_JWT}, Bearer other"], "comma_in_authorization_header"),
        (["Bearer"], "malformed_authorization_header"),
        ([f"Bearer {_JWT} extra"], "malformed_authorization_header"),
        ([f" Bearer {_JWT}"], "padded_authorization_header"),
        ([f"Bearer {_JWT} "], "padded_authorization_header"),
        (["Bearer "], "padded_authorization_header"),
        (["Bearer ="], "empty_bearer_credential"),
        (["Bearer =="], "empty_bearer_credential"),
        (["Bearer a=b"], "invalid_bearer_character"),
        (["Bearer ==a"], "invalid_bearer_character"),
        ([f"Basic {_JWT}"], "unsupported_authorization_scheme"),
        ([f"Token {_JWT}"], "unsupported_authorization_scheme"),
        (["Bearer tok en"], "malformed_authorization_header"),
        (["Bearer tok\ten"], "invalid_bearer_character"),
        (["Bearer tok\nen"], "invalid_bearer_character"),
        (["Bearer tok\x00en"], "invalid_bearer_character"),
        (["Bearer tokén"], "invalid_bearer_character"),
        (["Bearer " + "a" * (MAX_AUTHORIZATION_VALUE_LENGTH + 1)], "authorization_header_too_long"),
    ],
    ids=[
        "duplicate", "empty", "whitespace-only", "comma-folded", "scheme-only",
        "three-parts", "leading-space", "trailing-space",
        "scheme-and-space", "only-padding", "only-padding-2",
        "padding-in-middle", "leading-padding", "basic", "token-scheme",
        "internal-space", "tab", "newline", "nul", "non-ascii", "oversized",
    ],
)
def test_a_credential_that_cannot_be_read_exactly_is_refused(
    values: list[str], reason: str
) -> None:
    result = parse_bearer(values)
    assert result.outcome is BearerOutcome.MALFORMED
    assert result.token is None
    assert result.reason == reason


def test_two_headers_are_refused_rather_than_resolved() -> None:
    """Even when both carry the SAME credential. Which one an upstream proxy
    forwards is not something this server can know, so validating either would be
    validating a credential the upstream might not have seen."""
    assert parse_bearer([f"Bearer {_JWT}", f"Bearer {_JWT}"]).outcome is BearerOutcome.MALFORMED


def test_comma_folding_is_refused_for_the_same_reason() -> None:
    """RFC 7235 lets a proxy fold repeated headers into one comma-separated value,
    so a comma IS the duplicate case wearing a different shape. Reading the first
    credential would make the two cases behave differently for no reason a client
    could predict."""
    assert parse_bearer(["Bearer a, Bearer b"]).reason == "comma_in_authorization_header"


def test_a_maximum_length_credential_is_still_accepted() -> None:
    token = "a" * (MAX_AUTHORIZATION_VALUE_LENGTH - len("Bearer "))
    assert parse_bearer([f"Bearer {token}"]).well_formed


def test_no_refusal_reason_contains_the_credential() -> None:
    """`reason` goes into a log line next to the request id. It names the rule
    that failed, never the value that failed it — an oversized header is still
    credential material, and so is one with a stray byte in it."""
    secret = "s3cr3t-pasted-by-mistake"
    for values in (
        [f"Bearer {secret}", f"Bearer {secret}"],
        [f"Bearer {secret} extra"],
        [f"Basic {secret}"],
        ["Bearer " + secret + "\x00"],
        ["Bearer " + secret * 500],
    ):
        result = parse_bearer(values)
        assert secret not in result.reason, result.reason


# ---- The lenient extractor --------------------------------------------------


def test_the_extractor_returns_both_the_whole_value_and_the_credential() -> None:
    """Over-collects on purpose. The output is only ever compared against, so a
    candidate that turns out not to be a secret costs nothing while a missed one
    costs a credential in the log pipeline."""
    candidates = credential_candidates([f"Bearer {_JWT}"])
    assert f"Bearer {_JWT}" in candidates
    assert _JWT in candidates


@pytest.mark.parametrize(
    "value",
    [
        f"Bearer {_JWT}, Bearer other",
        f"Bearer {_JWT} extra",
        f"Basic {_JWT}",
        f"{_JWT}",
        f"  Bearer {_JWT}  ",
    ],
    ids=["comma-folded", "three-parts", "wrong-scheme", "bare-token", "padded"],
)
def test_the_extractor_still_finds_a_credential_the_parser_refuses(value: str) -> None:
    """The whole reason these are two functions. A header the parser rejects is
    MORE likely to hold a mis-pasted credential, not less — somebody put their
    token in the wrong place — so refusing to look at it would be exactly
    backwards.
    """
    assert parse_bearer([value]).outcome is BearerOutcome.MALFORMED
    assert any(_JWT in candidate for candidate in credential_candidates([value]))


def test_the_extractor_sees_every_header() -> None:
    """A token in the second `Authorization` header must still be masked. This is
    the case a `Headers.get` or a `dict(raw_headers)` silently drops."""
    candidates = credential_candidates(["Bearer first", "Bearer second"])
    assert any("first" in c for c in candidates)
    assert any("second" in c for c in candidates)


@pytest.mark.parametrize("values", [[], [""], ["   "], ["Bearer"], ["\x00"]])
def test_the_extractor_never_raises_on_anything(values: list[str]) -> None:
    """It runs on the request-id path, which must not be able to fail: a malformed
    header turning correlation-id generation into a 500 would take out every
    request that carried one."""
    assert isinstance(credential_candidates(values), list)


def test_one_or_more_spaces_separate_the_scheme_from_the_credential() -> None:
    """RFC 7235 writes the separator as `1*SP`, so a run of spaces is legal syntax
    and refusing it would be stricter than the spec for no security gain. Only
    SPACE counts: a tab is not `SP` and survives to be refused as an invalid
    character."""
    assert parse_bearer([f"Bearer  {_JWT}"]).token == _JWT
    assert parse_bearer([f"Bearer    {_JWT}"]).token == _JWT
    assert parse_bearer([f"Bearer\t{_JWT}"]).outcome is BearerOutcome.MALFORMED


def test_the_result_does_not_print_the_credential() -> None:
    """Every other line of this module is careful not to echo the token. A default
    dataclass repr would undo all of it the first time somebody logged a result or
    let one reach a traceback."""
    result = parse_bearer([f"Bearer {_JWT}"])
    assert result.token == _JWT
    assert _JWT not in repr(result)


def test_a_single_string_raises_rather_than_being_normalised() -> None:
    """The opposite choice from `main._safe_request_id`, deliberately.

    There, a throw would take correlation down for every request carrying a
    header, so a bad call has to degrade. Here a bad call means somebody reached
    for a single-value accessor — the exact bug this module exists to prevent —
    and it must be impossible to ship rather than quietly survivable. `str`
    satisfies `Sequence[str]`, so the type checker will not object.
    """
    with pytest.raises(TypeError, match="every Authorization value"):
        parse_bearer(f"Bearer {_JWT}")  # type: ignore[arg-type]


def test_the_outcome_is_named_for_syntax_rather_than_for_trust() -> None:
    """`WELL_FORMED`, not `OK`. Nothing here has been verified — no signature, no
    issuer, no audience, no expiry — and a value invented by anyone at all reaches
    this outcome. At an authentication seam a field called `ok` gets read as "this
    caller is fine", which is the one thing it does not say."""
    assert parse_bearer(["Bearer anyone-can-type-this"]).well_formed
    assert not hasattr(parse_bearer(["Bearer x"]), "ok")


# ---- The escape: a credential the whole-value candidate cannot isolate ------


@pytest.mark.parametrize(
    "value",
    [
        "Bearer first-credential, Bearer {secret}",
        "Bearer\tfirst-credential\t{secret}",
        "Bearer first-credential  {secret}",
        "Basic first, Bearer {secret}",
    ],
    ids=["comma-folded", "tab-separated", "double-spaced", "mixed-schemes"],
)
def test_a_credential_in_a_folded_or_malformed_field_is_isolated(value: str) -> None:
    """The escape the whole-value candidate leaves open.

    A folded header yields one long string that neither contains nor is contained
    in an id like `trace-<secret>` — so substring matching finds nothing and the
    credential is adopted. Substring matching only works once the credential has
    been ISOLATED, which is why the extractor splits on commas and whitespace
    before comparing.
    """
    secret = "s3cr3t-second-credential"
    candidates = credential_candidates([value.format(secret=secret)])
    assert secret in candidates, candidates


def test_a_scheme_word_alone_is_not_treated_as_a_secret() -> None:
    """`Authorization: Bearer` would otherwise make the literal word "Bearer"
    credential material, and every request id containing it would be thrown away
    for overlapping a secret that is written in the clear in every HTTP spec."""
    assert credential_candidates(["Bearer"]) == []
    assert credential_candidates(["Basic"]) == []
    assert "Bearer" not in credential_candidates([f"Bearer {_JWT}"])


# ---- Nobody reads this header with a single-value accessor -----------------


def test_no_module_reads_the_authorization_header_with_a_single_value_accessor() -> None:
    """Textual, because that is the shape this bug arrives in.

    `Headers.get("authorization")` returns the FIRST of two headers and
    `dict(scope["headers"])` keeps the LAST. Both look completely correct at the
    call site, both compile, both pass every test written with one header — and
    each silently ignores a different one of two. That is how it got into two
    separate middlewares here.

    `services/bearer.py` is the only module allowed to read it, and it reads the
    raw list. Everything else goes through `authorization_values`.

    `auth.py` is the one carve-out and it shrinks to nothing in the phase that
    wires this in: it currently passes the header to a function that ignores it
    entirely, so there is no value being read wrongly — but it IS the exact place
    the trap springs the moment that function starts reading, which is why it is
    named here rather than left to be noticed.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "daintree_assistant_server"
    allowed = {src / "services" / "bearer.py", src / "auth.py"}
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        if path in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            lowered = line.lower()
            if "authorization" not in lowered or lowered.lstrip().startswith("#"):
                continue
            if ".get(" in lowered or "dict(" in lowered:
                offenders.append(f"{path.relative_to(src)}:{number}: {line.strip()}")
    assert not offenders, (
        "these read the Authorization header with an accessor that silently drops "
        f"one of two headers: {offenders}. Use services.bearer.authorization_values."
    )
