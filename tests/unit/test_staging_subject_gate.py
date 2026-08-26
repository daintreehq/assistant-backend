"""The private-staging subject gate: its parser, its digest, and its readiness.

The wire behaviour lives in `tests/integration/test_account_auth.py`. What is
here is the half that decides membership, and almost every test below is about a
value that is NEARLY right — because a list that silently drops the line it could
not read is a list that locks out the person it was just edited to admit, and
nothing about the deployment looks wrong when that happens.

**No real digest appears in this file or may ever be added to it.** Every value is
either an obviously synthetic literal or is computed from a synthetic UUID through
the shipped helper, which is the better of the two: it proves the algorithm rather
than restating a constant.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from daintree_assistant_server.auth import subject_digest, subject_hash
from daintree_assistant_server.config import (
    EXECUTOR_KEY_ENV,
    FAST_RESPONSE_KEY_ENV,
    Settings,
    parse_subject_digests,
)

#: A subject nobody has. Deliberately full of hex LETTERS: the canonicalization
#: hazard below is the sharpest edge in the whole feature, and an all-digit UUID
#: is unchanged by uppercasing, so it would let the bug through silently.
SYNTHETIC_SUBJECT = "deadbeef-0b0a-4a9b-8c7d-feedfacecafe"
SYNTHETIC_SUBJECT_UPPER = SYNTHETIC_SUBJECT.upper()

#: A digest that is obviously not one. Used wherever the test is about the SHAPE
#: of the list rather than about membership.
FAKE = "a" * 64
OTHER_FAKE = "b" * 64


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "STATE_SIGNING_KEY_CURRENT": "sk",
        EXECUTOR_KEY_ENV: "sk-test",
        FAST_RESPONSE_KEY_ENV: "csk-test",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _staging(**overrides: object) -> Settings:
    """A staging deployment with enforcement on — the one posture the gate runs in."""
    base: dict[str, object] = {
        "ENVIRONMENT": "staging",
        "AUTH_MODE": "enforce",
        "SUPABASE_AUTH_ISSUER": "https://abcdefghij.supabase.co/auth/v1",
        "SUPABASE_JWKS_URL": "https://abcdefghij.supabase.co/auth/v1/.well-known/jwks.json",
        "SUPABASE_OAUTH_AUTHORIZE_URL": "https://abcdefghij.supabase.co/auth/v1/oauth/authorize",
        "SUPABASE_OAUTH_TOKEN_URL": "https://abcdefghij.supabase.co/auth/v1/oauth/token",
        "SUPABASE_OAUTH_CLIENT_ID": "daintree-assistant",
        "STAGING_ALLOWED_SUBJECT_DIGESTS": FAKE,
        "STAGING_SUBJECT_GATE_REVISION": "beta-01",
    }
    base.update(overrides)
    return _settings(**base)


# ---- the digest ------------------------------------------------------------


def test_the_digest_is_the_full_hash_under_the_shared_domain_separator() -> None:
    """One algorithm, and it is the short hash's own — untruncated.

    Recomputed here from the imported constant rather than from a literal string,
    because the domain separator has exactly one home in this repo (`auth.py`) and
    a second copy of it here would be a second place for it to drift.
    """
    import hashlib

    from daintree_assistant_server.auth import _SUBJECT_HASH_PREFIX

    expected = hashlib.sha256(
        (_SUBJECT_HASH_PREFIX + SYNTHETIC_SUBJECT).encode("utf-8")
    ).hexdigest()
    digest = subject_digest(SYNTHETIC_SUBJECT)
    assert digest == expected
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_the_full_digest_is_not_the_public_support_hash() -> None:
    """They share a prefix, and that is exactly why they must not be interchanged.

    The 16-character one is printed by the CLI and by every log line here, so
    gating on it would decide membership with a value already published
    everywhere. The prefix relationship makes that mistake invisible to a
    startswith-style check, so it is asserted head-on.
    """
    short = subject_hash(SYNTHETIC_SUBJECT)
    full = subject_digest(SYNTHETIC_SUBJECT)
    assert len(short) == 16
    assert full != short
    assert full.startswith(short)


def test_the_canonical_spelling_and_the_uppercase_one_are_different_digests() -> None:
    """The hazard, stated as an assertion.

    `_require_uuid` compares the `sub` claim against `value.lower()`, so an
    uppercase-hex UUID is ACCEPTED as canonical while spelling itself differently
    from `str(UUID(...))`. Hashing the raw claim would therefore produce two
    digests for one account, and the gate would admit or refuse the same person
    depending on how their token happened to be minted. Anything that computes a
    gate key must go through the `UUID` object.
    """
    assert subject_digest(SYNTHETIC_SUBJECT) != subject_digest(SYNTHETIC_SUBJECT_UPPER)
    assert subject_digest(str(UUID(SYNTHETIC_SUBJECT_UPPER))) == subject_digest(
        SYNTHETIC_SUBJECT
    )


# ---- the parser: what it accepts -------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (FAKE, {FAKE}),
        (FAKE + "\n", {FAKE}),
        ("\n".join([FAKE, OTHER_FAKE]), {FAKE, OTHER_FAKE}),
        ("\n".join([FAKE, OTHER_FAKE]) + "\n", {FAKE, OTHER_FAKE}),
    ],
    ids=["one", "one-with-lf", "two", "two-with-lf"],
)
def test_a_well_formed_list_parses(value: str, expected: set[str]) -> None:
    entries, problem = parse_subject_digests(value)
    assert problem is None
    assert entries == frozenset(expected)


@pytest.mark.parametrize("terminal", ["", "\n"], ids=["no-lf", "trailing-lf"])
def test_the_largest_legal_list_parses(terminal: str) -> None:
    """64 entries, at and just under the byte ceiling.

    The two bounds meet here on purpose: 64 entries and their separators is
    exactly 4,160 bytes, so this is the value that proves the ceiling was set to
    admit the largest legal list rather than to exclude it by one.
    """
    values = [f"{index:064x}" for index in range(1, 65)]
    raw = "\n".join(values) + terminal
    assert len(raw.encode("utf-8")) <= 4160
    entries, problem = parse_subject_digests(raw)
    assert problem is None
    assert len(entries) == 64


# ---- the parser: what it refuses -------------------------------------------


@pytest.mark.parametrize(
    ("value", "why"),
    [
        ("", "empty"),
        ("   ", "whitespace-only"),
        ("\n", "lf-only"),
        ("a" * 63, "short"),
        ("a" * 65, "long"),
        ("A" * 64, "uppercase"),
        ("a" * 63 + "A", "one-uppercase-character"),
        ("g" * 64, "non-hex"),
        ("a" * 63 + "z", "one-non-hex-character"),
        (f"{FAKE}\n\n{OTHER_FAKE}", "blank-line"),
        (f"{FAKE}\n\n", "two-terminal-lfs"),
        (f"\n{FAKE}", "leading-lf"),
        (f"{FAKE}\n{FAKE}", "duplicate"),
        (f"{FAKE}\r\n{OTHER_FAKE}", "crlf"),
        (f"{FAKE}\r{OTHER_FAKE}", "cr"),
        (f"{FAKE}\r\n", "trailing-crlf"),
        (f"﻿{FAKE}", "bom"),
        (f" {FAKE}", "leading-space"),
        (f"{FAKE} ", "trailing-space"),
        ("a" * 32 + " " + "a" * 31, "embedded-space"),
        ("a" * 32 + "\t" + "a" * 31, "embedded-tab"),
        ("\n".join(f"{index:064x}" for index in range(1, 66)), "sixty-five-entries"),
        ("\n".join(["a"] * 65), "sixty-five-short-entries"),
        ("a" * 5000, "over-the-byte-ceiling"),
        ("\udcff" + FAKE, "invalid-utf-8"),
    ],
)
def test_a_malformed_list_fails_closed(value: str, why: str) -> None:
    """Every near-miss yields an EMPTY set as well as a problem.

    The pairing is what makes the door safe independently of readiness: a caller
    that reads only the entries still admits nobody, so a parse failure can never
    be the thing that opens a private deployment.
    """
    entries, problem = parse_subject_digests(value)
    assert problem is not None, why
    assert entries == frozenset(), why


def test_a_refusal_never_quotes_the_value_it_refused() -> None:
    """Problem strings reach the startup log and a non-hardened `/readyz` body.

    A digest is the gate's key, so echoing the entry that failed would publish
    membership material through the diagnostic for a typo.
    """
    for value in (FAKE, f"{FAKE}\n{FAKE}", f"{FAKE} ", "A" * 64):
        _, problem = parse_subject_digests(value + "!")
        assert problem is not None
        assert FAKE not in problem
        assert "A" * 64 not in problem


def test_the_ceiling_rejects_at_exactly_one_byte_over() -> None:
    """The boundary itself, from both sides.

    "4,160 is accepted" alone would pass against any ceiling that is too LARGE,
    which is the direction that matters: a bound nobody can reach is a bound that
    is not there. 4,161 bytes must be refused, and refused for its size rather
    than for the shape of the entry that pushed it over.
    """
    entries = [f"{index:064x}" for index in range(1, 65)]
    at_ceiling = "\n".join(entries) + "\n"
    assert len(at_ceiling.encode("utf-8")) == 4160
    assert parse_subject_digests(at_ceiling)[1] is None

    _, problem = parse_subject_digests(at_ceiling + "a")
    assert problem is not None
    assert "4161 bytes" in problem


def test_an_oversized_value_is_refused_by_size_before_it_is_decoded() -> None:
    """Asserted through a value that is both oversized AND not valid UTF-8.

    Only an implementation that measures the raw bytes FIRST can report the size,
    because a decode-first one dies on the encoding and reports that instead. The
    lone surrogate is what `os.environ` produces for a byte sequence that was
    never valid UTF-8, so this is the real shape of the input, not a contrivance.
    """
    _, problem = parse_subject_digests("\udcff" + "a" * 5000)
    assert problem is not None
    assert "bytes" in problem
    assert "UTF-8" not in problem


def test_a_multibyte_character_counts_as_its_bytes() -> None:
    """The ceiling is a byte count and has to behave like one.

    2,000 three-byte characters is 6,000 bytes and 2,000 characters — under the
    ceiling by one measure and well over it by the one that matters.
    """
    _, problem = parse_subject_digests("あ" * 2000)
    assert problem is not None
    assert "6000 bytes" in problem


# ---- the revision label ----------------------------------------------------


@pytest.mark.parametrize(
    "revision",
    ["a", "0", "a" * 32, "beta-01", "cohort.2", "x_y-z.0"],
    ids=["one-char", "one-digit", "thirty-two", "hyphen", "dot", "mixed"],
)
def test_a_valid_revision_label_passes(revision: str) -> None:
    assert _staging(STAGING_SUBJECT_GATE_REVISION=revision).subject_gate_problems() == []


@pytest.mark.parametrize(
    "revision",
    ["", "-lead", ".lead", "_lead", "A", "beta 01", " beta", "beta ", "a" * 33, "beta/01", "béta"],
    ids=[
        "empty",
        "leading-hyphen",
        "leading-dot",
        "leading-underscore",
        "uppercase",
        "space",
        "leading-space",
        "trailing-space",
        "thirty-three",
        "slash",
        "non-ascii",
    ],
)
def test_a_malformed_revision_fails_closed_in_staging(revision: str) -> None:
    problems = _staging(STAGING_SUBJECT_GATE_REVISION=revision).subject_gate_problems()
    assert any("STAGING_SUBJECT_GATE_REVISION" in problem for problem in problems)


# ---- readiness -------------------------------------------------------------


def test_a_complete_staging_gate_is_ready() -> None:
    assert _staging().subject_gate_problems() == []
    assert _staging().staging_subject_gate_enabled is True


@pytest.mark.parametrize(
    "value",
    ["", "   ", "a" * 63, f"{FAKE}\n{FAKE}", f"{FAKE}\r\n{OTHER_FAKE}"],
    ids=["missing", "whitespace", "short", "duplicate", "crlf"],
)
def test_staging_enforcement_without_a_usable_list_fails_readiness(value: str) -> None:
    """It does NOT degrade to "no gate".

    An operator who asked for a private deployment and silently got a public one
    has no way to discover that, and the revision would take traffic looking
    exactly like a working one.
    """
    settings = _staging(STAGING_ALLOWED_SUBJECT_DIGESTS=value)
    assert settings.subject_gate_problems() != []
    assert settings.local_only_problems() != []


def test_the_gate_is_inert_below_enforcement() -> None:
    """`observe` and `open` never produce a subject to test.

    Every request under them is the anonymous principal, so a gate there could
    only refuse everyone or nobody. Requiring a list would fail readiness on the
    posture a staging rollout starts in.
    """
    for mode in ("open", "observe"):
        settings = _staging(AUTH_MODE=mode, STAGING_ALLOWED_SUBJECT_DIGESTS="")
        assert settings.staging_subject_gate_enabled is False
        assert settings.subject_gate_problems() == []


def test_a_development_deployment_does_not_suddenly_need_a_list() -> None:
    assert _settings().staging_subject_gate_enabled is False
    assert _settings().subject_gate_problems() == []


@pytest.mark.parametrize("environment", ["development", "test"])
def test_a_malformed_list_is_refused_even_where_the_gate_is_inert(environment: str) -> None:
    """Whatever is set is validated everywhere.

    A malformed list in development is the value that will be promoted, and
    finding out at promotion is finding out during a rollout.
    """
    settings = _settings(
        ENVIRONMENT=environment, STAGING_ALLOWED_SUBJECT_DIGESTS=f"{FAKE}\n{FAKE}"
    )
    assert settings.subject_gate_problems() != []


def test_production_may_not_hold_a_staging_list() -> None:
    """The decision this brief had to make, pinned so it cannot be softened by
    accident.

    Production neither enforces a list it was handed — that would be a private
    beta nobody decided to ship — nor ignores one, which would leave an operator
    believing production was gated when it was not. Both are decisions made by
    accident; refusing readiness is the only reading with no silent half.
    """
    settings = _settings(ENVIRONMENT="production", STAGING_ALLOWED_SUBJECT_DIGESTS=FAKE)
    assert settings.staging_subject_gate_enabled is False
    problems = settings.subject_gate_problems()
    assert any("must be unset in production" in problem for problem in problems)


def test_production_without_a_list_is_unaffected() -> None:
    assert _settings(ENVIRONMENT="production").subject_gate_problems() == []


def test_readiness_reaches_the_gate_through_local_only_problems() -> None:
    """The aggregator is the wiring. A method nothing calls gates nothing."""
    settings = _staging(STAGING_SUBJECT_GATE_REVISION="")
    assert set(settings.subject_gate_problems()) <= set(settings.local_only_problems())


# ---- membership ------------------------------------------------------------


def test_the_configured_digest_of_a_synthetic_subject_is_a_member() -> None:
    digest = subject_digest(SYNTHETIC_SUBJECT)
    settings = _staging(STAGING_ALLOWED_SUBJECT_DIGESTS=digest)
    assert settings.subject_gate_problems() == []
    assert digest in settings.allowed_subject_digests()


def test_an_uppercase_spelling_of_a_listed_subject_is_not_a_member() -> None:
    """The canonicalization hazard, one layer out from the digest itself.

    A list holding the canonical subject's digest must not be satisfied by hashing
    the raw uppercase `sub` claim. If it were, membership would depend on the
    spelling a token happened to carry.
    """
    settings = _staging(
        STAGING_ALLOWED_SUBJECT_DIGESTS=subject_digest(SYNTHETIC_SUBJECT)
    )
    assert subject_digest(SYNTHETIC_SUBJECT_UPPER) not in settings.allowed_subject_digests()
    # And the canonical form recovered from the uppercase spelling IS a member,
    # which is what the door is required to compute.
    assert subject_digest(str(UUID(SYNTHETIC_SUBJECT_UPPER))) in (
        settings.allowed_subject_digests()
    )


def test_an_unparseable_list_admits_nobody() -> None:
    settings = _staging(STAGING_ALLOWED_SUBJECT_DIGESTS=f"{FAKE}\n{FAKE}")
    assert settings.allowed_subject_digests() == frozenset()


# ---- the secret stays secret -----------------------------------------------


def test_the_list_never_appears_in_a_repr_or_a_string() -> None:
    """`SecretStr` is the type, and the reason is that the LIST is sensitive even
    though each entry is one-way: it enumerates a private cohort."""
    digest = subject_digest(SYNTHETIC_SUBJECT)
    settings = _staging(STAGING_ALLOWED_SUBJECT_DIGESTS=digest)
    for rendered in (repr(settings), str(settings), repr(settings.staging_allowed_subject_digests)):
        assert digest not in rendered
        assert FAKE not in rendered


def test_a_settings_validation_error_never_carries_the_list() -> None:
    """Pydantic echoes offending input in its error text. The secret must not be
    reachable through a failure somewhere else in the same model."""
    from pydantic import ValidationError

    digest = subject_digest(SYNTHETIC_SUBJECT)
    with pytest.raises(ValidationError) as caught:
        _settings(
            STAGING_ALLOWED_SUBJECT_DIGESTS=digest,
            ENVIRONMENT="not-an-environment",
        )
    assert digest not in str(caught.value)


def test_no_readiness_problem_ever_carries_an_entry() -> None:
    digest = subject_digest(SYNTHETIC_SUBJECT)
    settings = _staging(
        STAGING_ALLOWED_SUBJECT_DIGESTS=f"{digest}\n{digest}",
        STAGING_SUBJECT_GATE_REVISION="",
    )
    for problem in settings.local_only_problems():
        assert digest not in problem


def test_the_gate_is_not_a_required_secret() -> None:
    """It gates readiness through its own conditional check, not through the
    unconditional secret lists.

    `missing_secrets()` means "nothing can work without this", which is false: the
    gate is required only under one posture, and a deployment in any other one is
    complete without it. The distinction is the same one `auth_problems()` and
    `entitlement_problems()` exist to draw.
    """
    settings = _settings(STAGING_ALLOWED_SUBJECT_DIGESTS="", STAGING_SUBJECT_GATE_REVISION="")
    assert "STAGING_ALLOWED_SUBJECT_DIGESTS" not in settings.missing_secrets()
    assert not any(
        "STAGING_" in problem
        for problem in (*settings.missing_secrets(), *settings.weak_secrets())
    )



# ---- the published label ---------------------------------------------------


@pytest.mark.parametrize("revision", ["a", "beta-01", "a" * 32])
def test_a_valid_label_is_published_as_written(revision: str) -> None:
    assert _staging(STAGING_SUBJECT_GATE_REVISION=revision).published_gate_revision == revision


@pytest.mark.parametrize(
    "revision",
    ["", "A" * 64, "a" * 64, "sub=deadbeef-0b0a-4a9b", "a subject id", "a" * 33],
    ids=["unset", "uppercase-digest", "a-full-digest", "an-assignment", "a-sentence", "too-long"],
)
def test_an_unvalidated_label_is_never_published(revision: str) -> None:
    """`/version` answers while the service is UNREADY, on purpose.

    So "readiness refuses a malformed label" does not on its own make the label
    safe to echo: the deployment whose label is wrong is exactly the one whose
    `/version` an operator is reading. A digest or a subject id typed into the
    wrong variable must not be published by the very mistake that made it unsafe.
    """
    settings = _settings(STAGING_SUBJECT_GATE_REVISION=revision)
    assert settings.published_gate_revision == ""


def test_the_published_label_is_what_a_refusal_logs() -> None:
    """One projection, used by both readers. A second spelling is how the safe one
    stays safe while the other quietly is not."""
    settings = _staging(STAGING_SUBJECT_GATE_REVISION="cohort.2")
    assert settings.published_gate_revision == settings.staging_subject_gate_revision


def test_the_label_check_is_syntactic_and_says_so() -> None:
    """The honest bound on what the charset buys, pinned so nobody widens the
    claim by reading the regex optimistically.

    32 lowercase-hex characters are a legal label AND the first half of a digest;
    a de-hyphenated UUID fits too. Nothing here can tell a name from a secret, so
    "the label carries no subject material" is an OPERATOR contract — stated in
    `.env.example`, where an operator reads it — and what the validator actually
    guarantees is only that the value cannot reshape a log line or a JSON body.
    """
    flattened_uuid = str(UUID(SYNTHETIC_SUBJECT)).replace("-", "")
    assert len(flattened_uuid) == 32
    assert _settings(
        STAGING_SUBJECT_GATE_REVISION=flattened_uuid
    ).published_gate_revision == flattened_uuid
