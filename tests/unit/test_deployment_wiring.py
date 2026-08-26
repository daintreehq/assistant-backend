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

import json
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


# --- release state: extracted once, used by every executing test --------------
#
# Release state is what the three fallible steps tell each other, and since
# `allowFailure` stopped a failed step from aborting the build it is also the
# VERDICT. It used to live in /workspace, which IS the repository checkout, and
# to be trusted on existence alone — so a tree carrying `stage_deploy_ok` and
# `skip_smoke` walked a failed deploy through the smoke and the promotion and out
# as a green release, and a committed `rc_tag` aimed the cleanup at whatever name
# it named.
#
# It now lives on a build-scoped volume at /release, every file opens with the id
# of the build that wrote it, and `stamp`/`stamped` are the only way in and out.


def _step(step_id: str) -> dict[str, Any]:
    """One step of the build, by id. Asserts it exists — a `next()` over a
    renamed id would otherwise raise StopIteration and read as a broken test
    rather than as a missing step."""
    steps = yaml.safe_load(_CLOUDBUILD.read_text(encoding="utf-8"))["steps"]
    found = [s for s in steps if s.get("id") == step_id]
    assert len(found) == 1, f"expected exactly one {step_id!r} step, found {len(found)}"
    step: dict[str, Any] = found[0]
    return step


_HELPER_START = "# --- release state: read and written only through these"
_HELPER_END = "# --- end release state helpers"

#: Every step that touches release state. The helper block is byte-identical in
#: all four by construction, and a test below compares them character for
#: character — two trust checks that have drifted are worse than one.
_RELEASE_STATE_STEPS = ("deploy-rc", "smoke", "promote", "release-gate")


def _helper_block(step_id: str) -> str:
    """The `stamp`/`stamped` pair as it appears in one step, verbatim."""
    body: str = _step(step_id)["args"][-1]
    start = body.index(_HELPER_START)
    end = body.index(_HELPER_END, start)
    end = body.index("\n", end) + 1
    block = body[start:end]
    assert block.strip(), f"the release-state helpers extracted empty from {step_id}"
    return block


def _release_state_helpers(step_id: str = "deploy-rc") -> str:
    """The same pair, as runnable bash."""
    return _helper_block(step_id).replace("$$", "$")


def _extract(step_id: str, start_marker: str, end_marker: str) -> str:
    """A commented block of one step, as runnable bash.

    Extraction lifts a block OUT of its control flow, so on its own it proves the
    rules work rather than that the build runs them. Every block extracted here
    has a paired "this actually runs before the deploy" test.
    """
    body: str = _step(step_id)["args"][-1]
    start = body.index(start_marker)
    end = body.index(end_marker, start)
    end = body.index("\n", end) + 1
    block = body[start:end]
    assert block.strip(), f"{start_marker!r} extracted empty"
    return block.replace("$$", "$")


def _write_stamped(release: Path, name: str, build_id: str, *values: str) -> None:
    """A release-state file in the shape the build writes them."""
    body = "".join(f"{line}\n" for line in (build_id, *values))
    (release / name).write_text(body, encoding="utf-8")


def _read_stamped(release: Path, name: str, build_id: str) -> str:
    """The value half of a release-state file, asserting the stamp is the
    build's own — the assertion is the point, not the value."""
    lines = (release / name).read_text(encoding="utf-8").splitlines()
    assert lines, f"{name} was written empty"
    assert lines[0] == build_id, f"{name} is stamped {lines[0]!r}, not {build_id!r}"
    return "\n".join(lines[1:]).strip()


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


def _validate_url(value: str, environment: str = "") -> subprocess.CompletedProcess[str]:
    """The block, on one URL, in one declared environment.

    `environment` defaults to empty rather than to a real value on purpose: the
    block reads `$ENVIRONMENT` to choose between the enumerated origin rule and
    the suffix rule, and every case that predates that split is about the suffix
    rule. An empty value takes the same branch `development` does, so those cases
    keep asserting exactly what they always asserted.
    """
    return subprocess.run(  # noqa: S603 - a script this repo owns, run on fixed input
        ["bash", "-c", _validation_script()],
        env={
            "PATH": os.environ["PATH"],
            "DAINTREE_ENTITLEMENT_URL": value,
            "ENVIRONMENT": environment,
        },
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
    assert "stamped skip_smoke" in script, "the smoke does not honour the first-deploy hand-off"
    assert "/workspace/skip_smoke" not in script, (
        "the first-deploy hand-off is read out of the repository checkout, which "
        "means any tree carrying that file can skip the smoke suite"
    )
    assert "exit 0" in script

    # And the truer half: this bail-out is only reachable because deploy-rc wrote
    # the marker, and deploy-rc writes it only on the shape whose predicate
    # REFUSES an unreadable describe. A transient error used to land here — full
    # traffic, no readback, no smoke — and report green.
    deploy = "\n".join(_step("deploy-rc")["args"])
    assert "stamp skip_smoke" in deploy, "nothing hands the first-deploy case to the smoke"
    # CODE only. The step explains at length why it stopped asking `describe` a
    # second time, so a scan over raw text would be tripped by the comment that
    # documents the rule.
    deploy_code = "\n".join(
        line for line in deploy.splitlines() if not line.lstrip().startswith("#")
    )
    assert "if gcloud run services describe" not in deploy_code, (
        "the deploy shape is re-derived from a boolean describe, in which a denied "
        "permission and a missing service are the same answer"
    )


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
    carries nothing this test is about. The release-state helpers are prepended
    from the SAME step rather than reimplemented: the recording writes through
    `stamp` now, and a local imitation of it would be testing the imitation.
    """
    body: str = _step("deploy-rc")["args"][-1]
    start = body.index('RC_TAG="rc-')
    end = body.index('echo "Release candidate:', start)
    end = body.index("\n", end)
    block = body[start:end]
    assert block.strip(), "the recording block extracted empty"
    return (
        _release_state_helpers("deploy-rc")
        + "FLAGS=()\n"
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
    """Runs the recording and hands back the RELEASE directory.

    /workspace and /release are separate on purpose and the tests read the one
    the build actually writes its cross-step state to.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "gcloud"
    stub.write_text(_GCLOUD_STUB, encoding="utf-8")
    stub.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    release = tmp_path / "release"
    release.mkdir(parents=True)
    script = (
        _rc_recording_script()
        .replace("/release", str(release))
        .replace("/workspace", str(workspace))
    )
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
    return result, release


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
    build_id = "11111111-2222-3333-4444-555555555555"
    result, release = _run_rc_recording(
        build_id=build_id,
        interloper="rc-99999999aaaa",
        tmp_path=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    revision = _read_stamped(release, "rc_revision", build_id)
    url = _read_stamped(release, "rc_url", build_id)
    tag = _read_stamped(release, "rc_tag", build_id)
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
        result, release = _run_rc_recording(
            build_id=build_id, interloper="rc-none", tmp_path=tmp_path / f"run{i}"
        )
        assert result.returncode == 0, result.stderr
        tags.add(_read_stamped(release, "rc_tag", build_id))
    assert len(tags) == 2, f"two builds shared one tag: {tags}"


def test_the_derived_tag_is_a_legal_cloud_run_tag(tmp_path: Path) -> None:
    """Cloud Run rejects a tag that is not a DNS label, and it rejects one under
    three characters outright.

    The tag also becomes part of a hostname label beside the service name, so it
    is bounded rather than used whole. `BUILD_ID` is a UUID today; the derivation
    sanitises rather than trusts that, because a tag that fails validation fails
    the deploy AFTER the image has been built and pushed.
    """
    build_id = "AAAA-BBBB-cccc-dddd-eeee-ffff-0000-1111"
    result, release = _run_rc_recording(
        build_id=build_id,
        interloper="rc-none",
        tmp_path=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    tag = _read_stamped(release, "rc_tag", build_id)
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
    assert "stamped rc_revision" in body, (
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


# --- the entitlement origin is enumerated where it matters -------------------


@pytest.mark.parametrize(
    ("environment", "url"),
    [
        pytest.param(
            "staging",
            "https://staging.daintree.org/api/assistant/entitlement",
            id="staging_website",
        ),
        pytest.param(
            "production", "https://daintree.org/api/assistant/entitlement", id="production_apex"
        ),
        pytest.param(
            "production",
            "https://www.daintree.org/api/assistant/entitlement",
            id="production_www",
        ),
        pytest.param(
            "staging",
            "https://staging.daintree.org:443/api/assistant/entitlement",
            id="the_default_port_written_out",
        ),
        pytest.param(
            "development",
            "https://anything.daintree.org/api/assistant/entitlement",
            id="development_keeps_the_suffix_rule",
        ),
    ],
)
def test_the_build_accepts_an_approved_entitlement_origin(environment: str, url: str) -> None:
    """Each of these is a destination this environment is supposed to have.

    The default port written out is here because it is the spelling most likely
    to be rejected by an origin comparison done on strings: `https://x` and
    `https://x:443` are the same origin, and refusing one of them would be a
    check disagreeing with itself.

    The loopback carve-out is deliberately NOT in this list. Readiness allows it
    outside a hardened environment so a developer running the website locally can
    work; the build refuses `http://` in every environment, because a Cloud Run
    revision naming a loopback address is naming its own container. That
    asymmetry predates this rule and is the one place the build is stricter than
    the door it stands in for.
    """
    result = _validate_url(url, environment)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("environment", "url", "why"),
    [
        pytest.param(
            "staging",
            "https://preview-42.daintree.org/api/assistant/entitlement",
            "a preview deployment is a subdomain and is not the website",
            id="staging_preview_host",
        ),
        pytest.param(
            "staging",
            "https://daintree.org/api/assistant/entitlement",
            "the apex is not the staging website",
            id="staging_given_the_apex",
        ),
        pytest.param(
            "production",
            "https://staging.daintree.org/api/assistant/entitlement",
            "production must not ask staging about a subscription",
            id="production_given_staging",
        ),
        pytest.param(
            "staging",
            "https://staging.daintree.org:8443/api/assistant/entitlement",
            "an approved host on an unapproved port is a different service",
            id="an_unapproved_port",
        ),
    ],
)
def test_the_build_refuses_an_unapproved_entitlement_origin(
    environment: str, url: str, why: str
) -> None:
    """Every one of these passes the suffix rule, which is the point.

    Being a subdomain of daintree.org is the only thing that rule knows how to
    check, and the set of subdomains is not fixed — while this request carries
    the caller's Supabase access token to whatever it names.
    """
    result = _validate_url(url, environment)
    assert result.returncode != 0, why


@pytest.mark.parametrize("environment", ["staging", "production", "development", "test"])
def test_the_build_and_readiness_agree_about_the_origin_in_every_environment(
    environment: str,
) -> None:
    """Two implementations of one rule, in two languages, checked against each
    other in every environment rather than in the one somebody happened to try.

    The build check exists to name the mistake at the moment it is made rather
    than three steps later as a 503 on a candidate revision. It is only worth
    having while it agrees with the door it stands in for — and a shell `case`
    and a Python set are exactly the pair that drifts silently, because each one
    looks obviously right on its own.

    TWO ASYMMETRIES ARE DELIBERATE and are excluded from the sweep rather than
    quietly passing it, because an exclusion nobody wrote down is indistinguishable
    from a case nobody thought of:

    * `http://` on a loopback literal. Readiness allows it outside a hardened
      environment so a developer can run the website locally; the build refuses
      every `http://` URL, because a Cloud Run revision naming a loopback address
      is naming its own container.
    * `https://[::1]/...`, for the same underlying reason. The shell splits the
      authority on the first colon, which yields `[` for an IPv6 literal and
      refuses it — a limitation that predates this rule, since the suffix rule it
      replaces rejected the same value. Teaching the shell to parse IPv6 would buy
      nothing: the only URLs it can ever see are the ones a deploy sets, and a
      loopback destination is never one of them. The build being the stricter side
      fails closed, which is the right direction for the check to be wrong in.
    """
    from daintree_assistant_server.config import Settings

    candidates = [
        "https://staging.daintree.org/api/assistant/entitlement",
        "https://daintree.org/api/assistant/entitlement",
        "https://www.daintree.org/api/assistant/entitlement",
        "https://preview-42.daintree.org/api/assistant/entitlement",
        "https://staging.daintree.org:8443/api/assistant/entitlement",
        "https://staging.daintree.org:443/api/assistant/entitlement",
        # The two spellings a URL parser normalises and a byte comparison does
        # not. Both found a real divergence: uppercase was accepted by readiness
        # (urlsplit lowercases) and refused by the shell, and the trailing dot
        # was accepted by the suffix rule this replaces. A check that is stricter
        # than the door it stands in for rejects correct releases.
        "https://STAGING.DAINTREE.ORG/api/assistant/entitlement",
        "https://staging.daintree.org./api/assistant/entitlement",
        "https://EVIL.example/api/assistant/entitlement",
        # Two more the sweep did not originally carry, each of which WAS a real
        # divergence: an uppercase SCHEME (a URL scheme is case-insensitive and
        # `urlsplit` lowercases it) and a zero-padded port (`0443` is port 443 to
        # every parser that will handle this URL). The build refused both, which
        # is a release failing over a legal spelling rather than a bad value
        # getting through — the harmless direction, and still a broken release.
        "HTTPS://staging.daintree.org/api/assistant/entitlement",
        "HTTPS://daintree.org/api/assistant/entitlement",
        "https://staging.daintree.org:0443/api/assistant/entitlement",
        "https://daintree.org:0443/api/assistant/entitlement",
        "https://staging.daintree.org.:443/api/assistant/entitlement",
        "https://staging.daintree.org:80/api/assistant/entitlement",
        "https://daintree.org:notaport/api/assistant/entitlement",
        "https://user@staging.daintree.org/api/assistant/entitlement",
        "https://staging.daintree.org../api/assistant/entitlement",
        "https://staging.daintree.org/api/assistant/entitlements",
    ]
    for url in candidates:
        build_ok = _validate_url(url, environment).returncode == 0
        settings = Settings(  # type: ignore[call-arg]
            STATE_SIGNING_KEY_CURRENT="sk",
            TOGETHER_API_KEY="sk-test",
            CEREBRAS_API_KEY="csk-test",
            ENVIRONMENT=environment,
            DAINTREE_ENTITLEMENT_URL=url,
        )
        readiness_ok = not [
            p for p in settings.entitlement_problems() if "DAINTREE_ENTITLEMENT_URL" in p
        ]
        assert build_ok == readiness_ok, (
            f"{environment} / {url}: build says {build_ok}, readiness says {readiness_ok}"
        )


# --- the public-posture gate, proved by RUNNING it ---------------------------
#
# The rule this block enforces has a SCOPE, and the scope is the part that had to
# be decided rather than written: it binds on `staging` and deliberately not on
# every public deployment. The two rollout axes are staged independently on
# purpose, and the public production service runs `open`/`off` today as a product
# posture readiness accepts — so a blanket "no public service may fund anonymous
# work" would refuse the release that is currently correct, which is a gate
# nobody can ship through and therefore a gate somebody deletes.
#
# What makes staging different is not that it matters less. `staging` is the
# PRIVATE BETA — the only deployment the subject gate exists for, and `config.py`
# refuses to let production hold a membership list at all — so a publicly
# invokable staging service that does not enforce has no legitimate reading. The
# cases below assert both halves: the mistake is refused, and the two correct
# releases are not.


def _posture_gate_script(service: str = "daintree-assistant") -> str:
    """The public-posture gate and the subject-gate mount, as runnable bash.

    Extracted and EXECUTED for the reason every other block here is: this one is
    a `case`-free chain of string comparisons over five variables and one IAM
    lookup, and shell is exactly where a rule can be present and wrong. A grep
    for `allUsers` would pass against a block that reads the policy and then
    compares the wrong variable.

    The SERVICE is a parameter because the rule's exemption is keyed on it. It is
    substituted into the gcloud calls the same way Cloud Build substitutes it, so
    the name the block compares and the name it queries agree — a harness that
    let those two disagree would be testing a build nobody can run.
    """
    body: str = _step("deploy-rc")["args"][-1]
    start = body.index("# Is this service reachable by a stranger?")
    end = body.index("# The secret REFERENCES this build is asking for", start)
    block = body[start:end]
    assert block.strip(), "the posture gate block extracted empty"
    return (
        block.replace("$$", "$")
        .replace("${_SERVICE}", service)
        .replace("${_REGION}", "us-central1")
    )


def test_the_posture_gate_actually_runs_before_the_deploy() -> None:
    """The half `_posture_gate_script` cannot prove by executing anything.

    A refusal that fires after `gcloud run deploy` is not a refusal — the tagged
    candidate already exists and, on a public service, already has a URL. This is
    the same paired proof `test_the_validation_actually_runs_before_the_deploy`
    carries for the entitlement URL.
    """
    body: str = _step("deploy-rc")["args"][-1]
    start = body.index("# Is this service reachable by a stranger?")
    assert "gcloud run deploy" in body[start:], "the posture gate runs after the deploy"
    preamble = body[:start]
    assert "exit 0" not in preamble, "an early successful exit would skip the posture gate"
    for guard in ("if false", "if [ 1 -eq 0 ]", "if /bin/false"):
        assert guard not in body, f"the posture gate is wrapped in a guard that never runs: {guard}"


#: A `gcloud` that answers the three questions the posture gate asks, and lies
#: about them on demand. Everything the gate decides comes from these answers, so
#: the stub is the whole environment the rule is judged in.
_POSTURE_STUB = """#!/bin/bash
set -uo pipefail
case "$*" in
  *"run services get-iam-policy"*)
    [ "${IAM_READABLE:-yes}" = "yes" ] || { echo "PERMISSION_DENIED" >&2; exit 1; }
    if [ "${IAM_PUBLIC:-no}" = "yes" ]; then
      printf '{"bindings":[{"role":"roles/run.invoker","members":["allUsers"]}]}'
    else
      printf '{"bindings":[{"role":"roles/run.invoker","members":["user:someone@example.com"]}]}'
    fi
    ;;
  *"run services describe"*)
    if [ "${SERVICE_EXISTS:-yes}" != "yes" ]; then
      echo "ERROR: (gcloud.run.services.describe) Cannot find service: NOT_FOUND" >&2
      exit 1
    fi
    if [ "${DESCRIBE_READABLE:-yes}" != "yes" ]; then
      echo "ERROR: PERMISSION_DENIED: caller lacks run.services.get" >&2
      exit 1
    fi
    printf '{"metadata":{"annotations":{"run.googleapis.com/invoker-iam-disabled":"%s"}}}' \
      "${IAM_DISABLED:-false}"
    ;;
  *"secrets versions describe"*)
    [ -n "${GATE_STATE:-}" ] || { echo "NOT_FOUND" >&2; exit 1; }
    printf '%s\n' "$GATE_STATE"
    ;;
esac
exit 0
"""


#: The one service this file exempts, and the one it does not. The gate keys on
#: the NAME rather than on the declared environment, so the harness has to name
#: one — and defaulting it from the environment keeps every existing case saying
#: exactly what it always said.
_PRODUCTION_SERVICE = "daintree-assistant"
_STAGING_SERVICE = "daintree-assistant-staging"


def _run_posture_gate(
    tmp_path: Path,
    *,
    environment: str,
    service: str | None = None,
    auth_mode: str = "open",
    entitlement_mode: str = "off",
    entitlement_url: str = "",
    gate_revision: str = "",
    public: str = "no",
    service_exists: str = "yes",
    iam_readable: str = "yes",
    describe_readable: str = "yes",
    iam_disabled: str = "false",
    gate_state: str = "ENABLED",
) -> subprocess.CompletedProcess[str]:
    if service is None:
        service = _PRODUCTION_SERVICE if environment == "production" else _STAGING_SERVICE
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "gcloud"
    stub.write_text(_POSTURE_STUB, encoding="utf-8")
    stub.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    script = _posture_gate_script(service).replace("/workspace", str(workspace))
    # The refusals must fire under the step's real shell options, or a block that
    # only refuses because an unset variable aborted it would read as correct.
    script = "set -euo pipefail\n" + script
    return subprocess.run(  # noqa: S603 - a script this repo owns, on fixed input
        ["bash", "-c", script],
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "PROJECT_ID": "daintree-research",
            "SERVICE": service,
            "ENVIRONMENT": environment,
            "AUTH_MODE": auth_mode,
            "ENTITLEMENT_MODE": entitlement_mode,
            "DAINTREE_ENTITLEMENT_URL": entitlement_url,
            "STAGING_SUBJECT_GATE_REVISION": gate_revision,
            "SECRET_FLAGS": "TOGETHER_API_KEY=together-api-key:latest",
            "IAM_PUBLIC": public,
            "SERVICE_EXISTS": service_exists,
            "IAM_READABLE": iam_readable,
            "DESCRIBE_READABLE": describe_readable,
            "IAM_DISABLED": iam_disabled,
            "GATE_STATE": gate_state,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )


_STAGING_URL = "https://staging.daintree.org/api/assistant/entitlement"


def test_a_public_staging_service_is_refused_an_unenforced_release(tmp_path: Path) -> None:
    """The mistake with no correct version of itself.

    A staging service anybody can invoke, deployed with the door open, is a
    private beta that is not private — and because the server holds the upstream
    credential, every turn it serves is funded by Daintree for a stranger. There
    is no rollout step this is, which is what makes it a build refusal rather
    than an operator's judgement.
    """
    result = _run_posture_gate(tmp_path, environment="staging", auth_mode="open", public="yes")
    assert result.returncode != 0
    assert "publicly invokable" in result.stderr


def test_a_public_staging_service_is_refused_an_unenforced_entitlement(
    tmp_path: Path,
) -> None:
    """The second axis, refused on its own.

    Identity says the token is genuine; entitlement says the account behind it has
    paid. A public staging service that checks the first and not the second funds
    every verified account's turns, which is a smaller hole than the anonymous one
    and the same shape.
    """
    result = _run_posture_gate(
        tmp_path,
        environment="staging",
        auth_mode="enforce",
        entitlement_mode="observe",
        entitlement_url=_STAGING_URL,
        gate_revision="beta-1",
        public="yes",
    )
    assert result.returncode != 0
    assert "_ENTITLEMENT_MODE" in result.stderr


def test_a_public_staging_service_is_refused_without_an_entitlement_url(
    tmp_path: Path,
) -> None:
    """Enforcing a lookup with nowhere to send it.

    Repeated inside this block rather than left to the cross-field rule two
    hundred lines above, because the block is extracted and executed alone: a rule
    that is only correct in the presence of a distant one survives a refactor as a
    hole.
    """
    result = _run_posture_gate(
        tmp_path,
        environment="staging",
        auth_mode="enforce",
        entitlement_mode="enforce",
        entitlement_url="",
        gate_revision="beta-1",
        public="yes",
    )
    assert result.returncode != 0
    assert "_DAINTREE_ENTITLEMENT_URL" in result.stderr


def test_a_public_staging_service_is_refused_without_a_published_membership(
    tmp_path: Path,
) -> None:
    """The gate secret, checked by METADATA before the deploy mounts it.

    A secret that exists with no enabled version deploys cleanly and then fails
    the revision at startup, which reads as a broken image rather than as a list
    nobody published.
    """
    result = _run_posture_gate(
        tmp_path,
        environment="staging",
        auth_mode="enforce",
        entitlement_mode="enforce",
        entitlement_url=_STAGING_URL,
        gate_revision="beta-1",
        public="yes",
        gate_state="DISABLED",
    )
    assert result.returncode != 0
    assert "staging-allowed-subject-digests" in result.stderr


def test_an_unreadable_invoker_policy_stops_a_staging_release(tmp_path: Path) -> None:
    """Fail CLOSED, and only where the rule binds.

    Not knowing whether a staging service is public costs a re-run. Guessing
    "private" costs an open funded endpoint nobody decided to publish.
    """
    result = _run_posture_gate(
        tmp_path, environment="staging", auth_mode="open", iam_readable="no"
    )
    assert result.returncode != 0
    assert "invoker policy" in result.stderr


def test_a_describe_that_fails_for_any_other_reason_is_not_read_as_absent(
    tmp_path: Path,
) -> None:
    """ABSENT and FORBIDDEN are different answers, and only one of them is safe.

    A describe that fails on a denied permission or a transient API error is not
    evidence that the service does not exist — and reading it as such would
    report an EXISTING PUBLIC service as private, which is the one wrong answer
    this check is able to give. The previous-signing-key probe above draws the
    same distinction for the same reason.
    """
    result = _run_posture_gate(
        tmp_path, environment="staging", auth_mode="open", describe_readable="no"
    )
    assert result.returncode != 0
    assert "invoker policy" in result.stderr


def test_a_service_with_the_iam_check_switched_off_counts_as_public(
    tmp_path: Path,
) -> None:
    """The other way a Cloud Run service becomes reachable by anybody.

    It is not a binding at all: the invoker IAM check can be turned OFF on the
    service, after which the policy says nothing about who may call it. A gate
    that read only the bindings would call this service private and be
    confidently wrong — the worst shape a safety check can take.
    """
    result = _run_posture_gate(
        tmp_path,
        environment="staging",
        auth_mode="open",
        public="no",
        iam_disabled="true",
    )
    assert result.returncode != 0
    assert "publicly invokable" in result.stderr


def test_an_unreadable_gate_secret_is_not_read_as_enabled(tmp_path: Path) -> None:
    """The status is kept as well as the output.

    Discarding it would judge a command that FAILED on whatever it managed to
    print, and the whole point of this check is that exactly one answer proceeds.
    """
    result = _run_posture_gate(
        tmp_path,
        environment="staging",
        auth_mode="enforce",
        entitlement_mode="enforce",
        entitlement_url=_STAGING_URL,
        gate_revision="beta-1",
        gate_state="",
    )
    assert result.returncode != 0
    assert "staging-allowed-subject-digests" in result.stderr


def test_a_private_staging_rollout_step_is_not_refused(tmp_path: Path) -> None:
    """The rollout the document actually prescribes.

    Identity and gate behaviour are proven on an IAM-PRIVATE service at `open`,
    then `observe`, then `enforce`, and public invocation is attached at the end.
    A gate that refused the unenforced steps of that sequence would make the
    sequence unrunnable and force the whole cutover into one release.
    """
    result = _run_posture_gate(
        tmp_path, environment="staging", auth_mode="observe", public="no"
    )
    assert result.returncode == 0, result.stderr


def test_the_currently_correct_production_release_is_not_refused(tmp_path: Path) -> None:
    """THE case that decides the scope of the whole rule.

    Production is public, runs `AUTH_MODE=open` and `ENTITLEMENT_MODE=off`, and is
    the deployment this file ships on every push to `main`. Refusing it would be
    refusing the release that is currently correct — and the axes are staged
    independently on purpose, so "public implies enforce" is not a rule this repo
    holds. Moving production is a product decision that will arrive as its own
    rule, not as a side effect of a staging safety check.
    """
    result = _run_posture_gate(
        tmp_path, environment="production", auth_mode="open", public="yes"
    )
    assert result.returncode == 0, result.stderr


def test_a_fully_enforced_public_staging_release_is_accepted(tmp_path: Path) -> None:
    """The destination. If this failed, the cutover could never be released."""
    result = _run_posture_gate(
        tmp_path,
        environment="staging",
        auth_mode="enforce",
        entitlement_mode="enforce",
        entitlement_url=_STAGING_URL,
        gate_revision="beta-1",
        public="yes",
    )
    assert result.returncode == 0, result.stderr


def test_the_gate_secret_is_mounted_on_exactly_the_condition_it_decides_on(
    tmp_path: Path,
) -> None:
    """staging AND enforce — the same two conditions as
    `Settings.staging_subject_gate_enabled`.

    Mounting it more widely is refused by readiness (production may not hold a
    membership list at all); mounting it less widely fails readiness on the one
    deployment that needs it. There is no third option, which is why the condition
    is copied from the property rather than approximated.
    """
    mounted = _run_posture_gate(
        tmp_path / "on",
        environment="staging",
        auth_mode="enforce",
        entitlement_mode="enforce",
        entitlement_url=_STAGING_URL,
        gate_revision="beta-1",
    )
    assert mounted.returncode == 0, mounted.stderr
    assert "staging-allowed-subject-digests:latest" in mounted.stdout

    not_mounted = _run_posture_gate(
        tmp_path / "off", environment="production", auth_mode="open"
    )
    assert not_mounted.returncode == 0, not_mounted.stderr
    assert "staging-allowed-subject-digests" not in not_mounted.stdout


def test_a_private_staging_enforce_release_needs_a_membership_label(
    tmp_path: Path,
) -> None:
    """Readiness refuses the revision without it, so the build says so first."""
    result = _run_posture_gate(
        tmp_path,
        environment="staging",
        auth_mode="enforce",
        entitlement_mode="enforce",
        entitlement_url=_STAGING_URL,
        gate_revision="",
    )
    assert result.returncode != 0
    assert "_STAGING_SUBJECT_GATE_REVISION" in result.stderr


def test_a_label_naming_no_membership_is_refused(tmp_path: Path) -> None:
    """It is published verbatim on /version.

    So an operator reading the deployment they just raised would be told it holds
    a membership called `beta-3` while every verified account walks through.
    """
    result = _run_posture_gate(
        tmp_path, environment="production", auth_mode="open", gate_revision="beta-3"
    )
    assert result.returncode != 0
    assert "_STAGING_SUBJECT_GATE_REVISION" in result.stderr


def test_the_build_never_reads_a_secret_value() -> None:
    """`versions describe` returns metadata; `versions access` returns the payload.

    The membership list is a set of subject digests and a build log outlives the
    build, so the second command may not appear in the deployment surfaces at all.
    The deploy SA holds `secretmanager.viewer`, which cannot read a value — the
    least-privilege grant and this rule are two statements of one decision, and a
    gate on the text is what keeps them agreeing.
    """
    for path in (_CLOUDBUILD, _ROOT / "infra" / "gcp" / "bootstrap.sh", _SMOKE):
        # Comments excluded, because all three files EXPLAIN the rule by naming
        # the command it forbids — and a gate that cannot tell an explanation
        # from an instruction forces the explanation out, which is how the next
        # author learns the rule by breaking it.
        offending = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if "versions access" in line and not line.lstrip().startswith("#")
        ]
        assert not offending, (
            f"{path.name} reads a secret's payload; only metadata may be read "
            f"here: {offending}"
        )


# --- candidate isolation: the tag is retired on EVERY path -------------------
#
# A release candidate is deployed `--no-traffic --tag=rc-<build>`, which gives it
# a URL of its own — and a Cloud Run invoker binding is per-SERVICE, so on a
# public service that URL is public. The tag used to be removed on exactly one
# path: the promotion succeeding. A failed deploy, a failed environment readback,
# a failed smoke, or a promotion that fell over between the traffic move and the
# tag removal each left `rc-<build>` resolving indefinitely — on a revision that
# had just been judged unfit to take traffic.
#
# Cloud Build has no `always()` and a failed step aborts the build, so there was
# nowhere to put a cleanup that would run. `allowFailure` is what creates that
# place, and these tests hold the whole mechanism: the three fallible steps
# declare it, the final step does not, each step declines without its
# predecessor's marker, and the cleanup can only ever name this build's own tag.


def test_the_three_fallible_steps_record_rather_than_abort(
    cloudbuild: dict[str, Any],
) -> None:
    """`allowFailure` here is not a relaxation; it is what makes cleanup reachable.

    Without it the build stops at the failing step with the tag still attached.
    The build's red/green is unchanged, because `release-gate` fails on a missing
    marker — what changed is that the tag is gone before it goes red.
    """
    steps = {s["id"]: s for s in cloudbuild["steps"]}
    for name in ("deploy-rc", "smoke", "promote"):
        assert steps[name].get("allowFailure") is True, (
            f"{name} aborts the build on failure, so the cleanup below it never runs"
        )
    assert steps["release-gate"].get("allowFailure") is not True, (
        "the step that renders the verdict must be able to fail the build"
    )
    assert cloudbuild["steps"][-1]["id"] == "release-gate", (
        "the cleanup must be last, or a step after it would run with the "
        "candidate already retired"
    )


@pytest.mark.parametrize(
    ("step_id", "marker"),
    [("smoke", "stage_deploy_ok"), ("promote", "stage_smoke_ok")],
)
def test_a_step_declines_without_its_predecessors_marker(
    cloudbuild: dict[str, Any], step_id: str, marker: str
) -> None:
    """`allowFailure` means a step now runs after its predecessor failed.

    So each one has to decline for itself. Without this, a failed deploy would be
    followed by a smoke against a stale URL and then by a promotion of a revision
    no smoke ever saw — which is worse than the leak this mechanism was added to
    close.

    And it has to decline on the RIGHT question. `[ -f /workspace/<marker> ]` was
    the old test, and /workspace is the repository checkout — so the marker it
    asked about was a file the repo could carry. The predecessor is asked through
    `stamped`, which answers "did THIS build write it".
    """
    step = next(s for s in cloudbuild["steps"] if s.get("id") == step_id)
    script = "\n".join(step["args"])
    assert f"stamped {marker}" in script, f"{step_id} runs whatever happened before it"
    assert f"/workspace/{marker}" not in script, (
        f"{step_id} reads its predecessor's marker out of the repository checkout"
    )


#: A `gcloud` for the cleanup step: it reports whichever traffic rows the test
#: sets up, and records every tag anybody asks it to remove.
_CLEANUP_STUB = """#!/bin/bash
set -uo pipefail
case "$*" in
  *"run services describe"*"--format=json"*)
    [ "${DESCRIBE_OK:-yes}" = "yes" ] || exit 1
    cat "$TRAFFIC_JSON"
    ;;
  *"run services update-traffic"*)
    for arg in "$@"; do
      case "$arg" in
        --remove-tags=*) echo "${arg#--remove-tags=}" >> "$REMOVED_LOG" ;;
        --to-revisions=*|--to-latest) echo "TRAFFIC-MOVED:$arg" >> "$REMOVED_LOG" ;;
      esac
    done
    ;;
esac
exit 0
"""


_CLEANUP_BUILD_ID = "build-under-test"


def _run_cleanup(
    tmp_path: Path,
    *,
    rc_tag: str | None,
    attached_tags: list[str],
    markers: tuple[str, ...] = ("stage_deploy_ok", "stage_smoke_ok", "stage_promote_ok"),
    describe_ok: str = "yes",
    state_build_id: str = _CLEANUP_BUILD_ID,
    seed_checkout: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """The verdict step, run against a release directory this harness writes.

    `state_build_id` is what the state is STAMPED with, which is a different
    thing from what the step is told its own id is — that difference is the
    forgery the stamps exist to refuse. `seed_checkout` puts files in the fake
    /workspace, which is what a repository carrying release-state filenames would
    look like.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "gcloud"
    stub.write_text(_CLEANUP_STUB, encoding="utf-8")
    stub.chmod(0o755)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    release = tmp_path / "release"
    release.mkdir(parents=True)
    if rc_tag is not None:
        _write_stamped(release, "rc_tag", state_build_id, rc_tag)
    for marker in markers:
        _write_stamped(release, marker, state_build_id)
    for name in seed_checkout:
        (workspace / name).write_text("", encoding="utf-8")

    traffic = tmp_path / "traffic.json"
    traffic.write_text(
        json.dumps(
            {
                "status": {
                    "traffic": [
                        {"tag": tag, "revisionName": f"svc-{tag}", "url": f"https://{tag}"}
                        for tag in attached_tags
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    removed_log = tmp_path / "removed"
    removed_log.write_text("", encoding="utf-8")

    step = next(
        s
        for s in yaml.safe_load(_CLOUDBUILD.read_text(encoding="utf-8"))["steps"]
        if s.get("id") == "release-gate"
    )
    script = (
        step["args"][-1]
        .replace("$$", "$")
        .replace("${_SERVICE}", "daintree-assistant")
        .replace("${_REGION}", "us-central1")
        .replace("$COMMIT_SHA", "abc123")
        .replace("/release", str(release))
        .replace("/workspace", str(workspace))
    )
    result = subprocess.run(  # noqa: S603 - a script this repo owns, on fixed input
        ["bash", "-c", script],
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "BUILD_ID": _CLEANUP_BUILD_ID,
            "TRAFFIC_JSON": str(traffic),
            "REMOVED_LOG": str(removed_log),
            "DESCRIBE_OK": describe_ok,
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    removed = [
        line for line in removed_log.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return result, removed


def test_a_failed_release_retires_its_own_candidate_tag(tmp_path: Path) -> None:
    """The leak, closed. The build still goes red — but not with a live URL on a
    revision that failed its own smoke."""
    result, removed = _run_cleanup(
        tmp_path,
        rc_tag="rc-111111112222",
        attached_tags=["rc-111111112222"],
        markers=("stage_deploy_ok",),
    )
    assert result.returncode != 0, "a failed release must still fail the build"
    assert removed == ["rc-111111112222"]
    assert "smoke" in result.stderr and "promote" in result.stderr


def test_the_cleanup_cannot_touch_a_concurrent_builds_tag(tmp_path: Path) -> None:
    """The invariant the per-build tag exists for, asserted on the cleanup too.

    Promotion once removed a shared `candidate` tag and took a sibling build's
    candidate down with it. A cleanup that guessed at a name, or removed every
    `rc-` tag it found, would put that back — and would do it on the failure path,
    where a second build is most likely to be running.
    """
    result, removed = _run_cleanup(
        tmp_path,
        rc_tag="rc-111111112222",
        attached_tags=["rc-99999999aaaa"],
        markers=("stage_deploy_ok",),
    )
    assert result.returncode != 0
    assert removed == [], f"the cleanup removed a tag that is not ours: {removed}"


def test_the_cleanup_never_moves_traffic(tmp_path: Path) -> None:
    """`--remove-tags` alone says nothing about the split.

    A cleanup that reached for `--to-revisions` or `--to-latest` would be
    promoting or rolling back on the failure path, which is a decision this step
    is emphatically not making.
    """
    _, removed = _run_cleanup(
        tmp_path, rc_tag="rc-111111112222", attached_tags=["rc-111111112222"]
    )
    assert not any(entry.startswith("TRAFFIC-MOVED") for entry in removed), removed


def test_a_successful_release_leaves_the_promotions_own_cleanup_alone(
    tmp_path: Path,
) -> None:
    """The promotion removes the tag inside its traffic call, so on the happy path
    there is nothing attached — and a second removal would be an error on the one
    run that went perfectly."""
    result, removed = _run_cleanup(
        tmp_path, rc_tag="rc-111111112222", attached_tags=[]
    )
    assert result.returncode == 0, result.stderr
    assert removed == []


def test_a_build_that_never_tagged_anything_guesses_at_no_name(tmp_path: Path) -> None:
    """A deploy that failed before tagging wrote no file.

    Reaching for a default here is how the shared `candidate` name came back last
    time: there is nothing to clean up, and inventing something to clean up is a
    way to delete somebody else's.
    """
    result, removed = _run_cleanup(
        tmp_path, rc_tag=None, attached_tags=["rc-99999999aaaa"], markers=()
    )
    assert result.returncode != 0
    assert removed == []


def test_a_cleanup_that_cannot_confirm_the_tag_fails_the_build(tmp_path: Path) -> None:
    """A successful release whose tag might still be attached is not a success.

    The candidate URL is the thing being retired; "we could not tell" leaves it
    resolving, and a green build is exactly the report that stops anybody looking.
    """
    result, _ = _run_cleanup(
        tmp_path,
        rc_tag="rc-111111112222",
        attached_tags=["rc-111111112222"],
        describe_ok="no",
    )
    assert result.returncode != 0
    assert "could not determine" in result.stderr


# --- the environment readback, run rather than read --------------------------


def _readback_program() -> str:
    """The revision readback's python, as a runnable program.

    Executed rather than grepped for the same reason the shell blocks are: this
    one now decides two things that used to be reported and ignored — an
    unexpected application variable, and a secret reference that is not the one
    the deploy asked for — and "reports it" and "fails on it" differ by one line
    that a substring assertion cannot see.
    """
    step = next(
        s
        for s in yaml.safe_load(_CLOUDBUILD.read_text(encoding="utf-8"))["steps"]
        if s.get("id") == "deploy-rc"
    )
    body: str = step["args"][-1]
    segment = body[body.index("gcloud run revisions describe") :]
    opener = segment.index("<<'PYEOF'")
    start = segment.index("\n", opener) + 1
    end = segment.index("\nPYEOF", start)
    program = segment[start:end]
    assert program.strip(), "the readback program extracted empty"
    return program.replace("$$", "$")


def _run_readback(
    tmp_path: Path,
    *,
    wanted: dict[str, str],
    refs: dict[str, dict[str, str]],
    revision_env: list[dict[str, Any]],
) -> subprocess.CompletedProcess[str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "env_vars.json").write_text(json.dumps(wanted), encoding="utf-8")
    (workspace / "secret_refs.json").write_text(json.dumps(refs), encoding="utf-8")
    (workspace / "revision_readback_json").write_text(
        json.dumps({"spec": {"containers": [{"env": revision_env}]}}), encoding="utf-8"
    )
    program = _readback_program().replace("/workspace", str(workspace))
    script = tmp_path / "readback.py"
    script.write_text(program, encoding="utf-8")
    return subprocess.run(  # noqa: S603 - a program this repo owns, on fixed input
        ["python3", str(script)],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=60,
    )


_GATE_REF = {"secret": "staging-allowed-subject-digests", "version": "latest"}


def _coherent_revision() -> tuple[dict[str, str], dict[str, dict[str, str]], list[dict[str, Any]]]:
    wanted = {"AUTH_MODE": "enforce", "ENVIRONMENT": "staging"}
    refs = {"STAGING_ALLOWED_SUBJECT_DIGESTS": dict(_GATE_REF)}
    env: list[dict[str, Any]] = [
        {"name": "AUTH_MODE", "value": "enforce"},
        {"name": "ENVIRONMENT", "value": "staging"},
        {"name": "PORT", "value": "8080"},
        {"name": "K_SERVICE", "value": "daintree-assistant"},
        {
            "name": "STAGING_ALLOWED_SUBJECT_DIGESTS",
            "valueFrom": {
                "secretKeyRef": {"name": "staging-allowed-subject-digests", "key": "latest"}
            },
        },
    ]
    return wanted, refs, env


def test_the_readback_accepts_the_revision_the_build_generated(tmp_path: Path) -> None:
    """Platform names ride along and must not fail a release."""
    wanted, refs, env = _coherent_revision()
    result = _run_readback(tmp_path, wanted=wanted, refs=refs, revision_env=env)
    assert result.returncode == 0, result.stderr


def test_the_readback_refuses_an_application_variable_nobody_deployed(
    tmp_path: Path,
) -> None:
    """The allowlist, closed in the direction that was open.

    `--env-vars-file` replaces the whole environment, so an application control
    variable this build did not generate reached the revision some other way — a
    manual `--update-env-vars` a release was meant to wipe, or a deploy that did
    not take. Checking only that the requested names are PRESENT cannot see
    either, and this one is `AUTH_MODE`'s neighbourhood: the setting most worth
    smuggling in is the one that decides whether there is a door.
    """
    wanted, refs, env = _coherent_revision()
    env.append({"name": "ENTITLEMENT_MODE", "value": "off"})
    result = _run_readback(tmp_path, wanted=wanted, refs=refs, revision_env=env)
    assert result.returncode != 0
    assert "ENTITLEMENT_MODE" in result.stderr


def test_the_readback_refuses_a_secret_reference_nobody_asked_for(
    tmp_path: Path,
) -> None:
    """A membership list mounted on a revision this release did not mount it on."""
    wanted, refs, env = _coherent_revision()
    refs.pop("STAGING_ALLOWED_SUBJECT_DIGESTS")
    result = _run_readback(tmp_path, wanted=wanted, refs=refs, revision_env=env)
    assert result.returncode != 0
    assert "never requested" in result.stderr


def test_the_readback_refuses_a_missing_gate_reference(tmp_path: Path) -> None:
    """The inverse, and the one a private staging release turns on: the deploy
    asked for the membership list and the revision came up without it."""
    wanted, refs, env = _coherent_revision()
    env = [entry for entry in env if entry["name"] != "STAGING_ALLOWED_SUBJECT_DIGESTS"]
    result = _run_readback(tmp_path, wanted=wanted, refs=refs, revision_env=env)
    assert result.returncode != 0
    assert "not mounted" in result.stderr


def test_the_readback_refuses_the_right_secret_at_the_wrong_version(
    tmp_path: Path,
) -> None:
    """`latest` and a pinned number are different mounts.

    A reference to the right secret at the wrong version is a revision holding a
    membership nobody published — and it is the failure that looks most like
    success, because the name in the console is correct.
    """
    wanted, refs, env = _coherent_revision()
    env[-1]["valueFrom"]["secretKeyRef"]["key"] = "3"
    result = _run_readback(tmp_path, wanted=wanted, refs=refs, revision_env=env)
    assert result.returncode != 0
    assert "staging-allowed-subject-digests:3" in result.stderr


def test_the_readback_still_never_reads_a_secret_value(tmp_path: Path) -> None:
    """A `value` beside a `valueFrom` would be a payload, and nothing may print it.

    Cloud Run does not produce that shape, which is exactly why it is worth
    asserting: the comparison's safety rests on `value` and `valueFrom` being
    disjoint, and this is the case that would break it silently.
    """
    wanted, refs, env = _coherent_revision()
    env[-1]["value"] = "a-real-membership-list"
    result = _run_readback(tmp_path, wanted=wanted, refs=refs, revision_env=env)
    assert "a-real-membership-list" not in (result.stdout + result.stderr)


# --- ingress and the gate label are declared, mapped and passed --------------


def _shape_validation_script() -> str:
    """The ingress enum and the membership-label pattern, extracted the same way.

    They sit with the other enums rather than beside the gate itself: these are
    SHAPE questions that cost nothing, and the gate block spends an API call.
    Cheap refusals belong before expensive ones — and, like every other block
    here, a `case` pattern and a `grep -E` are present-and-wrong in ways only
    running them can settle.
    """
    step = next(
        s
        for s in yaml.safe_load(_CLOUDBUILD.read_text(encoding="utf-8"))["steps"]
        if s.get("id") == "deploy-rc"
    )
    body: str = step["args"][-1]
    start = body.index("# The two values the subject gate added")
    end = body.index("# The operational dials, checked for the same reason", start)
    block = body[start:end]
    assert block.strip(), "the shape validation block extracted empty"
    return block.replace("$$", "$")


def _validate_shapes(**values: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "INGRESS": "all",
        "STAGING_SUBJECT_GATE_REVISION": "",
    }
    env.update(values)
    return subprocess.run(  # noqa: S603 - a script this repo owns, run on fixed input
        ["bash", "-c", _shape_validation_script()],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_ingress_is_no_longer_a_literal_in_the_flag_array(
    cloudbuild: dict[str, Any], deploy_step: dict[str, Any]
) -> None:
    """`--ingress=all` sat unconditional inside the flag array used by BOTH the
    no-traffic candidate deploy and the first-ever full-traffic one.

    It is the network layer rather than the layer that guards this service — the
    invoker binding is that, and the posture gate reads it — but a hardcoded
    value cannot be moved for a deployment that should sit behind a load balancer
    or inside a VPC without forking this file.
    """
    script = "\n".join(deploy_step["args"])
    assert "--ingress=all" not in script, "ingress is still hardcoded"
    assert '--ingress="$$INGRESS"' in script, "ingress does not come from the substitution"
    assert "INGRESS=${_INGRESS}" in deploy_step["env"], "not mapped to env"
    assert cloudbuild["substitutions"]["_INGRESS"] == "all", (
        "the default must be what a domain-mapped public product needs, or this "
        "change moves a deployment"
    )


def test_ingress_is_not_sent_to_the_application(deploy_step: dict[str, Any]) -> None:
    """It is a Cloud Run deploy flag, not a setting `config.py` has ever heard of.

    Sending it would put an unknown name in the revision's environment, which the
    closed allowlist in the readback now refuses — so this would present as every
    release failing its own readback.
    """
    script = "\n".join(deploy_step["args"])
    names = re.search(r"NAMES = \((.*?)\)", script, re.S)
    assert names is not None
    assert '"INGRESS"' not in names.group(1)


@pytest.mark.parametrize(
    "value", ["all", "internal", "internal-and-cloud-load-balancing"]
)
def test_the_build_accepts_every_ingress_cloud_run_accepts(value: str) -> None:
    result = _validate_shapes(INGRESS=value)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("value", ["", "public", "ALL", "internal-and-load-balancing"])
def test_the_build_refuses_an_ingress_cloud_run_would_reject(value: str) -> None:
    """A typo here fails AFTER the image is built and pushed, which is the most
    expensive place in this file to learn a word is misspelled. The empty string
    is in the list on purpose: every other dial reads blank as "take the default",
    and this one has no default to take — `gcloud run deploy --ingress=` is an
    error, not an omission."""
    result = _validate_shapes(INGRESS=value)
    assert result.returncode != 0


@pytest.mark.parametrize("value", ["beta-1", "b", "beta.3_rc", "0"])
def test_the_build_accepts_a_well_formed_membership_label(value: str) -> None:
    result = _validate_shapes(STAGING_SUBJECT_GATE_REVISION=value)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("value", "why"),
    [
        pytest.param("Beta-1", "uppercase", id="uppercase"),
        pytest.param("-beta", "leading hyphen", id="leading_punctuation"),
        pytest.param("beta 1", "whitespace", id="whitespace"),
        pytest.param("a" * 33, "past the length bound", id="too_long"),
        pytest.param("0" * 64, "a subject digest in the label variable", id="a_digest"),
    ],
)
def test_the_build_refuses_a_membership_label_that_is_not_one(value: str, why: str) -> None:
    """The last case is the one the rule exists for.

    The label is published verbatim on a public unauthenticated /version — which
    answers while the service is UNREADY, so "readiness would refuse it" is not a
    reason a bad value is safe to echo. A digest typed into the variable next to
    the one that holds them would otherwise be published by the mistake that made
    it unsafe. It fails the length bound rather than being named as a digest,
    which is the same refusal for a smaller reason and the honest one to make.
    """
    result = _validate_shapes(STAGING_SUBJECT_GATE_REVISION=value)
    assert result.returncode != 0, why
    assert value not in result.stderr, "the refusal echoes the value it refused"


def test_the_membership_label_is_declared_mapped_and_passed(
    cloudbuild: dict[str, Any], deploy_step: dict[str, Any]
) -> None:
    """All three places, or the label is declared, mapped and never applied — and
    a private staging revision fails readiness on a value the operator set."""
    assert cloudbuild["substitutions"]["_STAGING_SUBJECT_GATE_REVISION"] == ""
    assert (
        "STAGING_SUBJECT_GATE_REVISION=${_STAGING_SUBJECT_GATE_REVISION}"
        in deploy_step["env"]
    )
    script = "\n".join(deploy_step["args"])
    names = re.search(r"NAMES = \((.*?)\)", script, re.S)
    assert names is not None
    assert '"STAGING_SUBJECT_GATE_REVISION"' in names.group(1)


def test_the_release_states_the_gate_expectation_in_both_directions(
    smoke_step: dict[str, Any],
) -> None:
    """An assertion that silently does not run is worse than one never asked for.

    Production and every staging revision below `enforce` publish `enabled:
    false`, and saying so out loud is what turns "the gate did not come up" from
    a branch that quietly skips into a failed release. Keyed on the same two
    conditions the deploy mounts the secret on, not on whether a label happens to
    be set — a label without staging + enforce is already refused before the
    deploy.
    """
    script = "\n".join(smoke_step["args"])
    assert "--expect-gate-revision" in script
    assert "--expect-no-gate" in script
    assert "STAGING_SUBJECT_GATE_REVISION=${_STAGING_SUBJECT_GATE_REVISION}" in (
        smoke_step["env"]
    )
    smoke_text = _SMOKE.read_text(encoding="utf-8")
    for flag in ("--expect-gate-revision", "--expect-no-gate"):
        assert flag in smoke_text, f"{flag} is passed by the build and unknown to the script"


# --- release state cannot be forged by the checkout --------------------------
#
# `allowFailure` is what made this urgent rather than tidy. Before it, a failed
# step aborted the build and the markers were informational; after it, the build
# runs to the end and the markers ARE the verdict. And they lived in /workspace,
# which is the repository checkout — so a tree carrying `stage_deploy_ok` and
# `skip_smoke` made a FAILED deploy flow through the smoke, through the
# promotion, and out as a green release, and a committed `rc_tag` aimed the
# cleanup at whatever name it contained.
#
# Two things close it and both are needed. The state moved to `/release`, a
# build-scoped volume nothing in the tree can write; and every file under it
# opens with the id of the build that wrote it, checked on every read. The volume
# alone would still trust a file; the stamp alone would still be a file the repo
# could carry, stamp and all.


def test_the_release_state_lives_outside_the_repository_checkout(
    cloudbuild: dict[str, Any],
) -> None:
    """/workspace IS the checkout. A marker there is a file the repo can ship."""
    steps = {s["id"]: s for s in cloudbuild["steps"]}
    for step_id in ("release-state-init", *_RELEASE_STATE_STEPS):
        volumes = steps[step_id].get("volumes") or []
        assert {"name": "release-state", "path": "/release"} in volumes, (
            f"{step_id} does not mount the release-state volume, so whatever it "
            "reads or writes is in the repository checkout"
        )
    # And nothing still names one of these under /workspace. The old paths would
    # keep working — that is the whole problem with them.
    for name in (
        "stage_deploy_ok",
        "stage_smoke_ok",
        "stage_promote_ok",
        "skip_smoke",
        "rc_tag",
        "rc_url",
        "rc_revision",
    ):
        for step in cloudbuild["steps"]:
            script = "\n".join(step.get("args", []))
            assert f"/workspace/{name}" not in script, (
                f"{step.get('id')} keeps {name} in the checkout"
            )


def test_the_release_state_is_emptied_and_bound_before_anything_writes_it(
    cloudbuild: dict[str, Any],
) -> None:
    """A volume is per-build today; this step is what makes that a property of
    this file rather than of the platform's current behaviour."""
    ids = [s["id"] for s in cloudbuild["steps"]]
    init = ids.index("release-state-init")
    for step_id in _RELEASE_STATE_STEPS:
        assert init < ids.index(step_id), (
            f"release state is initialised after {step_id} has already used it"
        )
    script = "\n".join(cloudbuild["steps"][init]["args"])
    assert "find /release -mindepth 1 -delete" in script
    assert "/release/build_id" in script


def _init_script() -> str:
    body: str = _step("release-state-init")["args"][-1]
    assert body.strip(), "the init step extracted empty"
    return body.replace("$$", "$")


def _run_init(tmp_path: Path, *, build_id: str) -> subprocess.CompletedProcess[str]:
    release = tmp_path / "release"
    release.mkdir(parents=True)
    # What a checkout-shaped forgery looks like once it has been copied in: the
    # exact filenames the verdict is assembled from.
    for name in ("stage_deploy_ok", "stage_smoke_ok", "stage_promote_ok"):
        (release / name).write_text("", encoding="utf-8")
    script = "set -euo pipefail\n" + _init_script().replace("/release", str(release))
    return subprocess.run(  # noqa: S603 - a script this repo owns, on fixed input
        ["bash", "-c", script],
        env={"PATH": os.environ["PATH"], "BUILD_ID": build_id},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_a_build_with_no_id_cannot_stamp_release_state(tmp_path: Path) -> None:
    """The binding is vacuous without one.

    A marker stamped with the empty string matches a check for the empty string,
    so every reader below would be verifying nothing while looking as though it
    verified something — the worst shape a safety check can take. Refusing here
    costs a re-run and happens before the image is even built.
    """
    result = _run_init(tmp_path, build_id="")
    assert result.returncode != 0
    assert "BUILD_ID is empty" in result.stderr


def test_the_init_step_clears_state_it_did_not_write(tmp_path: Path) -> None:
    """Emptied rather than assumed empty."""
    result = _run_init(tmp_path, build_id="build-1")
    assert result.returncode == 0, result.stderr
    release = tmp_path / "release"
    assert sorted(p.name for p in release.iterdir()) == ["build_id"]
    assert release.joinpath("build_id").read_text(encoding="utf-8").strip() == "build-1"


def test_every_step_reads_release_state_through_the_same_check() -> None:
    """Four copies of a trust check that drift apart are worse than one.

    They are copies because Cloud Build gives a step no way to share a shell
    function with another one, and the alternative — a library sourced from the
    volume — puts the trust check itself in a file, which is the shape this whole
    mechanism exists to distrust.
    """
    blocks = {step: _helper_block(step) for step in _RELEASE_STATE_STEPS}
    assert len(blocks) == 4, "the release-state step list emptied out"
    reference = blocks["deploy-rc"]
    assert "stamped()" in reference and "stamp()" in reference, (
        "the helper block no longer defines the pair every step reads state through"
    )
    assert '"$$STAMP" = "$$BUILD_ID"' in reference, (
        "the check no longer compares the file's stamp against this build"
    )
    for step_id, block in blocks.items():
        assert block == reference, (
            f"{step_id}'s copy of the release-state check has drifted from deploy-rc's"
        )


# --- the marker check, proved by RUNNING the smoke step's refusal -------------


def _smoke_decline_script() -> str:
    """The smoke step's own refusal, as runnable bash.

    Extracted with the helpers it calls, from the same step, because the question
    under test is what `stamped` decides — and a local reimplementation of it
    would be testing the reimplementation.
    """
    return (
        _release_state_helpers("smoke")
        + _extract(
            "smoke",
            "# The previous stage records success by writing a marker;",
            "# Note /healthz is NOT used here.",
        )
        + '\necho "URL=$URL"\n'
    )


def test_the_smoke_declines_before_it_reaches_the_assertion_suite() -> None:
    """The half `_smoke_decline_script` cannot prove by executing anything.

    A refusal that fired after `bash infra/gcp/smoke.sh` would be a report, not a
    gate: the suite would already have run against whatever URL was lying around.
    """
    body: str = _step("smoke")["args"][-1]
    start = body.index("# The previous stage records success by writing a marker;")
    assert "bash infra/gcp/smoke.sh" in body[start:], (
        "the smoke step's refusal runs after the assertion suite it gates"
    )
    assert "exit 0" not in body[:start], (
        "an early successful exit would skip the marker check"
    )


_SMOKE_BUILD_ID = "build-under-test"


def _run_smoke_decline(
    tmp_path: Path,
    *,
    release_state: dict[str, tuple[str, ...]],
    state_build_id: str = _SMOKE_BUILD_ID,
    seed_checkout: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    release = tmp_path / "release"
    release.mkdir(parents=True)
    for name, values in release_state.items():
        _write_stamped(release, name, state_build_id, *values)
    for name in seed_checkout:
        (workspace / name).write_text("", encoding="utf-8")
    script = (
        "set -euo pipefail\n"
        + _smoke_decline_script()
        .replace("/release", str(release))
        .replace("/workspace", str(workspace))
    )
    return subprocess.run(  # noqa: S603 - a script this repo owns, on fixed input
        ["bash", "-c", script],
        env={"PATH": os.environ["PATH"], "BUILD_ID": _SMOKE_BUILD_ID},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_this_builds_own_marker_starts_the_smoke(tmp_path: Path) -> None:
    """The positive control. Without it every refusal below could be a script
    that refuses everything, which passes the same assertions and gates nothing."""
    result = _run_smoke_decline(
        tmp_path,
        release_state={
            "stage_deploy_ok": (),
            "rc_url": ("https://rc-111111112222---svc.run.app",),
        },
    )
    assert result.returncode == 0, result.stderr
    assert "URL=https://rc-111111112222---svc.run.app" in result.stdout


def test_a_marker_the_repository_carried_does_not_start_a_smoke(tmp_path: Path) -> None:
    """THE forgery, reproduced. A checkout holding `stage_deploy_ok` and
    `skip_smoke` used to walk a failed deploy through the smoke, through the
    promotion, and out as a green release — because /workspace is the checkout and
    existence was the whole of the test."""
    result = _run_smoke_decline(
        tmp_path,
        release_state={},
        seed_checkout=("stage_deploy_ok", "skip_smoke", "rc_url"),
    )
    assert result.returncode != 0
    assert "nothing to smoke" in result.stderr


def test_a_marker_another_build_wrote_does_not_start_a_smoke(tmp_path: Path) -> None:
    """Same file, right place, wrong build.

    The volume closes the checkout; the stamp is what closes everything else, and
    it is the half that survives somebody deciding the volume is a nuisance.
    """
    result = _run_smoke_decline(
        tmp_path,
        release_state={"stage_deploy_ok": (), "rc_url": ("https://someone-else",)},
        state_build_id="some-other-build",
    )
    assert result.returncode != 0
    assert "nothing to smoke" in result.stderr


def test_a_foreign_first_deploy_hand_off_does_not_skip_the_smoke(
    tmp_path: Path,
) -> None:
    """`skip_smoke` is the marker that skips the entire assertion suite, so it is
    the most valuable one to forge. An unrecognised one is simply not there, which
    leaves the turn to smoke normally rather than to skip."""
    release = tmp_path / "release"
    release.mkdir(parents=True)
    _write_stamped(release, "stage_deploy_ok", _SMOKE_BUILD_ID)
    _write_stamped(release, "rc_url", _SMOKE_BUILD_ID, "https://ours")
    _write_stamped(release, "skip_smoke", "some-other-build")
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    script = (
        "set -euo pipefail\n"
        + _smoke_decline_script()
        .replace("/release", str(release))
        .replace("/workspace", str(workspace))
    )
    result = subprocess.run(  # noqa: S603 - a script this repo owns, on fixed input
        ["bash", "-c", script],
        env={"PATH": os.environ["PATH"], "BUILD_ID": _SMOKE_BUILD_ID},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "URL=https://ours" in result.stdout, "the foreign hand-off skipped the smoke"
    assert not (release / "stage_smoke_ok").exists()


def test_a_verdict_cannot_be_assembled_from_the_checkout(tmp_path: Path) -> None:
    """The final step's whole job is to say whether the release happened.

    Three empty files in the repository used to be enough to make it say yes.
    """
    result, removed = _run_cleanup(
        tmp_path,
        rc_tag=None,
        attached_tags=[],
        markers=(),
        seed_checkout=("stage_deploy_ok", "stage_smoke_ok", "stage_promote_ok"),
    )
    assert result.returncode != 0
    assert "deploy-rc" in result.stderr
    assert "smoke" in result.stderr
    assert "promote" in result.stderr
    assert removed == []


def test_a_candidate_tag_this_build_did_not_write_is_not_removed(
    tmp_path: Path,
) -> None:
    """A pre-seeded `rc_tag` used to aim the cleanup wherever it pointed.

    On the failure path, which is where a concurrent build is most likely to be
    holding a candidate of its own, that is a build deleting a sibling's tag.
    """
    result, removed = _run_cleanup(
        tmp_path,
        rc_tag="rc-99999999aaaa",
        attached_tags=["rc-99999999aaaa"],
        state_build_id="some-other-build",
    )
    assert removed == [], f"the cleanup acted on a tag it did not write: {removed}"
    assert result.returncode != 0
    assert "this build did not write it" in result.stderr


# --- a service is bound to one environment, and the binding is closed ---------
#
# `_ENVIRONMENT` is a word the caller picks and `_SERVICE` is independently
# overridable, and every posture rule in the file — plus the application's own
# private-beta subject gate — keys on that word. So a manual build with
# `_SERVICE=<the staging service>` and `_ENVIRONMENT` left at its production
# default was judged by production's rules while deploying onto staging. The
# deploy identity holds project-wide roles/run.admin, so the reach is real.


def _service_table_script() -> str:
    return _extract(
        "deploy-rc",
        "# --- which service is this, and what may it be? ---",
        "# --- end service/environment table ---",
    )


def test_the_service_table_actually_runs_before_the_deploy() -> None:
    """The paired proof. A table consulted after the deploy decides nothing."""
    body: str = _step("deploy-rc")["args"][-1]
    start = body.index("# --- which service is this, and what may it be? ---")
    assert "gcloud run deploy" in body[start:], "the service table runs after the deploy"
    preamble = body[:start]
    assert "exit 0" not in preamble, "an early successful exit would skip the table"
    for guard in ("if false", "if [ 1 -eq 0 ]", "if /bin/false"):
        assert guard not in body, f"the table is wrapped in a guard that never runs: {guard}"


def test_the_service_name_reaches_the_rule_as_data(deploy_step: dict[str, Any]) -> None:
    """Compared, therefore an environment variable.

    Cloud Build substitutes `${_SERVICE}` before bash parses, so a rule written
    against the substitution is a rule written against source text — untestable,
    and one stray quote away from being something other than a comparison.
    """
    assert "SERVICE=${_SERVICE}" in deploy_step["env"]
    assert '"$$SERVICE"' in _step("deploy-rc")["args"][-1]


def _run_service_table(
    *, service: str, environment: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - a script this repo owns, on fixed input
        ["bash", "-c", "set -euo pipefail\n" + _service_table_script()],
        env={
            "PATH": os.environ["PATH"],
            "SERVICE": service,
            "ENVIRONMENT": environment,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    ("service", "environment"),
    [
        pytest.param(_PRODUCTION_SERVICE, "production", id="production"),
        pytest.param(_STAGING_SERVICE, "staging", id="staging"),
    ],
)
def test_the_two_releases_this_repo_actually_performs_are_accepted(
    service: str, environment: str
) -> None:
    """A closed table that refuses the releases the project makes is a table
    somebody deletes on the first Friday it blocks one."""
    result = _run_service_table(service=service, environment=environment)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("service", "environment", "why"),
    [
        pytest.param(
            _STAGING_SERVICE, "production", "may be deployed as", id="staging_as_prod"
        ),
        # The default. `_ENVIRONMENT` is `production` unless somebody sets it, so
        # this is what "I forgot the flag" looks like, and it is the substitution
        # combination that walked past both gates.
        pytest.param(_STAGING_SERVICE, "", "may be deployed as", id="staging_unset"),
        pytest.param(
            _PRODUCTION_SERVICE,
            "development",
            "may be deployed as",
            id="prod_as_development",
        ),
        pytest.param(_PRODUCTION_SERVICE, "test", "may be deployed as", id="prod_as_test"),
    ],
)
def test_a_service_deployed_under_the_wrong_environment_is_refused(
    service: str, environment: str, why: str
) -> None:
    """The word decides which rules apply; the service decides what is actually
    being changed. Letting them disagree is letting the caller pick the rules."""
    result = _run_service_table(service=service, environment=environment)
    assert result.returncode != 0
    assert why in result.stderr


def test_the_one_real_service_may_be_released_as_staging() -> None:
    """The row that has to say two words, and why.

    There is ONE Cloud Run service. `assistant.daintree.org` resolves to it, it
    runs as `production` today, and the private-staging rollout this pipeline was
    hardened for moves THAT service to `staging`. An earlier version of the table
    pinned it to `production` alone — which closed the substitution hole and, in
    the same stroke, refused the only release anybody was actually trying to make
    and left the posture gate below as dead code for every real build.
    """
    result = _run_service_table(service=_PRODUCTION_SERVICE, environment="staging")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "service",
    ["daintree-assistant-preview", "some-other-service", "daintree-assistant2", ""],
)
def test_a_service_this_file_does_not_know_is_refused(service: str) -> None:
    """Not "trusted by default".

    The deploy identity holds project-wide roles/run.admin, so an unknown name is
    every service in the project — and there is no posture rule here that knows
    what any of them are for. Adding one is adding a row, in a commit.
    """
    result = _run_service_table(service=service, environment="production")
    assert result.returncode != 0
    assert "not a service this file knows" in result.stderr
    assert "Add a row" in result.stderr


def test_the_production_exemption_is_keyed_to_the_production_service(
    tmp_path: Path,
) -> None:
    """THE bypass, closed.

    The posture gate's exemption is a fact about ONE Cloud Run service — the
    public one running open/off by product decision. Keyed on the word
    `production` instead, any build could claim it: `_SERVICE=<staging>` with
    `_ENVIRONMENT` left at its production default is the whole exploit, and it
    took the staging service public and unenforced with a green build.
    """
    result = _run_posture_gate(
        tmp_path,
        environment="production",
        service=_STAGING_SERVICE,
        auth_mode="open",
        public="yes",
    )
    assert result.returncode != 0
    assert "publicly invokable" in result.stderr


def test_the_production_service_released_as_staging_is_gated_not_exempt(
    tmp_path: Path,
) -> None:
    """The deployment this entire hardening batch exists to make safe.

    `daintree-assistant` is the production service AND the machine the private
    staging rollout runs on. Exempting it by name meant the one release anybody
    was going to make was the one release nothing checked. Declaring `staging` is
    asking to be judged by staging's rules, and this proves it gets them.
    """
    result = _run_posture_gate(
        tmp_path,
        environment="staging",
        service=_PRODUCTION_SERVICE,
        auth_mode="open",
        public="yes",
    )
    assert result.returncode != 0
    assert "publicly invokable" in result.stderr


def test_the_currently_correct_production_release_is_still_exempt(
    tmp_path: Path,
) -> None:
    """The other half, and the reason the exemption survives at all: production is
    public and `open` by a standing product decision, so a gate that refused it
    would fail the very next release and be deleted rather than fixed."""
    result = _run_posture_gate(
        tmp_path,
        environment="production",
        service=_PRODUCTION_SERVICE,
        auth_mode="open",
        public="yes",
    )
    assert result.returncode == 0, result.stderr


def test_the_posture_gate_exemption_is_the_pair_not_the_service_alone() -> None:
    """The gate reads a name it holds AND the word it was handed — both.

    Either half alone is a known-bad design. The service alone exempts the only
    service there is, so the gate never fires. The word alone lets a caller type
    `production` at a public staging service and walk past, which is the hole
    this whole block exists to close.
    """
    script = _posture_gate_script()
    assert "PRODUCTION_SERVICE=daintree-assistant" in script
    assert '"$SERVICE" = "$PRODUCTION_SERVICE"' in script
    assert '"$ENVIRONMENT" = "production"' in script
    assert '"$IS_PRODUCTION_RELEASE" != "yes"' in script


# --- absent and unreadable are different answers, everywhere ------------------


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_an_unreadable_service_stops_a_release_in_every_environment(
    environment: str, tmp_path: Path
) -> None:
    """It used to stop only a staging one, and the branch it protected is the
    one that creates a service at full traffic behind a /readyz check alone."""
    result = _run_posture_gate(
        tmp_path, environment=environment, describe_readable="no"
    )
    assert result.returncode != 0
    assert "invoker policy" in result.stderr


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_an_unreadable_invoker_policy_stops_a_release_in_every_environment(
    environment: str, tmp_path: Path
) -> None:
    """The other half of the same answer: the service is there and we cannot see
    who may call it."""
    result = _run_posture_gate(tmp_path, environment=environment, iam_readable="no")
    assert result.returncode != 0
    assert "invoker policy" in result.stderr


def _deploy_shape_script() -> str:
    return _extract(
        "deploy-rc",
        "# --- first deploy, or a release candidate? ---",
        "# --- end first deploy or release candidate ---",
    )


def test_the_deploy_shape_is_decided_before_the_deploy_and_consumed_by_it() -> None:
    """The paired proof, and the one that keeps the two halves wired together.

    The shape block sets a variable; the `if` below has to be the thing that
    reads it, or the block is a decision nothing acts on.
    """
    body: str = _step("deploy-rc")["args"][-1]
    start = body.index("# --- first deploy, or a release candidate? ---")
    end = body.index("# --- end first deploy or release candidate ---", start)
    assert "gcloud run deploy" in body[end:], "the shape is decided after the deploy"
    after = body[end:]
    assert 'if [ "$$DEPLOY_SHAPE" = "rc" ]; then' in after, (
        "nothing branches on the shape this block decided"
    )
    # And the state it reads is the one the posture check already computed. Two
    # describes is how the boolean crept back in last time.
    posture = _posture_gate_script()
    assert "SERVICE_STATE=read" in posture and "SERVICE_STATE=absent" in posture, (
        "the posture check no longer records what the describe found"
    )


def _run_deploy_shape(*, state: str | None) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ["PATH"]}
    if state is not None:
        env["SERVICE_STATE"] = state
    return subprocess.run(  # noqa: S603 - a script this repo owns, on fixed input
        [
            "bash",
            "-c",
            "set -euo pipefail\n"
            + _deploy_shape_script().replace("${_SERVICE}", "daintree-assistant")
            + '\necho "SHAPE=$DEPLOY_SHAPE"\n',
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    ("state", "shape"),
    [("read", "rc"), ("absent", "create")],
)
def test_the_two_known_states_choose_the_two_real_shapes(state: str, shape: str) -> None:
    """The positive control. Both shapes have to remain reachable, or this gate
    is just a refusal with extra steps."""
    result = _run_deploy_shape(state=state)
    assert result.returncode == 0, result.stderr
    assert f"SHAPE={shape}" in result.stdout


@pytest.mark.parametrize("state", ["unreadable", "", "yes", "no"])
def test_an_unreadable_service_never_reaches_the_full_traffic_path(state: str) -> None:
    """THE finding.

    The first-deploy path deploys at 100% of traffic behind a /readyz check
    alone — no environment readback, no smoke suite — and the boolean `describe`
    that chose it read a denied permission, a transient API error and a genuinely
    missing service as the same answer. So one flake put an existing, public,
    serving service onto an unsmoked revision and still reported green.
    """
    result = _run_deploy_shape(state=state)
    assert result.returncode != 0
    assert "Refusing to guess" in result.stderr


def test_a_shape_decided_from_nothing_refuses(tmp_path: Path) -> None:
    """SERVICE_STATE unset is what a refactor that moved the posture check out
    from under this one would look like. Under `set -u` an unguarded read would
    abort, which is safe by accident; this refuses on purpose and says why."""
    result = _run_deploy_shape(state=None)
    assert result.returncode != 0
    assert "Refusing to guess" in result.stderr


# --- an interpolated substitution is a name, or the build stops ---------------
#
# Four substitutions are expanded into SHELL SOURCE rather than passed as
# environment variables, because they identify resources inside gcloud arguments
# in a dozen places across four steps. Cloud Build expands them before bash
# parses, so each one is a fragment of the program: a `_RUNTIME_SA` of
# `x"); SERVICE_STATE=absent; REST=("` closes the flag array, overwrites the
# answer the service probe computed, and takes the full-traffic first-deploy
# branch onto a service that already exists.
#
# That single value defeats the service/environment table, the public-posture
# refusal and the deploy-shape refusal at once — all three are ordinary shell
# BELOW the point where the injected text has already run — so this is the
# precondition those rules stand on rather than a rule beside them.

#: The four names the build interpolates, and the only four it may.
_INTERPOLATED = frozenset({"_SERVICE", "_REGION", "_AR_REPO", "_RUNTIME_SA"})

#: Cloud Build's own substitutions that reach a script as SOURCE. `COMMIT_SHA`
#: and `SHORT_SHA` look platform-supplied and are overridable on a manual build,
#: so they are guarded like the four above. `PROJECT_ID` is not overridable, and
#: `BUILD_ID` reaches bash as environment data rather than as source.
_GUARDED_BUILTINS = frozenset({"COMMIT_SHA", "SHORT_SHA"})
_UNGUARDED_BUILTINS = frozenset({"PROJECT_ID"})


def test_only_the_guarded_names_are_ever_interpolated_into_a_script(
    cloudbuild: dict[str, Any],
) -> None:
    """The drift gate, and the reason this stays closed.

    A fifth substitution written into a gcloud flag would be source code that
    nothing checks, and it would look exactly like the four beside it. Both
    directions are asserted: a name that appears in a script must be guarded, and
    a guarded name that no script interpolates has stopped earning its check.
    """
    interpolated: set[str] = set()
    for step in cloudbuild["steps"]:
        body = "\n".join(str(arg) for arg in step.get("args", []))
        interpolated.update(re.findall(r"\$\{(_[A-Za-z0-9_]+)\}", body))
    assert interpolated, "no step interpolates anything — this scan found nothing to check"
    assert interpolated == set(_INTERPOLATED), (
        "the set of substitutions expanded into shell source has changed: "
        f"{sorted(interpolated)} against the guarded {sorted(_INTERPOLATED)}. "
        "Either guard the new one in substitution-guard, or pass it as an "
        "environment variable like every other operator value."
    )
    guard_env = "\n".join(_step("substitution-guard")["env"])
    for name in _INTERPOLATED:
        assert "${" + name + "}" in guard_env, (
            f"{name} is interpolated into a script and never checked"
        )

    # And Cloud Build's OWN substitutions, which are the ones nobody thinks to
    # check because they look platform-supplied. A manual build can override
    # COMMIT_SHA and SHORT_SHA, and `--labels=commit-sha=$SHORT_SHA` sits inside
    # the same flag array as the four above.
    builtins: set[str] = set()
    for step in cloudbuild["steps"]:
        body = "\n".join(str(arg) for arg in step.get("args", []))
        builtins.update(re.findall(r"(?<!\$)\$([A-Z][A-Z0-9_]*)", body))
    assert builtins, "no step interpolates a built-in — this scan found nothing"
    assert builtins <= (_GUARDED_BUILTINS | _UNGUARDED_BUILTINS), (
        "a Cloud Build substitution reaches a script as source and nothing checks "
        f"it: {sorted(builtins - _GUARDED_BUILTINS - _UNGUARDED_BUILTINS)}"
    )
    for name in _GUARDED_BUILTINS & builtins:
        assert f"{name}=${name}" in guard_env, (
            f"{name} is interpolated into a script and never checked"
        )


def test_the_guard_runs_before_anything_can_parse_a_substitution(
    cloudbuild: dict[str, Any],
) -> None:
    """A check inside `deploy-rc` could not do this.

    The interpolation happens before bash reads that step's first line, so a
    refusal written there runs after the injected text already has. And the guard
    must be able to stop the build, which `allowFailure` would take away.
    """
    ids = [s["id"] for s in cloudbuild["steps"]]
    assert ids[0] == "substitution-guard", (
        "a step that parses an unchecked substitution runs before the guard"
    )
    guard = cloudbuild["steps"][0]
    assert guard.get("allowFailure") is not True, "the guard cannot fail the build"
    # NO step declares `waitFor`, not just the guard. The whole file relies on
    # file order — the guard ahead of everything, the smoke ahead of the
    # promotion, the verdict last — and a single `waitFor: ['-']` anywhere makes
    # that step start immediately, in parallel with the guard that is meant to
    # stop it.
    for step in cloudbuild["steps"]:
        assert "waitFor" not in step, (
            f"{step.get('id')} declares waitFor, which detaches it from file order"
        )
    # And the guard itself must not interpolate what it is checking, or it is the
    # first thing the injected value reaches.
    assert "${_" not in "\n".join(guard["args"]), (
        "the guard interpolates a substitution into its own script"
    )


def _run_substitution_guard(**values: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "SERVICE": "daintree-assistant",
        "REGION": "us-central1",
        "AR_REPO": "daintree",
        "RUNTIME_SA": "daintree-assistant-run",
        "COMMIT_SHA": "0123456789abcdef0123456789abcdef01234567",
        "SHORT_SHA": "0123456",
    }
    env.update(values)
    body: str = _step("substitution-guard")["args"][-1]
    return subprocess.run(  # noqa: S603 - a script this repo owns, on fixed input
        ["bash", "-c", "set -euo pipefail\n" + body.replace("$$", "$")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_the_guard_passes_the_values_this_repo_actually_deploys_with() -> None:
    """The positive control. Every refusal below is worthless beside a check that
    refuses everything, and the defaults are what `main` releases with."""
    subs = yaml.safe_load(_CLOUDBUILD.read_text(encoding="utf-8"))["substitutions"]
    result = _run_substitution_guard(
        SERVICE=subs["_SERVICE"],
        REGION=subs["_REGION"],
        AR_REPO=subs["_AR_REPO"],
        RUNTIME_SA=subs["_RUNTIME_SA"],
    )
    assert result.returncode == 0, result.stderr
    # And the other service in the table, which is a legal name too.
    assert _run_substitution_guard(SERVICE=_STAGING_SERVICE).returncode == 0


@pytest.mark.parametrize(
    ("field", "value", "id_"),
    [
        # THE payload. It closes the flag array, overwrites the probe's answer and
        # reopens an array to swallow the rest of the step.
        pytest.param(
            "RUNTIME_SA", 'x"); SERVICE_STATE=absent; REST=("', "runtime_sa_injection"
        ),
        pytest.param("SERVICE", "daintree-assistant$(id)", "command_substitution"),
        pytest.param("SERVICE", "daintree-assistant`id`", "backtick"),
        pytest.param("SERVICE", "daintree-assistant; rm -rf /", "semicolon"),
        pytest.param("AR_REPO", "daintree && curl evil.example", "and_and"),
        pytest.param("REGION", "us-central1 --project=elsewhere", "flag_injection"),
        # grep matches per LINE, so a first line that looks like a name and a
        # payload after it is the shape a `grep -Eq` check waves through.
        pytest.param("SERVICE", "daintree-assistant\n; id", "newline_smuggle"),
        pytest.param("SERVICE", "", "empty"),
        pytest.param("SERVICE", "9-not-a-name", "leading_digit"),
        pytest.param("SERVICE", "Daintree-Assistant", "uppercase"),
        pytest.param("RUNTIME_SA", "a" * 64, "too_long"),
        # Cloud Build's own substitutions, overridable on a manual build and
        # sitting in the same flag array as the four names above.
        pytest.param(
            "SHORT_SHA", "x); SERVICE_STATE=absent; REST=(", "short_sha_injection"
        ),
        pytest.param("COMMIT_SHA", "abc1234$(id)", "commit_sha_substitution"),
        # What `gcloud builds submit` produces, which tags the image with nothing.
        pytest.param("SHORT_SHA", "", "sha_absent"),
        pytest.param("COMMIT_SHA", "ABC1234", "sha_uppercase"),
        pytest.param("SHORT_SHA", "abc12", "sha_too_short"),
    ],
)
def test_the_guard_refuses_a_substitution_that_is_not_a_name(
    field: str, value: str, id_: str
) -> None:
    """Anything outside the alphabet, whatever it would have done."""
    result = _run_substitution_guard(**{field: value})
    assert result.returncode != 0, f"{id_} was accepted as a resource name"
    assert "must be a resource NAME" in result.stderr or (
        "must be a git object name" in result.stderr
    )


def test_the_guard_never_echoes_the_value_it_refused() -> None:
    """It has just decided the value may be a program, and a build log outlives
    the build. The rule that broke is the whole diagnostic."""
    marker = "zzunlikelyzz"
    result = _run_substitution_guard(SERVICE=f'x"); {marker}; ("')
    assert result.returncode != 0
    assert marker not in result.stdout + result.stderr
