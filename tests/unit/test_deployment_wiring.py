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


def test_the_smoke_checks_the_account_endpoint_without_a_credential() -> None:
    """The half of the account contract a gate can actually check.

    The route requires a real principal, so an anonymous request must be refused
    — which is the endpoint working rather than failing, and is true in every
    posture. There is deliberately no authenticated half: see
    `test_the_smoke_has_no_way_to_be_handed_an_access_token`.
    """
    text = _SMOKE.read_text(encoding="utf-8")
    assert "/v1/daintree/account" in text
    assert 'head_ "Account status"' in text


def test_the_smoke_has_no_way_to_be_handed_an_access_token() -> None:
    """The captured-token workflow is gone, and must not come back.

    It took the NAME of an environment variable holding a real access token, and
    the usage it documented obtained that value from a CLI subcommand printing the
    token to stdout — a subcommand the CLI does not have (`internal/cli/auth.go`
    dispatches login, status, logout and disconnect, and nothing else). Access
    tokens are memory-only inside that process on purpose; a facility that prints
    one for a shell to capture demotes the design to a convention, and the first
    place the captured value lands is a CI log.

    Checked as an exact-string absence rather than by reading the account section,
    because the failure this guards against is somebody re-adding the convenience
    — anywhere in the file, comments included.
    """
    text = _SMOKE.read_text(encoding="utf-8")
    for spelling in ("--account-token", "ACCOUNT_TOKEN", "auth token"):
        assert spelling not in text, f"the captured-token workflow is back ({spelling})"


def test_the_smoke_can_assert_a_posture_rather_than_adapting_to_it() -> None:
    """Every posture check derives its expectation from what the deployment says.

    So a revision that shipped `required: false` when it meant `true` adapts to
    the anonymous branch and passes all of them. The script cannot catch that by
    reading harder — somebody has to state the intent.
    """
    text = _SMOKE.read_text(encoding="utf-8")
    assert "--expect-posture" in text
    assert "--expect-entitlement" in text
    assert "expected AUTH_MODE" in text
    assert "expected ENTITLEMENT_MODE" in text


def test_the_smoke_asserts_the_manifest_values_not_only_its_shape() -> None:
    """Type and coherence checks cannot see the wrong PROJECT.

    A revision carrying another environment's OAuth configuration publishes a
    perfectly well-formed manifest. The two values that pin it to this deployment
    are the public client id — which names a specific Supabase project — and the
    redirect URI, which is a fixed contract with the CLI rather than a deployment
    value at all.
    """
    from daintree_assistant_server.config import OAUTH_REDIRECT_URI

    text = _SMOKE.read_text(encoding="utf-8")
    assert "--expect-client-id" in text
    assert OAUTH_REDIRECT_URI in text, (
        "the smoke pins a redirect_uri that config.py no longer serves"
    )


def _run_smoke(*args: str) -> subprocess.CompletedProcess[str]:
    """The real script, on arguments its parser refuses before any network call.

    Only refusals are exercised here. A well-formed invocation would fall through
    to service discovery and then to HTTP, which a unit test has no business
    doing — but every case below exits from the argument loop, which runs first.
    """
    return subprocess.run(  # noqa: S603 - a script this repo owns, on fixed input
        ["bash", str(_SMOKE), *args],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["--expect-build-sha"], id="build_sha_missing_value"),
        pytest.param(["--expect-build-sha", ""], id="build_sha_blank"),
        pytest.param(["--expect-client-id"], id="client_id_missing_value"),
        pytest.param(["--expect-client-id", ""], id="client_id_blank"),
        pytest.param(["--expect-entitlement"], id="entitlement_missing_value"),
        pytest.param(["--expect-entitlement", "on"], id="entitlement_not_a_mode"),
        pytest.param(["--expect-posture", "yes"], id="posture_not_a_mode"),
        pytest.param(["--account-token-var", "TOK"], id="the_removed_token_flag"),
    ],
)
def test_the_smoke_refuses_an_expectation_it_cannot_honour(args: list[str]) -> None:
    """A BLANK value is the case worth executing rather than grepping.

    The natural call passes a build substitution, and an unset one would leave the
    flag present and the check switched off — the single failure a gate must never
    have, because the build log then shows the flag being passed. `case` patterns
    and quoting fail silently in shell, so this runs the real parser rather than
    asserting on its source.
    """
    result = _run_smoke(*args)
    assert result.returncode == 2, result.stdout + result.stderr


# --- the pre-promotion smoke step ---------------------------------------------
#
# Nothing read this step before. It is the last gate a bad revision passes through
# and it inlined its own, weaker copy of two of the assertions in
# infra/gcp/smoke.sh — so a check added to the rich script never reached the gate
# that could have stopped a release, and neither half noticed.


@pytest.fixture(scope="module")
def smoke_step(cloudbuild: dict[str, Any]) -> dict[str, Any]:
    step: dict[str, Any] = next(s for s in cloudbuild["steps"] if s.get("id") == "smoke")
    return step


def test_the_smoke_step_runs_the_shared_script_rather_than_its_own_curls(
    smoke_step: dict[str, Any],
) -> None:
    """ONE definition of what a working deployment is.

    Two definitions drift, and they drift in the direction of the copy nobody
    runs by hand — which was the copy gating production.
    """
    script = "\n".join(smoke_step["args"])
    # The INVOCATION, not a mention. Several comments in this step name the script
    # while explaining why it is used, so a substring test would keep passing after
    # somebody deleted the one line that actually runs it and left the prose behind.
    assert re.search(r"^\s*bash\s+infra/gcp/smoke\.sh\b", script, re.MULTILINE), (
        "the pre-promotion gate no longer executes infra/gcp/smoke.sh"
    )
    # And it must not have grown a second definition beside it. `curl` survives
    # only in the readiness warm-up, which is deliberately this step's own.
    assertions = [
        line
        for line in script.splitlines()
        if "curl" in line and not line.lstrip().startswith("#") and "readyz" not in line
    ]
    assert not assertions, f"the gate is inlining its own checks again: {assertions}"


def test_the_smoke_step_states_every_expectation_the_release_asked_for(
    smoke_step: dict[str, Any],
) -> None:
    """The script ADAPTS to a deployment it is not told about.

    That is what makes it useful against an unknown host and exactly wrong here,
    where the whole question is whether this revision came up as the one that was
    requested. Each flag below is an expectation that is silent if omitted.
    """
    script = "\n".join(smoke_step["args"])
    for flag in (
        "--expect-build-sha",
        "--expect-env",
        "--expect-posture",
        "--expect-entitlement",
        "--expect-client-id",
    ):
        assert flag in script, f"the candidate smoke does not assert {flag}"


@pytest.mark.parametrize(
    ("env_name", "substitution"),
    [
        ("RELEASE_SHA", "$COMMIT_SHA"),
        ("ENVIRONMENT", "${_ENVIRONMENT}"),
        ("AUTH_MODE", "${_AUTH_MODE}"),
        ("ENTITLEMENT_MODE", "${_ENTITLEMENT_MODE}"),
        ("SUPABASE_OAUTH_CLIENT_ID", "${_SUPABASE_OAUTH_CLIENT_ID}"),
    ],
)
def test_the_smoke_step_takes_its_expectations_as_environment_not_as_source(
    smoke_step: dict[str, Any], env_name: str, substitution: str
) -> None:
    """Cloud Build substitutes before bash parses, so an inline `${_FOO}` is CODE.

    These are the values an operator types by hand when raising a staging
    revision, which is exactly where a stray quote — or a `$(…)` — comes from. The
    deploy step already learned this; the gate that follows it has to as well.
    """
    assert f"{env_name}={substitution}" in smoke_step["env"], "not mapped to env"
    script = "\n".join(smoke_step["args"])
    assert substitution not in script, f"{substitution} is interpolated into the script"


def test_the_smoke_step_still_bails_out_on_the_first_deploy(
    smoke_step: dict[str, Any],
) -> None:
    """The candidate URL needs an invoker binding that does not exist yet.

    deploy-rc checks readiness with an identity token instead and drops the
    marker; without this bail-out the very first release of a new project fails
    on a 403 that has nothing to do with the build.
    """
    script = "\n".join(smoke_step["args"])
    assert "/workspace/skip_smoke" in script
    assert "exit 0" in script


def test_the_smoke_step_keeps_the_readiness_warm_up_out_of_the_assertions(
    smoke_step: dict[str, Any],
) -> None:
    """The retry belongs to the step, not to the script.

    `gcloud run deploy` already waited for Ready, so this covers network flakiness
    — and an assertion suite that silently retried would be unable to report a
    route that is only intermittently reachable, which is a real deployment fault.
    """
    script = "\n".join(smoke_step["args"])
    assert "readyz not answering yet" in script


def test_the_promotion_names_the_revision_the_smoke_proved(cloudbuild: dict[str, Any]) -> None:
    """`--to-latest` re-resolves; the gate proved one specific revision.

    `candidate` is a single shared tag, so two overlapping builds are enough:
    build A smokes revision A, build B lands revision B behind the same tag, and
    A's promotion then hands 100% of traffic to a revision nothing tested. Ordering
    the steps cannot fix that — the race is between BUILDS, not between steps — so
    the promotion has to name what the deploy recorded.
    """
    promote = next(s for s in cloudbuild["steps"] if s.get("id") == "promote")
    # CODE only. The step explains at length why it does not say `--to-latest`, so
    # a check over the raw text would be tripped by the comment that documents the
    # rule — the mirror of the vacuous-substring problem, failing where it should
    # pass instead of passing where it should fail.
    script = "\n".join(
        line
        for line in "\n".join(promote["args"]).splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "--to-latest" not in script, (
        "the promotion resolves 'latest' at promotion time instead of promoting the "
        "revision the smoke actually proved"
    )
    assert "--to-revisions" in script and "rc_revision" in script, (
        "the promotion must promote the revision name recorded by the deploy step"
    )
    deploy = next(s for s in cloudbuild["steps"] if s.get("id") == "deploy-rc")
    assert "rc_revision" in "\n".join(deploy["args"]), (
        "the deploy step must record the candidate revision name for the promotion"
    )


def test_the_smoke_step_runs_before_the_promotion(cloudbuild: dict[str, Any]) -> None:
    """Order is the whole value of the gate.

    Cloud Build runs steps in file order unless `waitFor` says otherwise, so a
    smoke that landed after the traffic shift would report on a revision already
    serving users.
    """
    ids = [s.get("id") for s in cloudbuild["steps"]]
    assert ids.index("smoke") < ids.index("promote")
    smoke = next(s for s in cloudbuild["steps"] if s.get("id") == "smoke")
    assert "waitFor" not in smoke, (
        "a waitFor on the smoke step can let it run concurrently with the promotion"
    )
    # File order only holds while NEITHER step overrides it. A `waitFor` naming an
    # earlier step is how promotion would start before the smoke finished — and it
    # would leave the index comparison above still passing, which is exactly the
    # kind of green this gate must not produce.
    promote = next(s for s in cloudbuild["steps"] if s.get("id") == "promote")
    assert "waitFor" not in promote, (
        "a waitFor on the promotion can let it overtake the smoke that gates it"
    )


def test_the_deploy_actually_consumes_the_generated_env_file() -> None:
    """The three-place wiring test above proves a name is declared, mapped and
    listed — and would still pass if the file it builds stopped being passed to
    `gcloud run deploy` at all."""
    text = _CLOUDBUILD.read_text(encoding="utf-8")
    assert "/workspace/env_vars.json" in text
    assert "--env-vars-file" in text


#: The settings CLAUDE.md carves out of the "one hardcoded optimum" rule — the
#: ones whose right value is whatever production says this week. Paired with the
#: environment variable each must arrive as.
_OPERATIONAL_DIALS = (
    ("_TOOL_PROJECTION_MODE", "TOOL_PROJECTION_MODE"),
    ("_TOOL_PROJECTION_ROLLOUT_PERCENT", "TOOL_PROJECTION_ROLLOUT_PERCENT"),
    ("_MAIN_OUTPUT_LINT_ENABLED", "MAIN_OUTPUT_LINT_ENABLED"),
    ("_EXPOSE_DEBUG_SELECTOR_REASON", "EXPOSE_DEBUG_SELECTOR_REASON"),
)


@pytest.mark.parametrize("substitution,env_name", _OPERATIONAL_DIALS)
def test_every_operational_dial_survives_a_release(
    cloudbuild: dict[str, Any],
    deploy_step: dict[str, Any],
    substitution: str,
    env_name: str,
) -> None:
    """A rollback lever a routine release undoes is not a rollback lever.

    `TOOL_PROJECTION_MODE` gates a behaviour that WITHHOLDS tools from the model,
    and `.env.example` tells an operator to roll it back by updating the Cloud Run
    service's environment — a config-only revision, no deploy. That advice was
    true for exactly as long as nobody pushed to `main`: the deploy hands Cloud
    Run an `--env-vars-file`, which REPLACES the whole environment, so a manual
    `--update-env-vars` was silently reverted by the next release and the
    projection came back on with nothing to show it had.

    So the dial has to be settable in the file that does the replacing. Same three
    places as an account setting: declared, mapped, passed.
    """
    assert substitution in cloudbuild["substitutions"], "not declared"
    assert f"{env_name}=${{{substitution}}}" in deploy_step["env"], "not mapped to env"
    script = "\n".join(deploy_step["args"])
    names = re.search(r"NAMES = \((.*?)\)", script, re.S)
    assert names is not None, "the NAMES tuple moved; this gate is now blind"
    assert f'"{env_name}"' in names.group(1), "declared and mapped but never passed"


@pytest.mark.parametrize("substitution,_env", _OPERATIONAL_DIALS)
def test_an_operational_dial_defaults_to_taking_the_shipped_value(
    cloudbuild: dict[str, Any], substitution: str, _env: str
) -> None:
    """Empty, so adding these changed no deployment.

    The Python filter that builds the env file drops blank values, so an empty
    substitution means the variable is never sent and `config.py`'s own default
    is what the revision comes up with. Writing `shadow` here instead would be
    the same value today and a second place to remember on the day it moves.
    """
    assert cloudbuild["substitutions"][substitution] == "", (
        f"{substitution} pins a value that config.py should be deciding"
    )


def _dial_validation_script() -> str:
    """The dial checks, extracted the same way and for the same reason.

    Grepping for a `case` pattern proves the text is there. It does not prove the
    pattern matches — `[1-9][0-9]` and `[1-9]|[0-9]` differ by one character and
    by the whole rule — so this block is executed against real values below.
    """
    step = next(
        s
        for s in yaml.safe_load(_CLOUDBUILD.read_text(encoding="utf-8"))["steps"]
        if s.get("id") == "deploy-rc"
    )
    body: str = step["args"][-1]
    start = body.index("# The operational dials, checked for the same reason")
    end = body.index("# The cross-field rule", start)
    block = body[start:end]
    assert block.strip(), "the dial validation block extracted empty"
    return block.replace("$$", "$")


def _validate_dials(**values: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "TOOL_PROJECTION_MODE": "",
        "TOOL_PROJECTION_ROLLOUT_PERCENT": "",
        "MAIN_OUTPUT_LINT_ENABLED": "",
        "EXPOSE_DEBUG_SELECTOR_REASON": "",
    }
    env.update(values)
    return subprocess.run(  # noqa: S603 - a script this repo owns, run on fixed input
        ["bash", "-c", _dial_validation_script()],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({}, id="all_empty_take_the_defaults"),
        pytest.param({"TOOL_PROJECTION_MODE": "disabled"}, id="the_rollback"),
        pytest.param({"TOOL_PROJECTION_MODE": "enforced"}, id="the_destination"),
        pytest.param({"TOOL_PROJECTION_ROLLOUT_PERCENT": "0"}, id="halt_but_keep_measuring"),
        pytest.param({"TOOL_PROJECTION_ROLLOUT_PERCENT": "1"}, id="first_stage"),
        pytest.param({"TOOL_PROJECTION_ROLLOUT_PERCENT": "25"}, id="mid_stage"),
        pytest.param({"TOOL_PROJECTION_ROLLOUT_PERCENT": "100"}, id="every_session"),
        pytest.param({"MAIN_OUTPUT_LINT_ENABLED": "false"}, id="lint_off"),
        pytest.param({"EXPOSE_DEBUG_SELECTOR_REASON": "true"}, id="debug_on"),
    ],
)
def test_the_build_accepts_every_dial_setting_an_operator_would_use(
    values: dict[str, str],
) -> None:
    """Each of these is a step in a documented procedure. Refusing one would send
    an operator to edit the file during the incident the dial exists for."""
    result = _validate_dials(**values)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"TOOL_PROJECTION_MODE": "enabled"}, id="mode_plausible_wrong_word"),
        pytest.param({"TOOL_PROJECTION_MODE": "Shadow"}, id="mode_wrong_case"),
        pytest.param({"TOOL_PROJECTION_ROLLOUT_PERCENT": "1000"}, id="percent_stray_zero"),
        pytest.param({"TOOL_PROJECTION_ROLLOUT_PERCENT": "-1"}, id="percent_negative"),
        pytest.param({"TOOL_PROJECTION_ROLLOUT_PERCENT": "50%"}, id="percent_with_sign"),
        pytest.param({"MAIN_OUTPUT_LINT_ENABLED": "no"}, id="bool_pydantic_would_take_it"),
        pytest.param({"EXPOSE_DEBUG_SELECTOR_REASON": "1"}, id="bool_numeric"),
    ],
)
def test_the_build_refuses_a_dial_value_that_would_deploy_something_else(
    values: dict[str, str],
) -> None:
    """`1000` is the one that matters: `enforced` at a percentage this let through
    is every session at once, which is the staged rollout defeated by a keystroke.

    `no` and `1` are the other shape — pydantic accepts both, so readiness would
    pass and the revision would come up with a posture nothing in this repo's
    documentation describes.
    """
    result = _validate_dials(**values)
    assert result.returncode != 0, (
        f"accepted {values}, which reaches the revision as a value nothing checked"
    )


# --- one candidate tag per build, proved by RUNNING the recording -------------


def _rc_recording_script() -> str:
    """The tag derivation and metadata recording, as runnable bash.

    Extracted and EXECUTED for the same reason the entitlement validation is.
    The property under test is a race between two builds, and no substring
    assertion can settle it: the old code contained the string `--tag=candidate`
    and the string `rc_revision`, which is everything a grep could ask for, and
    it still recorded whichever revision happened to hold the shared tag when the
    describe ran.

    `FLAGS` is supplied by the harness — it is assembled far above this block and
    carries nothing this test is about.
    """
    step = next(
        s
        for s in yaml.safe_load(_CLOUDBUILD.read_text(encoding="utf-8"))["steps"]
        if s.get("id") == "deploy-rc"
    )
    body: str = step["args"][-1]
    start = body.index('RC_TAG="rc-')
    end = body.index('echo "Release candidate:', start)
    end = body.index("\n", end)
    block = body[start:end]
    assert block.strip(), "the recording block extracted empty"
    return (
        "FLAGS=()\n"
        + block.replace("$$", "$")
        .replace("${_SERVICE}", "daintree-assistant")
        .replace("${_REGION}", "us-central1")
        .replace("$COMMIT_SHA", "abc123")
    )


#: A `gcloud` that behaves like the real one in the ways this test needs, and
#: races like a real overlapping build.
#:
#: `describe` is where the window is: the stub lands a SECOND build's revision
#: between our deploy and our describe, which is precisely the interleaving the
#: shared `candidate` tag could not survive.
#:
#: The interloper's row is emitted FIRST, ahead of ours, on purpose. A recording
#: that reached for `traffic[0]`, or for the newest row, or for anything other
#: than the row carrying our own tag, would read as correct against a state where
#: ours happened to come first — so the fixture puts ours where a careless
#: implementation will not find it.
_GCLOUD_STUB = """#!/bin/bash
set -euo pipefail
STATE="$TRAFFIC_STATE"
row() { printf '{"tag": "%s", "url": "https://%s---svc.run.app", "revisionName": "svc-%s"}' "$1" "$1" "$1"; }
case "$*" in
  *"run deploy"*)
    for arg in "$@"; do
      case "$arg" in
        --tag=*) MINE="${arg#--tag=}" ;;
      esac
    done
    # A tag names ONE revision: deploying under a tag another revision already
    # holds MOVES it rather than adding a second row. That is the mechanism the
    # shared name fell foul of, so the stub models it rather than appending.
    if [ -f "$STATE" ]; then
      grep -v "\\"tag\\": \\"$MINE\\"" "$STATE" > "$STATE.tmp" || true
      mv "$STATE.tmp" "$STATE"
    fi
    row "$MINE" >> "$STATE"
    echo >> "$STATE"
    ;;
  *"run services describe"*"--format=json"*)
    # The overlapping build lands here, between our deploy and our describe.
    printf '{"status": {"traffic": ['
    { row "$INTERLOPER"; echo; cat "$STATE"; } | grep -v '^$' | paste -sd, -
    printf ']}}'
    ;;
  *"run services describe"*)
    ;;
esac
"""


def _run_rc_recording(
    *, build_id: str, interloper: str, tmp_path: Path
) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "gcloud"
    stub.write_text(_GCLOUD_STUB, encoding="utf-8")
    stub.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    script = _rc_recording_script().replace("/workspace", str(workspace))
    result = subprocess.run(  # noqa: S603 - a script this repo owns, on fixed input
        ["bash", "-c", script],
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "BUILD_ID": build_id,
            "INTERLOPER": interloper,
            "TRAFFIC_STATE": str(tmp_path / "traffic"),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result, workspace


def test_a_concurrent_build_cannot_make_us_record_its_revision(tmp_path: Path) -> None:
    """The race, reproduced and closed.

    A second build lands its own revision between our deploy and our describe.
    Under the shared `candidate` tag that was enough to make us record, smoke and
    promote a revision we never deployed; the smoke's build-SHA assertion turned
    most of those into a spurious red build rather than a bad promotion, but a
    gate whose correctness depends on a different assertion firing is not a gate.

    With a tag only this build can name, the interloper's row is simply not ours.

    This proves the FILTER: our own row is found among foreign ones wherever it
    sits. Two other tests carry the rest of the property — uniqueness, so no
    other build can hold our tag, and a guard against the literal `candidate`
    coming back, which is how the filter would end up correct and pointed at a
    shared name again.
    """
    result, workspace = _run_rc_recording(
        build_id="11111111-2222-3333-4444-555555555555",
        interloper="rc-99999999aaaa",
        tmp_path=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    revision = (workspace / "rc_revision").read_text(encoding="utf-8").strip()
    url = (workspace / "rc_url").read_text(encoding="utf-8").strip()
    tag = (workspace / "rc_tag").read_text(encoding="utf-8").strip()
    assert tag == "rc-111111112222"
    assert revision == "svc-rc-111111112222", (
        f"recorded {revision!r} — a concurrent build's revision, not our own"
    )
    assert "99999999" not in revision and "99999999" not in url


def test_two_builds_derive_two_different_tags(tmp_path: Path) -> None:
    """Derived from BUILD_ID, never from the commit.

    Two builds of the SAME commit are exactly the overlapping pair this has to
    separate — a re-run of a failed release, a retry after a flake — and they
    share a commit sha. A tag derived from `COMMIT_SHA` would collide on the one
    case the mechanism exists for.
    """
    tags = set()
    for i, build_id in enumerate(
        ("aaaaaaaa-1111-2222-3333-444444444444", "bbbbbbbb-1111-2222-3333-444444444444")
    ):
        result, workspace = _run_rc_recording(
            build_id=build_id, interloper="rc-none", tmp_path=tmp_path / f"run{i}"
        )
        assert result.returncode == 0, result.stderr
        tags.add((workspace / "rc_tag").read_text(encoding="utf-8").strip())
    assert len(tags) == 2, f"two builds shared one tag: {tags}"


def test_the_derived_tag_is_a_legal_cloud_run_tag(tmp_path: Path) -> None:
    """Cloud Run rejects a tag that is not a DNS label, and it rejects one under
    three characters outright.

    The tag also becomes part of a hostname label beside the service name, so it
    is bounded rather than used whole. `BUILD_ID` is a UUID today; the derivation
    sanitises rather than trusts that, because a tag that fails validation fails
    the deploy AFTER the image has been built and pushed.
    """
    result, workspace = _run_rc_recording(
        build_id="AAAA-BBBB-cccc-dddd-eeee-ffff-0000-1111",
        interloper="rc-none",
        tmp_path=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    tag = (workspace / "rc_tag").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"[a-z][a-z0-9-]{2,62}", tag), tag
    assert tag.islower()
    assert len(tag) <= 15, f"tag {tag!r} is longer than the derivation bounds it to"


def test_a_build_with_no_usable_id_refuses_rather_than_sharing_a_name(
    tmp_path: Path,
) -> None:
    """Falling back to a shared name would reintroduce the collision at the one
    moment nobody is watching.

    An unusable `BUILD_ID` means something is wrong with the build environment,
    not with this release, and the honest response is to stop.
    """
    result, _ = _run_rc_recording(build_id="!!!!", interloper="rc-none", tmp_path=tmp_path)
    assert result.returncode != 0
    assert "refusing to share a tag" in result.stderr


def test_the_promotion_removes_only_this_builds_tag(cloudbuild: dict[str, Any]) -> None:
    """Cleanup used to be `--remove-tags=candidate`, which every concurrent build
    also carried.

    So promoting A deleted the tag B was about to describe, and B failed on a
    revision that was perfectly healthy — the pipeline taking down its own
    sibling. The tag comes from the file the deploy wrote, like the revision name
    beside it.
    """
    promote = next(s for s in cloudbuild["steps"] if s.get("id") == "promote")
    script = "\n".join(
        line
        for line in "\n".join(promote["args"]).splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "--remove-tags=candidate" not in script, (
        "the promotion removes a shared tag that other builds are relying on"
    )
    assert "rc_tag" in script, (
        "the promotion must remove the tag recorded by its own deploy step"
    )
    assert "--remove-tags=" in script


def test_no_step_still_names_the_shared_candidate_tag(cloudbuild: dict[str, Any]) -> None:
    """A single leftover `--tag=candidate` or `tag=="candidate"` puts the whole
    race back, and it would not fail any other test here — the new tag would be
    written, and then ignored."""
    for step in cloudbuild["steps"]:
        script = "\n".join(
            line
            for line in "\n".join(step.get("args", [])).splitlines()
            if not line.lstrip().startswith("#")
        )
        assert "--tag=candidate" not in script, step.get("id")
        assert '"candidate"' not in script, step.get("id")


# --- the smoke step states the WHOLE manifest -------------------------------


#: Every manifest field the release asserts, paired with the deploy substitution
#: that supplies it. `scopes` is absent on purpose: it is hardcoded in the server
#: rather than deployed, and has its own test below.
_MANIFEST_EXPECTATIONS = (
    ("--expect-issuer", "SUPABASE_AUTH_ISSUER"),
    ("--expect-authorize-url", "SUPABASE_OAUTH_AUTHORIZE_URL"),
    ("--expect-token-url", "SUPABASE_OAUTH_TOKEN_URL"),
    ("--expect-jwks-url", "SUPABASE_JWKS_URL"),
    ("--expect-account-url", "DAINTREE_ACCOUNT_URL"),
    ("--expect-subscribe-url", "DAINTREE_SUBSCRIBE_URL"),
    ("--expect-access-token-seconds", "AUTH_ACCESS_TOKEN_SECONDS"),
    ("--expect-session-max-age", "AUTH_SESSION_MAX_AGE_SECONDS"),
)


@pytest.mark.parametrize("flag,env_name", _MANIFEST_EXPECTATIONS)
def test_the_release_states_every_manifest_value_it_deployed(
    flag: str, env_name: str, cloudbuild: dict[str, Any]
) -> None:
    """The deploy proves what Cloud Run was ASKED for; only the manifest proves
    what the revision came up holding.

    The client id was asserted first because it names a Supabase project, which
    left every field beside it checked for shape and not for value — and a
    candidate carrying another project's issuer, token endpoint or JWKS URL
    publishes a body that is typed, coherent and entirely wrong. `configured=true`
    with the right client id is not evidence about the endpoints next to it.
    """
    smoke = next(s for s in cloudbuild["steps"] if s.get("id") == "smoke")
    env = {entry.split("=", 1)[0] for entry in smoke.get("env", [])}
    assert env_name in env, f"the smoke step never receives {env_name}"
    script = "\n".join(smoke["args"])
    assert flag in script, f"the release never states {flag}"
    assert f'"$${env_name}"' in script, (
        f"{flag} is passed, but not from {env_name} — the assertion would be about "
        "a value this deployment did not set"
    )


def test_every_manifest_expectation_is_a_flag_the_smoke_accepts() -> None:
    """A flag the release passes and the script does not know is `exit 2` on
    every deploy — and a flag the script grew that the release never passes is an
    assertion nobody runs. Checked in both directions for that reason."""
    smoke_text = _SMOKE.read_text(encoding="utf-8")
    build_text = _CLOUDBUILD.read_text(encoding="utf-8")
    for flag, _ in _MANIFEST_EXPECTATIONS:
        assert flag in smoke_text, f"{flag} is passed by the build and unknown to the script"
    assert "--expect-scopes" in smoke_text and "--expect-scopes" in build_text


def test_the_released_scopes_are_the_scopes_the_server_publishes() -> None:
    """The one expectation that is not a deployment value.

    `_OAUTH_SCOPES` is hardcoded — Supabase supports the standard set and no
    application-defined ones, so there is nothing a per-environment value could
    express. That makes the released expectation a literal, and a literal beside
    a constant is exactly the pair that drifts: a scope added to the code and not
    to the release fails every deploy, and one added to the release and not the
    code fails nothing until somebody wonders why the assertion never fires.
    """
    from daintree_assistant_server.api.daintree_auth import _OAUTH_SCOPES

    text = _CLOUDBUILD.read_text(encoding="utf-8")
    expected = ",".join(_OAUTH_SCOPES)
    assert f"EXPECTED_SCOPES={expected}" in text, (
        f"the release expects scopes that are not {expected!r} — the literal in "
        "cloudbuild.yaml and _OAUTH_SCOPES in api/daintree_auth.py have drifted"
    )


def test_the_scopes_expectation_rides_with_the_oauth_client() -> None:
    """A deployment publishing no OAuth client publishes no scopes either.

    Stating them unconditionally would hand the script a flag whose value is
    correct and whose field is absent, and production — which ships with no OAuth
    client — would fail its own release gate.
    """
    text = _CLOUDBUILD.read_text(encoding="utf-8")
    index = text.index("--expect-scopes")
    preceding = text[:index]
    assert 'if [ -n "$$SUPABASE_OAUTH_CLIENT_ID" ]; then' in preceding


def test_the_deploy_reads_the_revisions_environment_back() -> None:
    """The gap between "what we asked Cloud Run for" and "what the revision has".

    A stale env-vars file, a flag dropped from FLAGS, a value the platform
    refused and silently omitted, or a revision that is simply not the one just
    deployed all live in that gap, and every one of them yields a candidate that
    passes its own health check.
    """
    deploy = next(
        s
        for s in yaml.safe_load(_CLOUDBUILD.read_text(encoding="utf-8"))["steps"]
        if s.get("id") == "deploy-rc"
    )
    body: str = deploy["args"][-1]
    assert "gcloud run revisions describe" in body
    assert "/workspace/rc_revision" in body, (
        "the environment must be read back from the revision this build recorded, "
        "not from the service, or it describes whatever is serving"
    )
    # Read back by revision, and compared against the file the build generated.
    readback = body[body.index("gcloud run revisions describe") :]
    assert "/workspace/env_vars.json" in readback


def test_the_environment_readback_can_never_print_a_secret() -> None:
    """By construction rather than by filtering.

    Secrets reach the container through `--set-secrets` as `valueFrom`
    references, so the comparison reads `value` and an entry without one is
    skipped — a secret has no value here to print even by accident. The failure
    message names KEYS only for the same reason the URL validator does: one of
    these values is a URL an operator may have pasted a credential into.
    """
    deploy = next(
        s
        for s in yaml.safe_load(_CLOUDBUILD.read_text(encoding="utf-8"))["steps"]
        if s.get("id") == "deploy-rc"
    )
    body: str = deploy["args"][-1]
    readback = body[body.index("gcloud run revisions describe") :]
    assert '"value" in entry' in readback, (
        "the readback must skip entries with no literal value, which is what "
        "excludes every secret reference"
    )
    assert "valueFrom" in readback, "the reason for the filter is not written down"
    # The diagnostics join key names. A formatter interpolating a value would
    # show up as one of these dictionaries being indexed in the message.
    assert 'join(missing)' in readback or '", ".join(missing)' in readback
    assert "got[k]" not in readback.split("ERROR:")[-1], (
        "the failure message reads a value out of the deployed environment"
    )
