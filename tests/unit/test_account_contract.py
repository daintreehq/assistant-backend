"""``AccountResponse`` against the CANONICAL fixtures, not against hand-built dicts.

The defect this file is shaped by: the backend and the CLI each implemented this
contract correctly, from the same prose, and disagreed — the backend omitted every
entitlement field from an identity-only answer, the CLI required ``checked_at`` on
every success. Both suites were green, because each had written its own fixture.

So the fixtures move out of the test and into ``tests/fixtures/contract/``, where
the CLI copies them byte-for-byte. A test that builds its own input can only ever
check that this repository agrees with itself.

The dict-built tests below the fixture sweep are still here on purpose: they pin
the individual RULES with the specific error each one raises, which a fixture
proves only in aggregate.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from daintree_assistant_server.contracts.account import (
    ACCOUNT_CONTRACT_VERSION,
    AccountResponse,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "fixtures" / "contract"
_MANIFEST: dict[str, Any] = json.loads((_CONTRACT / "manifest.json").read_text("utf-8"))
_HASH = _MANIFEST["subject_hash"]


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Refuse a fixture that names the same field twice.

    `json.loads` keeps the LAST occurrence silently, so a fixture carrying both
    `"access": "granted"` and `"access": "unverified"` would be a green test of
    whichever happened to come second — and the file a human reads would say
    something different from the contract the test proved.
    """
    seen: dict[str, Any] = {}
    for name, value in pairs:
        if name in seen:
            raise ValueError(f"duplicate key in fixture: {name}")
        seen[name] = value
    return seen


def _fixture(name: str) -> dict[str, Any]:
    return json.loads(  # type: ignore[no-any-return]
        (_CONTRACT / name).read_text("utf-8"), object_pairs_hook=_no_duplicate_keys
    )


def _rows(valid: bool) -> list[dict[str, Any]]:
    return [row for row in _MANIFEST["account"] if row["valid"] is valid]


def _cases(valid: bool) -> list[Any]:
    return [
        pytest.param(row["file"], id=Path(row["file"]).stem) for row in _rows(valid)
    ]


# --- the canonical fixtures ------------------------------------------------


@pytest.mark.parametrize("name", _cases(valid=True))
def test_every_valid_fixture_parses_and_re_emits_the_same_fields(name: str) -> None:
    """Parsed AND re-emitted, because the CLI decodes what this model DUMPS.

    A fixture that parses but serializes to something else would still leave the
    two repositories reading different bytes, so validating alone is half the
    contract. What this catches: a normalised timestamp, an added or defaulted
    field, an explicit null dropped by `exclude_none`, an unknown field.

    What it does NOT catch, so that the name does not promise more than it
    delivers: byte-level differences that survive a parse — key order, whitespace,
    escape spelling. Those do not matter to a decoder. Duplicate keys DO, and are
    caught by `_fixture`'s hook rather than by this comparison.
    """
    raw = _fixture(name)
    dumped = AccountResponse.model_validate(raw).model_dump(mode="json", exclude_none=True)
    assert dumped == raw
    # Dict equality treats `0 == False` and `1 == True`, so a fixture spelling a
    # boolean as a number would compare equal after Pydantic coerced it. The
    # fixture is what another language parses, so the JSON TYPE has to be right.
    for field in ("entitlement_stale",):
        if field in raw:
            assert isinstance(raw[field], bool)


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        pytest.param(row["file"], row["reason"], id=Path(row["file"]).stem)
        for row in _rows(valid=False)
    ],
)
def test_every_invalid_fixture_is_refused_for_the_declared_reason(
    name: str, reason: str
) -> None:
    """Each of these is a shape this server must never emit — and the manifest
    names WHICH rule refuses it.

    Asserting only "some ValidationError" would let a fixture pass for an
    unrelated reason: a typo in an unrelated field refuses the body too, and the
    rule the fixture exists to demonstrate would never run. The reason is the
    difference between a test of the contract and a test of the file.

    They are kept as FILES rather than inline dicts so the CLI can assert the
    mirror image — that its decoder rejects them too — from the same bytes.
    """
    with pytest.raises(ValidationError, match=reason):
        AccountResponse.model_validate(_fixture(name))


@pytest.mark.parametrize("section", ["account", "entitlement"])
def test_the_manifest_lists_every_fixture_on_disk(section: str) -> None:
    """The tests iterate the MANIFEST, not the directory.

    So a fixture missing from it is a file nothing reads — it sits there looking
    like coverage while proving nothing, and the failure is silent in the
    direction that matters. Both directories are checked here even though the
    entitlement bodies have no semantic consumer yet — that arrives with the
    cross-boundary suite — this is the only test that can see the whole directory,
    so it is the only place a stray file would show.
    """
    listed = {row["file"] for row in _MANIFEST[section]}
    on_disk = {
        f"{section}/{p.relative_to(_CONTRACT / section)}"
        for p in (_CONTRACT / section).rglob("*.json")
    }
    assert on_disk == listed


def test_the_reference_instant_is_a_real_aware_timestamp() -> None:
    """Every entitlement fixture's age is measured from this, so a malformed or
    naive value here would silently move every freshness boundary at once."""
    instant = datetime.fromisoformat(_MANIFEST["reference_instant"].replace("Z", "+00:00"))
    assert instant.tzinfo is not None
    assert instant == datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)


def test_the_identity_only_shape_is_exactly_what_the_brief_locked() -> None:
    """The three-field answer, spelled out.

    This is the specific body the CLI rejected while requiring ``checked_at``, so
    it is asserted as a literal rather than only as "the fixture parses".
    """
    assert _fixture("account/unverified.json") == {
        "version": 1,
        "subject_hash": "b4c864ea44cbb4a1",
        "access": "unverified",
    }


def test_the_version_the_model_emits_is_the_version_the_manifest_declares() -> None:
    assert _MANIFEST["account_contract_version"] == ACCOUNT_CONTRACT_VERSION
    assert AccountResponse(subject_hash=_HASH, access="unverified").version == 1


# --- the individual rules --------------------------------------------------


def test_unverified_cannot_carry_a_lookup_s_results() -> None:
    """``unverified`` means no lookup happened, so it cannot report one's answer.

    A client reading `access: unverified` beside `plan_id: standard` has been told
    two contradictory things about whether anybody asked.
    """
    with pytest.raises(ValidationError, match="cannot carry entitlement fields"):
        AccountResponse(subject_hash=_HASH, access="unverified", plan_id="standard")


def test_granted_requires_a_named_plan() -> None:
    """Access to something unnamed cannot later be attributed or capped.

    Also pins PRECEDENCE: this body is missing `checked_at` and
    `entitlement_source` too, and the plan is still the thing reported — the
    specific contradiction beats the first absent field.
    """
    with pytest.raises(ValidationError, match="requires a plan_id"):
        AccountResponse(subject_hash=_HASH, access="granted")


@pytest.mark.parametrize(
    ("missing", "match"),
    [
        pytest.param("checked_at", "requires checked_at", id="checked_at"),
        pytest.param("entitlement_source", "requires an entitlement_source", id="source"),
        pytest.param("entitlement_stale", "requires entitlement_stale", id="stale"),
    ],
)
def test_a_lookup_that_happened_reports_when_and_from_where(
    missing: str, match: str
) -> None:
    """Absent is not a weaker version of these.

    A client cannot tell "we checked and will not say when" from "we did not
    check", and keeping those two apart is the whole reason `unverified` exists.
    """
    complete: dict[str, Any] = {
        "subject_hash": _HASH,
        "access": "granted",
        "plan_id": "standard",
        "entitlement_source": "polar",
        "entitlement_stale": False,
        "checked_at": datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC),
    }
    del complete[missing]
    with pytest.raises(ValidationError, match=match):
        AccountResponse(**complete)


@pytest.mark.parametrize(
    ("source", "stale", "access", "match"),
    [
        pytest.param(
            "polar", True, "granted", "only valid with entitlement_source 'cache'",
            id="polar_cannot_be_stale",
        ),
        pytest.param(
            "cache", False, "granted", "'cache' is always stale", id="cache_is_never_fresh"
        ),
        pytest.param(
            "cache", True, "subscription_required", "may only report 'granted'",
            id="no_cached_refusal",
        ),
        pytest.param(
            "cache", True, "subscription_inactive", "may only report 'granted'",
            id="no_cached_inactive",
        ),
    ],
)
def test_the_closed_table_is_mirrored_from_the_entitlement_client(
    source: str, stale: bool, access: str, match: str
) -> None:
    """The same `(source, stale, access)` rules the client enforces on the way in.

    Unreachable through the route — the client refuses these bodies before a
    verdict exists — and mirrored anyway, so a later edit to one layer cannot
    leave the two disagreeing about what is legal.
    """
    with pytest.raises(ValidationError, match=match):
        AccountResponse(
            subject_hash=_HASH,
            access=access,  # type: ignore[arg-type]
            plan_id="pro" if access == "granted" else None,
            entitlement_source=source,  # type: ignore[arg-type]
            entitlement_stale=stale,
            checked_at=datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC),
        )


def test_a_refusal_may_still_name_the_plan_it_refers_to() -> None:
    """`granted` REQUIRES a plan; no access value FORBIDS one.

    A lapsed Pro subscription is still Pro, and the website says so. Pinned
    because "granted requires a plan" reads easily as "only granted may have one",
    which would refuse an ordinary answer.
    """
    body = AccountResponse(**_fixture("account/subscription_inactive.json"))
    assert body.access == "subscription_inactive"
    assert body.plan_id == "pro"


def test_a_naive_timestamp_is_refused() -> None:
    """It would serialize without an offset and be read in whatever zone the
    client assumes — silently shifting how fresh a billing answer looks."""
    with pytest.raises(ValidationError):
        AccountResponse(
            subject_hash=_HASH,
            access="granted",
            plan_id="standard",
            entitlement_source="polar",
            entitlement_stale=False,
            checked_at=datetime(2026, 8, 25, 12, 0, 0),  # noqa: DTZ001 - the point
        )


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("NOTAHASH", id="not_hex"),
        pytest.param("B4C864EA44CBB4A1", id="uppercase"),
        pytest.param("b4c864ea44cbb4a", id="too_short"),
        pytest.param("b4c864ea44cbb4a11", id="too_long"),
        pytest.param("", id="empty"),
    ],
)
def test_the_subject_hash_shape_is_enforced(value: str) -> None:
    """It is the one identifier that travels, and the CLI's format is 16 lowercase
    hex characters. A value outside that shape will not correlate with anything."""
    with pytest.raises(ValidationError):
        AccountResponse(subject_hash=value, access="unverified")


def test_an_unknown_access_value_is_refused() -> None:
    with pytest.raises(ValidationError):
        AccountResponse(subject_hash=_HASH, access="probably")  # type: ignore[arg-type]


def test_an_unknown_plan_is_refused() -> None:
    with pytest.raises(ValidationError):
        AccountResponse(
            subject_hash=_HASH,
            access="granted",
            plan_id="enterprise",  # type: ignore[arg-type]
            entitlement_source="polar",
            entitlement_stale=False,
            checked_at=datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC),
        )


def test_an_unexpected_field_is_a_construction_error() -> None:
    """`extra="forbid"`: a field arriving here by accident should be a build
    failure, not a surprise on somebody's terminal."""
    with pytest.raises(ValidationError):
        AccountResponse.model_validate(
            {"subject_hash": _HASH, "access": "unverified", "quota_remaining": 5}
        )


def test_a_false_stale_flag_survives_exclude_none() -> None:
    """`False` is not `None`. A grant that was checked and is fresh must SAY so —
    dropping the flag would make it indistinguishable from one nobody checked."""
    dumped = AccountResponse(
        subject_hash=_HASH,
        access="granted",
        plan_id="pro",
        entitlement_source="polar",
        entitlement_stale=False,
        checked_at=datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC),
    ).model_dump(mode="json", exclude_none=True)
    assert dumped["entitlement_stale"] is False
    assert dumped["checked_at"] == "2026-08-26T12:00:00Z"


def _granted(status: str) -> dict[str, Any]:
    """A complete `granted` body, varying only the field under test.

    Complete because the model validator runs after the field validators: a body
    missing `checked_at` would fail for a reason that has nothing to do with the
    status, and the test would pass while proving nothing.
    """
    return {
        "subject_hash": _HASH,
        "access": "granted",
        "plan_id": "standard",
        "subscription_status": status,
        "entitlement_source": "polar",
        "entitlement_stale": False,
        "checked_at": datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC),
    }


def test_the_status_bound_is_inclusive_at_sixty_four_bytes() -> None:
    """The agreed maximum is USABLE, not the first refused value.

    Same posture as the entitlement client's own bounds: a limit that refuses at
    exactly its stated number leaves a producer racing its own boundary.
    """
    body = AccountResponse(**_granted("a" * 64))
    assert body.subscription_status == "a" * 64
    assert body.model_dump(mode="json", exclude_none=True)["subscription_status"] == "a" * 64

    with pytest.raises(ValidationError, match="exceeds 64 UTF-8 bytes"):
        AccountResponse(**_granted("a" * 65))


def test_the_status_bound_counts_bytes_not_characters() -> None:
    """The whole point of the change, and the case the old bound let through.

    22 CJK characters are 66 UTF-8 bytes and 22 characters, so `max_length=64`
    admitted them while the producer — which measures the unit the cross-service
    contract is written in — refuses them. Two layers stating the same contract
    have to state the SAME contract.
    """
    over = "あ" * 22
    assert len(over) <= 64 and len(over.encode("utf-8")) == 66
    with pytest.raises(ValidationError, match="exceeds 64 UTF-8 bytes"):
        AccountResponse(**_granted(over))

    under = "あ" * 21  # 63 bytes
    assert AccountResponse(**_granted(under)).subscription_status == under


def test_a_lone_surrogate_status_is_a_verdict_and_never_an_encoding_crash() -> None:
    """`utf8_len`, not `.encode` — see its docstring in `text_utils`.

    `json.loads` accepts `"\\ud800"` and pydantic passes it straight through, so a
    bare encode inside a validator raises `UnicodeEncodeError`, which is not a
    `ValidationError` and would answer 500 where this model should simply decide.
    The producer measures with the same helper, so both bounds agree here too.
    """
    assert AccountResponse(**_granted("\ud800")).subscription_status == "\ud800"
    with pytest.raises(ValidationError, match="exceeds 64 UTF-8 bytes"):
        AccountResponse(**_granted("\ud800" * 65))


def test_the_status_refusal_names_the_field_and_not_the_value() -> None:
    """The message says what rule broke, never what broke it.

    This field is free-form text the WEBSITE chose, and
    `services.entitlement._without_echoed_secrets` exists because that website
    could echo a bearer into it — so the message we author must not carry it into
    a log line. (Pydantic's error envelope still records `input` for a local
    caller; what this pins is the sentence, which is the part that propagates.)
    """
    secret = "sk-live-" + "9" * 60
    with pytest.raises(ValidationError) as exc:
        AccountResponse(**_granted(secret))
    message = exc.value.errors()[0]["msg"]
    assert "subscription_status exceeds 64 UTF-8 bytes" in message
    assert secret not in message
