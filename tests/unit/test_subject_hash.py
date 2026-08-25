"""``subject_hash`` is a CROSS-REPO contract, and nothing else enforces it.

The whole value of the id is that a person can read the one their CLI printed and
have it match a log line here. Both sides compute it independently, and a drift in
either — a changed prefix, a different truncation — produces two perfectly good
hashes of the same subject that never compare equal. Nothing would fail; support
would simply stop working.

CI checks the `cli/` submodule out but does not BUILD it, so this pins the
algorithm against literals derived from the Go source
(`cli/internal/auth/status.go`, `SubjectHash`) rather than by running it.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from daintree_assistant_server.auth import ANONYMOUS, Principal, subject_hash

_CLI_SOURCE = Path(__file__).resolve().parents[2] / "cli" / "internal" / "auth" / "status.go"


def test_the_hash_is_sixteen_lowercase_hex_characters() -> None:
    value = subject_hash("8f14e45f-ea8b-4c1d-9b2a-0000feedface")
    assert re.fullmatch(r"[0-9a-f]{16}", value)


def test_the_algorithm_matches_the_cli_byte_for_byte() -> None:
    """Recomputed here from the Go definition rather than trusting our own helper.

    ``hex.EncodeToString(sha256("daintree-assistant-subject:"+subject)[:8])``.
    """
    subject = "8f14e45f-ea8b-4c1d-9b2a-0000feedface"
    expected = hashlib.sha256(
        b"daintree-assistant-subject:" + subject.encode("utf-8")
    ).digest()[:8].hex()
    assert subject_hash(subject) == expected


def test_the_cli_still_defines_it_the_way_we_copied_it() -> None:
    """Fails loudly if a submodule bump moves the algorithm out from under us.

    Skipped-by-absence is deliberate: an unpopulated submodule is a setup state,
    not a regression, and the check above still holds the Python side.
    """
    if not _CLI_SOURCE.exists():  # pragma: no cover - submodule not initialised
        return
    source = _CLI_SOURCE.read_text(encoding="utf-8")
    assert 'sha256.Sum256([]byte("daintree-assistant-subject:" + subject))' in source
    assert "hex.EncodeToString(sum[:8])" in source


def test_it_is_one_way_and_does_not_echo_the_subject() -> None:
    subject = "8f14e45f-ea8b-4c1d-9b2a-0000feedface"
    value = subject_hash(subject)
    assert subject not in value
    assert subject[:8] not in value


def test_an_empty_subject_produces_no_hash() -> None:
    """Matching the CLI, which returns "" rather than the hash of nothing.

    A hash of the empty string would be a stable, plausible-looking id shared by
    every anonymous caller — the one value that must never appear in a support
    queue as if it identified somebody.
    """
    assert subject_hash("") == ""


def test_the_anonymous_principal_has_no_correlation_id() -> None:
    assert ANONYMOUS.subject_hash == ""
    assert ANONYMOUS.is_authenticated is False


def test_an_authenticated_principal_hashes_its_own_subject() -> None:
    from uuid import UUID

    user_id = UUID("8f14e45f-ea8b-4c1d-9b2a-0000feedface")
    principal = Principal(permissions=frozenset(), user_id=user_id)
    assert principal.subject_hash == subject_hash(str(user_id))
    assert str(user_id) not in principal.subject_hash
