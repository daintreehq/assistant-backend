"""The `/v1/daintree/auth/config` manifest, checked against its actual consumer.

The CLI does not merely read this manifest — it VALIDATES it before using any of
it (`internal/auth/discovery.go`), and refuses the whole thing on a mismatch. So
the two sides are one contract with two implementations, and every rule this
backend enforces exists to keep a bad configuration from reaching a user's machine
at all.

That makes the failure mode specific and nasty: a field renamed, a version bumped
or a trust anchor widened on one side is invisible until someone tries to sign in,
at which point the error arrives on their laptop rather than in our CI. These
tests read the CLI's own source and compare.

SKIPPED while the vendored pin predates `internal/auth/` — see the note on the
error-taxonomy gate and `TODO.md`. Verified by hand against the sibling checkout
when written.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from daintree_assistant_server.api.daintree_auth import (
    _AUTH_CONFIG_VERSION,
    _OAUTH_SCOPES,
    _auth_config_body,
)
from daintree_assistant_server.config import (
    _ALLOWED_ISSUER_SUFFIXES,
    _ALLOWED_LINK_SUFFIXES,
    _MAX_CLIENT_ID_LENGTH,
    OAUTH_REDIRECT_URI,
    Settings,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_DISCOVERY_GO = REPO_ROOT / "cli" / "internal" / "auth" / "discovery.go"
CLI_LOOPBACK_GO = REPO_ROOT / "cli" / "internal" / "auth" / "loopback.go"


def _cli_source() -> str:
    if not CLI_DISCOVERY_GO.exists():
        pytest.skip(
            "cli/internal/auth/discovery.go is absent — the submodule is not "
            "populated, or its pin predates the CLI's OAuth discovery "
            "(git submodule update --init cli, then move the pin; see TODO.md)"
        )
    return CLI_DISCOVERY_GO.read_text(encoding="utf-8")


def _go_string_list(source: str, name: str) -> list[str]:
    """The string literals from a `var <name> = []string{...}` block."""
    match = re.search(rf"var {name} = \[\]string\{{(.*?)\}}", source, re.DOTALL)
    assert match, f"could not find `var {name} = []string{{...}}` in discovery.go"
    body = "\n".join(
        line for line in match.group(1).splitlines() if not line.strip().startswith("//")
    )
    return re.findall(r'"([^"]+)"', body)


def _go_bool_map_keys(source: str, name: str) -> set[str]:
    """The keys from a `var <name> = map[string]bool{...}` block."""
    match = re.search(rf"var {name} = map\[string\]bool\{{(.*?)\}}", source, re.DOTALL)
    assert match, f"could not find `var {name} = map[string]bool{{...}}` in discovery.go"
    body = "\n".join(
        line for line in match.group(1).splitlines() if not line.strip().startswith("//")
    )
    return set(re.findall(r'"([^"]+)"\s*:\s*true', body))


def test_the_manifest_version_matches_the_version_the_cli_understands() -> None:
    """The client checks this FIRST and refuses everything else on a mismatch, so
    a disagreement here is not a degraded login — it is no login at all, for every
    user, with an error naming a number nobody recognises."""
    source = _cli_source()
    match = re.search(r"^const SupportedManifestVersion = (\d+)", source, re.MULTILINE)
    assert match, "could not read SupportedManifestVersion from discovery.go"
    assert int(match.group(1)) == _AUTH_CONFIG_VERSION


def test_every_field_the_cli_decodes_is_a_field_we_publish() -> None:
    """A field the client reads and we never send arrives as a zero value, which
    its validator then rejects for a reason that names the wrong problem."""
    source = _cli_source()
    struct = re.search(r"type Manifest struct \{(.*?)\n\}", source, re.DOTALL)
    assert struct, "could not find `type Manifest struct` in discovery.go"
    decoded = set(re.findall(r'json:"([^",]+)"', struct.group(1)))
    # SessionPolicy is a nested struct, so its fields are decoded from the
    # `session_policy` object rather than the top level. PARSED rather than
    # hardcoded: two names written out here would let a rename on the other side
    # pass a test whose whole job is to catch renames.
    nested_struct = re.search(r"type SessionPolicy struct \{(.*?)\n\}", source, re.DOTALL)
    assert nested_struct, "could not find `type SessionPolicy struct` in discovery.go"
    nested = set(re.findall(r'json:"([^",]+)"', nested_struct.group(1)))
    assert nested, "parsed no fields out of SessionPolicy"

    settings = Settings(
        TOGETHER_API_KEY="tgp_v1_test-key",
        STATE_SIGNING_KEY_CURRENT="test-signing-key",
        SUPABASE_AUTH_ISSUER="https://abcdefghij.supabase.co/auth/v1",
        SUPABASE_JWKS_URL="https://abcdefghij.supabase.co/auth/v1/.well-known/jwks.json",
        SUPABASE_OAUTH_AUTHORIZE_URL="https://abcdefghij.supabase.co/auth/v1/oauth/authorize",
        SUPABASE_OAUTH_TOKEN_URL="https://abcdefghij.supabase.co/auth/v1/oauth/token",
        SUPABASE_OAUTH_CLIENT_ID="daintree-assistant",
        SUPABASE_OAUTH_REDIRECT_URI="http://127.0.0.1:42813/oauth/callback",
        DAINTREE_ACCOUNT_URL="https://staging.daintree.org/account",
        DAINTREE_SUBSCRIBE_URL="https://staging.daintree.org/subscribe",
    )
    body = _auth_config_body(settings)
    assert body["configured"] is True
    published = set(body) | nested
    missing = sorted((decoded - nested) - published)
    assert not missing, (
        f"the CLI decodes {missing} from this manifest and we never send them — "
        "each would arrive as a zero value and be rejected for the wrong reason"
    )
    assert set(body["session_policy"]) == nested  # type: ignore[arg-type]


def test_the_environment_we_report_is_one_the_cli_accepts() -> None:
    """`environment` is checked against a closed set. A value outside it fails the
    whole manifest, so this repo's environment enum and the CLI's must agree."""
    allowed = _go_bool_map_keys(_cli_source(), "allowedEnvironments")
    annotation = Settings.model_fields["environment"].annotation
    ours = set(get_args(annotation))
    assert ours and all(isinstance(v, str) for v in ours), (
        "the environment field is no longer a string Literal, so this comparison "
        "is reading something other than the set of environments we publish"
    )
    assert ours <= allowed, sorted(ours - allowed)


def test_every_scope_we_request_is_one_the_cli_permits() -> None:
    allowed = _go_bool_map_keys(_cli_source(), "allowedScopes")
    assert set(_OAUTH_SCOPES) <= allowed, sorted(set(_OAUTH_SCOPES) - allowed)


def test_the_trust_anchors_agree() -> None:
    """Both sides pin these, and the backend's job is to be the STRICTER of the
    two: a host we would publish and the client would refuse is a login that fails
    on a user's machine instead of at our readiness check."""
    source = _cli_source()
    cli_issuers = set(_go_string_list(source, "allowedIssuerSuffixes"))
    cli_links = set(_go_string_list(source, "allowedLinkSuffixes"))
    assert set(_ALLOWED_ISSUER_SUFFIXES) <= cli_issuers, (
        f"we would publish an issuer under {sorted(set(_ALLOWED_ISSUER_SUFFIXES) - cli_issuers)}, "
        "which the CLI refuses"
    )
    assert set(_ALLOWED_LINK_SUFFIXES) <= cli_links, (
        f"we would publish a link under {sorted(set(_ALLOWED_LINK_SUFFIXES) - cli_links)}, "
        "which the CLI refuses"
    )


def test_the_client_id_bound_is_not_looser_than_the_cli_s() -> None:
    source = _cli_source()
    match = re.search(r"^const maxClientIDLen = (\d+)", source, re.MULTILINE)
    assert match, "could not read maxClientIDLen from discovery.go"
    assert int(match.group(1)) >= _MAX_CLIENT_ID_LENGTH


def test_the_published_callback_is_the_one_the_client_compiled_in() -> None:
    """The single most important value in the manifest, and the one the client
    compares for EQUALITY rather than validating structurally.

    A disagreement here is not a degraded login: the client refuses the whole
    manifest with "the backend expects the sign-in callback at X; this build only
    accepts Y", and no amount of correct configuration around it helps. Rebuilt
    from the client's own three constants rather than compared against a copied
    string, so a change to any of them is caught.
    """
    if not CLI_LOOPBACK_GO.exists():
        pytest.skip(
            "cli/internal/auth/loopback.go is absent — the submodule is not "
            "populated, or its pin predates the CLI's OAuth loopback (see TODO.md)"
        )
    source = CLI_LOOPBACK_GO.read_text(encoding="utf-8")
    parts = {}
    for name, pattern in (
        ("host", r'CallbackHost\s*=\s*"([^"]+)"'),
        ("port", r"CallbackPort\s*=\s*(\d+)"),
        ("path", r'CallbackPath\s*=\s*"([^"]+)"'),
    ):
        match = re.search(pattern, source)
        assert match, f"could not read Callback{name.title()} from loopback.go"
        parts[name] = match.group(1)
    expected = f"http://{parts['host']}:{parts['port']}{parts['path']}"
    assert expected == OAUTH_REDIRECT_URI
