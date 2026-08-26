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

import os
import re
import subprocess
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


# --- the entitlement URL, validated by RUNNING the check ----------------------


def _validation_script() -> str:
    """The deploy step's validation, as runnable bash.

    EXTRACTED AND EXECUTED rather than grepped. A substring assertion on shell
    source proves the text is present, not that it does anything — and shell is
    exactly where a rule can be present and wrong, because `case` patterns,
    quoting and `$$` escaping all fail silently. The block below has to actually
    accept and reject.

    Cloud Build expands `$$` to a literal `$` before the shell sees it, so the
    same substitution is applied here; `$${...}` parameter expansions survive it
    unchanged.
    """
    step = next(
        s
        for s in yaml.safe_load(_CLOUDBUILD.read_text(encoding="utf-8"))["steps"]
        if s.get("id") == "deploy-rc"
    )
    body: str = step["args"][-1]
    start = body.index("# And the SHAPE of that URL")
    end = body.index("# together-api-key is REQUIRED", start)
    block = body[start:end]
    # Extraction lifts the block OUT of its control flow, so on its own this
    # proves the rules work and not that the build runs them. A block placed
    # after an `exit`, or inside `if false`, would test green and never fire.
    # `test_the_validation_actually_runs_before_the_deploy` is the other half.
    assert block.strip(), "the validation block extracted empty"
    return block.replace("$$", "$")


def test_the_validation_actually_runs_before_the_deploy() -> None:
    """The half `_validation_script` cannot prove by executing anything.

    It runs the rules in isolation, so it would stay green if the block were
    moved somewhere the build never reaches. This checks the block is inside the
    step, ahead of the `gcloud run deploy`, and not behind an early exit.
    """
    step = next(
        s
        for s in yaml.safe_load(_CLOUDBUILD.read_text(encoding="utf-8"))["steps"]
        if s.get("id") == "deploy-rc"
    )
    body: str = step["args"][-1]
    start = body.index("# And the SHAPE of that URL")
    assert "gcloud run deploy" in body[start:], "the validation runs after the deploy"

    # What would actually skip it, which is narrower than "any exit above it":
    # every `exit 1` in the preamble is a guarded refusal — the other validations
    # doing their job — and one of those firing means the build stops, which is
    # safe. The dangerous shapes are an early SUCCESSFUL exit, or a guard that is
    # never true wrapping the block.
    preamble = body[:start]
    assert "exit 0" not in preamble, "an early successful exit would skip the validation"
    for guard in ("if false", "if [ 1 -eq 0 ]", "if /bin/false"):
        assert guard not in body, f"the validation is wrapped in a guard that never runs: {guard}"


def _validate_url(value: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - a script this repo owns, run on fixed input
        ["bash", "-c", _validation_script()],
        env={"PATH": os.environ["PATH"], "DAINTREE_ENTITLEMENT_URL": value},
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://staging.daintree.org/api/assistant/entitlement", id="staging"),
        pytest.param("https://daintree.org/api/assistant/entitlement", id="apex"),
        pytest.param("https://www.daintree.org/api/assistant/entitlement", id="another_subdomain"),
        pytest.param("", id="unset"),
    ],
)
def test_the_build_accepts_an_entitlement_url_it_should_not_block(url: str) -> None:
    result = _validate_url(url)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("url", "because"),
    [
        pytest.param(
            "http://staging.daintree.org/api/assistant/entitlement",
            "https",
            id="plain_http",
        ),
        pytest.param("http://127.0.0.1:5173/api", "https", id="loopback"),
        pytest.param(
            "https://staging.daintree.org/api?user=someone",
            "query string",
            id="query_string",
        ),
        pytest.param("https://staging.daintree.org/api#frag", "query string", id="fragment"),
        pytest.param("https://u:p@staging.daintree.org/api", "userinfo", id="userinfo"),
        pytest.param("https://daintree.com/api/assistant/entitlement", "daintree.org", id="wrong_tld"),
        # The two spellings a substring test on the whole URL would wave through,
        # and the reason the host is parsed out and compared on its own.
        pytest.param(
            "https://daintree.org.evil.example/api", "daintree.org", id="suffix_lookalike"
        ),
        pytest.param(
            "https://evil.example/daintree.org", "daintree.org", id="path_lookalike"
        ),
        pytest.param(
            "https://notdaintree.org/api", "daintree.org", id="prefix_lookalike"
        ),
        pytest.param("https://staging.daintree.org/a b", "whitespace", id="embedded_space"),
        pytest.param("   ", "whitespace", id="whitespace_only"),
        # A backslash is read as `/` by URL parsers, so the real host here is
        # `evil.example` — while a naive shell split sees no `/`, keeps the whole
        # string, and matches it against `*.daintree.org`. The most dangerous
        # spelling on this list, because it looks like a subdomain in a log line.
        pytest.param(
            "https://evil.example\\.daintree.org/api", "backslash", id="backslash_host_spoof"
        ),
        pytest.param(
            "https://daintree.org:not-a-port/api", "non-numeric port", id="non_numeric_port"
        ),
        pytest.param("https://daintree.org:99999/api", "out of range", id="port_out_of_range"),
        pytest.param("https://daintree.org:0/api", "out of range", id="port_zero"),
        # The route itself. Every case below is on an ALLOWED host and would have
        # shipped before this rule existed — which is the point of them: the host
        # check cannot tell a billing endpoint from a form handler, a logging
        # route or a preview deployment on the same site, and the caller's bearer
        # is attached to whichever one is written here.
        pytest.param(
            "https://staging.daintree.org/", "exactly the path", id="path_root"
        ),
        pytest.param(
            "https://staging.daintree.org", "exactly the path", id="path_absent"
        ),
        pytest.param(
            "https://staging.daintree.org/api/assistant/entitlements",
            "exactly the path",
            id="path_sibling_plural",
        ),
        pytest.param(
            "https://staging.daintree.org/api/assistant/account",
            "exactly the path",
            id="path_sibling_route",
        ),
        pytest.param(
            "https://staging.daintree.org/api/assistant/entitlementX",
            "exactly the path",
            id="path_suffix_glued",
        ),
        pytest.param(
            "https://staging.daintree.org/x/api/assistant/entitlement",
            "exactly the path",
            id="path_prefixed",
        ),
        pytest.param(
            "https://staging.daintree.org/api//assistant/entitlement",
            "exactly the path",
            id="path_double_slash",
        ),
        pytest.param(
            "https://staging.daintree.org/api/assistant/%65ntitlement",
            "exactly the path",
            id="path_percent_encoded",
        ),
        pytest.param(
            "https://staging.daintree.org/api/%2e%2e/api/assistant/entitlement",
            "exactly the path",
            id="path_encoded_dot_segments",
        ),
        pytest.param(
            "https://staging.daintree.org/api/assistant/entitlement/",
            "exactly the path",
            id="path_trailing_slash",
        ),
        pytest.param(
            "https://staging.daintree.org/API/assistant/entitlement",
            "exactly the path",
            id="path_upper_case",
        ),
    ],
)
def test_the_build_refuses_an_entitlement_url_it_must_not_forward_a_bearer_to(
    url: str, because: str
) -> None:
    """The failure this closes: a URL that is only checked when a mode BINDS is
    one that gets typed wrong months before anybody finds out.

    Readiness catches every case here and would abort the release safely — but as
    a 503 on a candidate revision three steps later, which tells an operator that
    something is wrong rather than that they typed `daintree.com`. This request
    forwards the caller's own account credential, so the host is the one value
    that cannot be allowed to drift quietly.
    """
    result = _validate_url(url)
    assert result.returncode == 1, result.stdout
    assert because in result.stderr


def test_the_build_refuses_a_sample_of_what_readiness_refuses() -> None:
    """A SAMPLE, deliberately — this does not prove the two rules are equivalent.

    Proving that would mean enumerating URL space, and the two are not in fact
    identical: the build is stricter in places (it refuses an uppercase host and
    a `@` anywhere, both of which readiness accepts). Strictness in the build is
    harmless — it fails the deploy that made the mistake. The direction that
    would matter is the build accepting something readiness refuses, and each
    case below is checked against the real `Settings` object rather than against
    a copy of its prose, so a drift in that direction shows up here.
    """
    from daintree_assistant_server.config import Settings

    for url in (
        "http://staging.daintree.org/api",
        "https://staging.daintree.org/api?user=someone",
        "https://daintree.com/api",
        "https://daintree.org.evil.example/api",
        "https://daintree.org:not-a-port/api",
        "   ",
        # The route, on a host both halves allow — so the ONLY thing that can
        # refuse these is the path rule, in each half independently.
        "https://staging.daintree.org/",
        "https://staging.daintree.org",
        "https://staging.daintree.org/api/assistant/entitlements",
        "https://staging.daintree.org/api/assistant/entitlementX",
        "https://staging.daintree.org/x/api/assistant/entitlement",
        "https://staging.daintree.org/api//assistant/entitlement",
        "https://staging.daintree.org/api/assistant/%65ntitlement",
        "https://staging.daintree.org/api/%2e%2e/api/assistant/entitlement",
        "https://staging.daintree.org/api/assistant/entitlement/",
        "https://staging.daintree.org/API/assistant/entitlement",
    ):
        settings = Settings(
            TOGETHER_API_KEY="tgp_v1_test-key",
            STATE_SIGNING_KEY_CURRENT="test-signing-key",
            ENVIRONMENT="staging",
            DAINTREE_ENTITLEMENT_URL=url,
        )
        assert settings.entitlement_problems(), f"readiness accepts {url}"
        assert _validate_url(url).returncode == 1, f"the build accepts {url}"


def test_the_build_and_readiness_agree_on_the_one_url_that_should_ship() -> None:
    """The other direction, and the one a refusal-only suite cannot show.

    A path rule that is wrong in the STRICT direction fails closed and is caught
    by whoever tries to deploy — but only if somebody tries. Both halves have to
    accept the canonical spelling, and each half is checked here against its own
    implementation rather than against a shared constant, so a typo in either one
    is a failure rather than a matching pair of mistakes.
    """
    from daintree_assistant_server.config import Settings

    url = "https://staging.daintree.org/api/assistant/entitlement"
    settings = Settings(
        TOGETHER_API_KEY="tgp_v1_test-key",
        STATE_SIGNING_KEY_CURRENT="test-signing-key",
        ENVIRONMENT="staging",
        DAINTREE_ENTITLEMENT_URL=url,
    )
    assert settings.entitlement_problems() == []
    result = _validate_url(url)
    assert result.returncode == 0, result.stderr


def test_the_build_never_echoes_the_path_it_refused() -> None:
    """The refusal one line above a credential.

    An operator who pastes a bearer into this variable produces a value whose
    PATH can carry it, and the build's stderr is a CI log a great many people can
    read. The `@` branch above catches the classic `https://token@host/` spelling
    before anything echoes, and this branch must not reopen the hole from the
    other side — so it names the route we require and nothing the operator wrote.
    """
    result = _validate_url("https://staging.daintree.org/api/assistant/sekrit-token-value")
    assert result.returncode == 1
    assert "sekrit-token-value" not in result.stderr
    assert "sekrit-token-value" not in result.stdout


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
