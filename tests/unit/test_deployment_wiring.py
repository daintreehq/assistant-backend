"""The deployment wiring for account settings, checked offline.

Cloud Build substitutions are wired in THREE places that must agree: the
`substitutions` block declares them, the deploy step's `env` maps them onto
environment variables, and a Python heredoc lists the names it will actually pass
to `gcloud run deploy`. Miss the third and the setting is declared, mapped, and
silently never applied — a revision that reports itself healthy while running the
default posture.

That is the same shape of bug the two CLI fixtures already have a gate for, and it
is invisible to every other check: the YAML parses, the build succeeds, and the
damage only surfaces as "we set enforce and it kept serving everybody".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_CLOUDBUILD = _ROOT / "cloudbuild.yaml"
_SMOKE = _ROOT / "infra" / "gcp" / "smoke.sh"

#: Every setting the account work added, as (substitution, environment variable).
_ACCOUNT_SETTINGS = [
    ("_ENTITLEMENT_MODE", "ENTITLEMENT_MODE"),
    ("_DAINTREE_ENTITLEMENT_URL", "DAINTREE_ENTITLEMENT_URL"),
    ("_AUTH_MODE", "AUTH_MODE"),
    ("_SUPABASE_AUTH_ISSUER", "SUPABASE_AUTH_ISSUER"),
    ("_SUPABASE_OAUTH_CLIENT_ID", "SUPABASE_OAUTH_CLIENT_ID"),
    ("_DAINTREE_ACCOUNT_URL", "DAINTREE_ACCOUNT_URL"),
    ("_DAINTREE_SUBSCRIBE_URL", "DAINTREE_SUBSCRIBE_URL"),
]


@pytest.fixture(scope="module")
def cloudbuild() -> dict[str, Any]:
    parsed: dict[str, Any] = yaml.safe_load(_CLOUDBUILD.read_text(encoding="utf-8"))
    return parsed


@pytest.fixture(scope="module")
def deploy_step(cloudbuild: dict[str, Any]) -> dict[str, Any]:
    step: dict[str, Any] = next(
        s for s in cloudbuild["steps"] if s.get("id") == "deploy-rc"
    )
    return step


@pytest.mark.parametrize("substitution,env_name", _ACCOUNT_SETTINGS)
def test_every_account_setting_is_declared_mapped_and_passed(
    cloudbuild: dict[str, Any],
    deploy_step: dict[str, Any],
    substitution: str,
    env_name: str,
) -> None:
    """All three places, or the setting does not reach the revision."""
    assert substitution in cloudbuild["substitutions"], "not declared"
    assert f"{env_name}=${{{substitution}}}" in deploy_step["env"], "not mapped to env"
    # The heredoc that builds the --set-env-vars payload. Matched as source text
    # because it is a Python program embedded in a shell script inside YAML, and
    # any attempt to execute it here would be testing the copy rather than the
    # original.
    script = "\n".join(deploy_step["args"])
    names = re.search(r"NAMES = \((.*?)\)", script, re.S)
    assert names is not None, "the NAMES tuple moved; this gate is now blind"
    assert f'"{env_name}"' in names.group(1), "declared and mapped but never passed"


def test_the_shipped_defaults_do_not_move_a_deployment(
    cloudbuild: dict[str, Any],
) -> None:
    """A file that deploys production on every push to main must default to the
    posture production is already in."""
    subs = cloudbuild["substitutions"]
    assert subs["_AUTH_MODE"] == "open"
    assert subs["_ENTITLEMENT_MODE"] == "off"
    # Empty STRING, not null: a null reaches the deploy as the literal "None" in
    # some renderings, and the empty case is the identity-only rollout every
    # deployment is currently in.
    assert subs["_DAINTREE_ENTITLEMENT_URL"] == ""
    assert subs["_SUPABASE_OAUTH_CLIENT_ID"] == ""


def test_no_real_oauth_or_billing_value_is_committed(cloudbuild: dict[str, Any]) -> None:
    """These name a specific Supabase project and a specific website.

    They are not secrets — the discovery endpoint publishes them — but a value
    committed here follows every fork of the file, and the wrong issuer in a
    deployment is every login failing.
    """
    subs = cloudbuild["substitutions"]
    for name in (
        "_SUPABASE_AUTH_ISSUER",
        "_SUPABASE_JWKS_URL",
        "_SUPABASE_OAUTH_AUTHORIZE_URL",
        "_SUPABASE_OAUTH_TOKEN_URL",
        "_SUPABASE_OAUTH_CLIENT_ID",
        "_DAINTREE_ENTITLEMENT_URL",
    ):
        assert subs[name] == "", f"{name} carries a committed value"


def test_the_build_refuses_a_binding_mode_it_cannot_satisfy() -> None:
    """Readiness enforces this too, and would abort the release safely.

    It would do so three steps later, with the operator learning from a 503 that
    the revision never took traffic. Saying it at validation names the actual
    mistake instead.
    """
    text = _CLOUDBUILD.read_text(encoding="utf-8")
    assert 'case "$$ENTITLEMENT_MODE" in' in text
    assert "requires _AUTH_MODE=enforce" in text
    assert "requires _DAINTREE_ENTITLEMENT_URL" in text


# --- the smoke script ---------------------------------------------------------


def test_the_smoke_checks_the_account_endpoint() -> None:
    text = _SMOKE.read_text(encoding="utf-8")
    assert "/v1/daintree/account" in text
    assert "--account-token-var" in text


def test_the_smoke_takes_a_variable_NAME_and_never_a_token() -> None:
    """A credential passed as an argument lands in shell history, in `ps` output,
    and in the CI log line that echoes the command.

    The flag therefore accepts the NAME of an environment variable to read. This
    pins that there is no value-taking spelling of it, and that the read is bash's
    own indirect expansion rather than `eval` — which would re-parse a credential
    as shell.
    """
    text = _SMOKE.read_text(encoding="utf-8")
    assert "--account-token" not in text.replace("--account-token-var", "")
    # `-` inside the expansion: under `set -u` a bare `${!VAR}` on an unset name
    # ABORTS, so the "empty or unset" branch would be unreachable and the smoke
    # would die instead of reporting.
    assert 'ACCOUNT_TOKEN="${!ACCOUNT_TOKEN_VAR-}"' in text
    # `eval` would re-parse a credential as shell.
    assert "eval " not in text
    # Indirect expansion evaluates array subscripts, so an unvalidated name is a
    # command-substitution primitive. The identifier check is what stops a flag
    # that exists to keep a credential OUT of argv becoming a way to get code IN.
    assert "'^[A-Za-z_][A-Za-z0-9_]*$'" in text
    # The credential reaches curl through a --config file on stdin, never argv,
    # where `ps` would show it to every user on the machine.
    assert "--config -" in text
    assert 'header = "Authorization: Bearer' in text
    assert '-H "Authorization: Bearer $ACCOUNT_TOKEN"' not in text


def test_the_smoke_reads_cache_headers_from_a_get_not_a_head() -> None:
    """The account route registers GET only.

    FastAPI does not synthesise HEAD, so `curl -I` answers 405 and a header
    assertion made against it reads an error page — passing or failing for
    reasons that have nothing to do with the route under test.
    """
    text = _SMOKE.read_text(encoding="utf-8")
    account_section = text.split("head_ \"Account status\"", 1)[1]
    assert "curl -sS -I" not in account_section
    assert "-D \"$ACCOUNT_HEADERS_FILE\"" in account_section


def test_the_account_check_is_executable_code_not_only_a_usage_comment() -> None:
    """Guards against the section being deleted while the flag's documentation
    survives — which would leave every string-matching assertion above green."""
    text = _SMOKE.read_text(encoding="utf-8")
    body = [
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    joined = "\n".join(body)
    assert 'head_ "Account status"' in joined
    assert "/v1/daintree/account" in joined
    assert 'ACCOUNT_TOKEN_VAR="$2"' in joined


def test_the_smoke_can_assert_a_posture_rather_than_adapting_to_it() -> None:
    """Every posture check derives its expectation from what auth/config reports.

    So a deployment that shipped `required: false` when it meant `true` adapts to
    the anonymous branch and passes all of them. The script cannot catch that by
    reading harder — somebody has to state the intent.
    """
    text = _SMOKE.read_text(encoding="utf-8")
    assert "--expect-posture" in text
    assert "expected auth posture" in text


def test_the_deploy_actually_consumes_the_generated_env_file() -> None:
    """The three-place wiring test above proves a name is declared, mapped and
    listed — and would still pass if the file it builds stopped being passed to
    `gcloud run deploy` at all."""
    text = _CLOUDBUILD.read_text(encoding="utf-8")
    assert "/workspace/env_vars.json" in text
    assert "--env-vars-file" in text


def test_the_smoke_never_prints_the_token_it_was_given() -> None:
    """Every line that reports on the authenticated check must be derived from the
    SHAPE of the answer, never from the credential or the account behind it."""
    text = _SMOKE.read_text(encoding="utf-8")
    # `$ACCOUNT_TOKEN_VAR` is the variable's NAME and is safe to print — it is how
    # "DAINTREE_TOKEN is empty or unset" names the thing the operator forgot to
    # export. Only a read of the value itself is forbidden, so the boundary
    # matters: a plain substring check cannot tell the two apart.
    forbidden = re.compile(r"\$\{?ACCOUNT_TOKEN\}?(?!_VAR)")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("pass ", "fail ", "echo ")):
            continue
        assert not forbidden.search(stripped), line
