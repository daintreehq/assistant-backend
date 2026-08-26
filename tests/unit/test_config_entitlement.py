"""``ENTITLEMENT_MODE`` and the URL it depends on.

The rule the whole file exists to hold: a bad entitlement configuration fails
readiness. It is never quietly demoted to ``off``, because an operator who asked
for enforcement and got an open till has no way to find out.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from daintree_assistant_server.config import (
    EXECUTOR_KEY_ENV,
    FAST_RESPONSE_KEY_ENV,
    Settings,
)

_STAGING_URL = "https://staging.daintree.org/api/assistant/entitlement"

_ENFORCED_AUTH: dict[str, object] = {
    "AUTH_MODE": "enforce",
    "SUPABASE_AUTH_ISSUER": "https://proj.supabase.co/auth/v1",
    "SUPABASE_JWKS_URL": "https://proj.supabase.co/auth/v1/.well-known/jwks.json",
    "SUPABASE_OAUTH_AUTHORIZE_URL": "https://proj.supabase.co/auth/v1/oauth/authorize",
    "SUPABASE_OAUTH_TOKEN_URL": "https://proj.supabase.co/auth/v1/oauth/token",
    "SUPABASE_OAUTH_CLIENT_ID": "daintree-assistant",
}


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "STATE_SIGNING_KEY_CURRENT": "sk",
        EXECUTOR_KEY_ENV: "sk-test",
        FAST_RESPONSE_KEY_ENV: "csk-test",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_the_shipped_default_is_off_and_asks_nothing() -> None:
    settings = _settings()
    assert settings.entitlement_mode == "off"
    assert settings.daintree_entitlement_url == ""
    assert settings.entitlement_problems() == []
    assert settings.entitlement_is_configured is False


def test_an_unknown_mode_is_rejected_by_the_type() -> None:
    with pytest.raises(ValidationError):
        _settings(ENTITLEMENT_MODE="on")


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_a_binding_mode_requires_enforced_identity(mode: str) -> None:
    problems = _settings(
        ENTITLEMENT_MODE=mode, DAINTREE_ENTITLEMENT_URL=_STAGING_URL
    ).entitlement_problems()
    assert any("requires AUTH_MODE=enforce" in p for p in problems)


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_a_binding_mode_requires_a_url(mode: str) -> None:
    problems = _settings(ENTITLEMENT_MODE=mode, **_ENFORCED_AUTH).entitlement_problems()
    assert any("requires DAINTREE_ENTITLEMENT_URL" in p for p in problems)


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_a_complete_binding_configuration_passes(mode: str) -> None:
    settings = _settings(
        ENTITLEMENT_MODE=mode, DAINTREE_ENTITLEMENT_URL=_STAGING_URL, **_ENFORCED_AUTH
    )
    assert settings.entitlement_problems() == []
    assert settings.entitlement_is_configured is True


def test_a_bad_configuration_fails_readiness_rather_than_downgrading() -> None:
    """The single most important property here.

    ``local_only_problems`` is what readiness consults, so an entitlement problem
    has to reach it — otherwise the revision starts, reports itself healthy, and
    charges nobody.
    """
    settings = _settings(ENTITLEMENT_MODE="enforce", ENVIRONMENT="staging")
    assert settings.entitlement_mode == "enforce"  # NOT silently reset to "off"
    problems = settings.local_only_problems()
    assert any("ENTITLEMENT_MODE=enforce" in p for p in problems)


# --- the URL itself, validated in every mode -------------------------------


def test_the_url_is_validated_even_while_the_mode_is_off() -> None:
    """A malformed value is not dormant under ``off``.

    The account endpoint uses it the moment somebody configures a mode, and the
    rollout is the worst time to discover it.
    """
    problems = _settings(DAINTREE_ENTITLEMENT_URL="https://evil.example.com/e").entitlement_problems()
    assert any("must point at a Daintree site" in p for p in problems)


def test_a_query_string_is_refused() -> None:
    problems = _settings(
        DAINTREE_ENTITLEMENT_URL=f"{_STAGING_URL}?user=someone-else"
    ).entitlement_problems()
    assert any("must not carry a query string" in p for p in problems)


def test_a_fragment_is_refused() -> None:
    problems = _settings(
        DAINTREE_ENTITLEMENT_URL=f"{_STAGING_URL}#frag"
    ).entitlement_problems()
    assert any("fragment" in p for p in problems)


def test_embedded_credentials_are_refused() -> None:
    problems = _settings(
        DAINTREE_ENTITLEMENT_URL="https://user:pass@staging.daintree.org/api/assistant/entitlement"
    ).entitlement_problems()
    assert any("must not embed credentials" in p for p in problems)


def test_plain_http_is_refused_outside_loopback() -> None:
    problems = _settings(
        DAINTREE_ENTITLEMENT_URL="http://staging.daintree.org/api/assistant/entitlement"
    ).entitlement_problems()
    assert any("https" in p for p in problems)


def test_a_loopback_url_is_developable() -> None:
    settings = _settings(
        DAINTREE_ENTITLEMENT_URL="http://127.0.0.1:5173/api/assistant/entitlement"
    )
    assert settings.entitlement_problems() == []
    assert settings.entitlement_is_configured is True


def test_the_loopback_carve_out_is_withdrawn_in_a_hardened_environment() -> None:
    problems = _settings(
        ENVIRONMENT="production",
        DAINTREE_ENTITLEMENT_URL="http://127.0.0.1:5173/api/assistant/entitlement",
    ).entitlement_problems()
    assert problems


def test_configured_is_independent_of_the_mode() -> None:
    """``/v1/daintree/account`` resolves whenever the URL is usable.

    A mode is what makes a verdict BINDING, not what makes it obtainable — which
    is what lets the contract be exercised against the real website before it can
    refuse anybody's turn.
    """
    settings = _settings(ENTITLEMENT_MODE="off", DAINTREE_ENTITLEMENT_URL=_STAGING_URL)
    assert settings.entitlement_is_configured is True


# --- the full mode matrix ---------------------------------------------------


@pytest.mark.parametrize("auth_mode", ["open", "observe"])
@pytest.mark.parametrize("entitlement_mode", ["observe", "enforce"])
def test_no_unenforced_identity_mode_may_carry_a_binding_entitlement_mode(
    auth_mode: str, entitlement_mode: str
) -> None:
    """``observe`` is the one worth stating separately.

    It verifies a present token and then deliberately returns the ANONYMOUS
    principal, so it looks like identity is on while there is still no subject to
    ask the website about — the combination most likely to be configured by
    someone who believes they have staged this correctly.
    """
    settings = _settings(
        AUTH_MODE=auth_mode,
        ENTITLEMENT_MODE=entitlement_mode,
        DAINTREE_ENTITLEMENT_URL=_STAGING_URL,
    )
    assert any("requires AUTH_MODE=enforce" in p for p in settings.entitlement_problems())


@pytest.mark.parametrize("auth_mode", ["open", "observe", "enforce"])
def test_entitlement_off_is_compatible_with_every_identity_mode(auth_mode: str) -> None:
    """The two rollouts are staged independently; `off` constrains neither."""
    overrides = {**_ENFORCED_AUTH, "AUTH_MODE": auth_mode, "ENTITLEMENT_MODE": "off"}
    assert _settings(**overrides).entitlement_problems() == []


def test_a_url_ending_in_a_bare_question_mark_is_still_refused() -> None:
    """`urlsplit` reports an empty query for this, so a component test passes it.

    It carries no data — which is why it should be corrected in configuration
    rather than tolerated in a value that is compared and forwarded.
    """
    problems = _settings(
        DAINTREE_ENTITLEMENT_URL=f"{_STAGING_URL}?"
    ).entitlement_problems()
    assert any("query string or fragment" in p for p in problems)


# --- the route, not just the origin -----------------------------------------
#
# The host rule answers "is this us". It cannot answer "is this the endpoint that
# is supposed to receive a forwarded bearer", and every path on every
# `daintree.org` subdomain is a place the caller's Supabase access token can be
# delivered: a form handler, a logging route, a preview deployment, something
# added next month. `services/entitlement.py` stores the configured value verbatim
# and hands it to `build_request` with nothing appended or normalised, so the
# string validated here IS the request line.


def test_the_canonical_staging_url_is_accepted() -> None:
    """The spelling every fixture in this repo and the website's own contract use.

    Stated as its own test rather than left implicit in the mode matrix: a path
    rule that is wrong in the strict direction refuses the one URL that should
    ship, and nothing else in this file would say so.
    """
    assert _settings(DAINTREE_ENTITLEMENT_URL=_STAGING_URL).entitlement_problems() == []


def test_the_canonical_loopback_url_is_accepted() -> None:
    """The carve-out relaxes host and scheme, and only those.

    A developer running the website locally still serves it at the same route, so
    there is nothing for the path rule to be lenient about.
    """
    settings = _settings(
        DAINTREE_ENTITLEMENT_URL="http://127.0.0.1:5173/api/assistant/entitlement"
    )
    assert settings.entitlement_problems() == []


@pytest.mark.parametrize(
    ("path", "why"),
    [
        pytest.param("/", "the site root", id="root"),
        pytest.param("", "no path at all", id="bare_host"),
        pytest.param("/api/assistant/entitlements", "a plural sibling", id="sibling_plural"),
        pytest.param("/api/assistant/account", "a different route", id="sibling_route"),
        pytest.param("/api/assistant/entitlementX", "a glued suffix", id="suffix_glued"),
        pytest.param("/x/api/assistant/entitlement", "a prefixed mount", id="prefixed"),
        pytest.param("/api//assistant/entitlement", "a doubled slash", id="double_slash"),
        pytest.param("/api/assistant/entitlement/", "a trailing slash", id="trailing_slash"),
        pytest.param("/API/assistant/entitlement", "a case change", id="upper_case"),
    ],
)
def test_only_the_exact_route_is_accepted_on_an_allowed_host(path: str, why: str) -> None:
    """Every one of these is on `staging.daintree.org` and passed before this rule.

    Exact equality is what makes the list this short: no case here needs a rule of
    its own, and none of them can be argued into matching.
    """
    problems = _settings(
        DAINTREE_ENTITLEMENT_URL=f"https://staging.daintree.org{path}"
    ).entitlement_problems()
    assert any("must name exactly the path" in p for p in problems), why


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/api/assistant/%65ntitlement", id="encoded_letter"),
        pytest.param("/api/%2e%2e/api/assistant/entitlement", id="encoded_dot_segments"),
        pytest.param("/api%2fassistant/entitlement", id="encoded_slash"),
    ],
)
def test_a_percent_escape_in_the_path_is_refused_on_its_own_terms(path: str) -> None:
    """Refused as an ENCODING, before equality is even asked.

    Decoding and then comparing would mean betting that our reading of an escape
    matches the website's router; not decoding would accept a string that arrives
    there as the right route by a spelling this contract never agreed to. A fixed
    literal route never needs one, so refusing costs nothing.
    """
    problems = _settings(
        DAINTREE_ENTITLEMENT_URL=f"https://staging.daintree.org{path}"
    ).entitlement_problems()
    assert any("must not percent-encode" in p for p in problems)


def test_the_loopback_carve_out_does_not_relax_the_route() -> None:
    """The carve-out is about there being no network to intercept.

    That is an argument about the host and the scheme. It says nothing about
    which route is being called, and a local website pointed at the wrong one is
    how a wrong value gets copied into a staging deployment.
    """
    problems = _settings(
        DAINTREE_ENTITLEMENT_URL="http://127.0.0.1:5173/api"
    ).entitlement_problems()
    assert any("must name exactly the path" in p for p in problems)


def test_the_route_refusal_never_echoes_the_url_or_the_path() -> None:
    """These strings reach the startup log and, outside production, a readiness body.

    The value most likely to be malformed is one with a credential pasted into it,
    and a path is somewhere a token fits. Naming the route we require says
    everything an operator needs; repeating what they wrote adds only the risk.
    """
    secret = "sekrit-token-value"
    url = f"https://staging.daintree.org/api/assistant/{secret}"
    problems = _settings(DAINTREE_ENTITLEMENT_URL=url).entitlement_problems()
    assert problems
    for problem in problems:
        assert secret not in problem
        assert url not in problem


# ---------------------------------------------------------------------------
# The approved origin, where the environment makes it a boundary.
#
# The path rule answers "is this the route" and the suffix rule answers "is this
# us", and between them they still leave every daintree.org subdomain a place a
# caller's Supabase access token can be delivered. The set of subdomains is not
# fixed — a preview deployment, a marketing host, something stood up next month —
# and a suffix rule cannot tell any of them from the website, because being a
# subdomain is the only thing it knows how to check.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("environment", "url"),
    [
        pytest.param("staging", _STAGING_URL, id="staging_website"),
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
    ],
)
def test_an_approved_origin_is_accepted(environment: str, url: str) -> None:
    """The destinations each hardened environment is supposed to have.

    The explicit `:443` matters more than it looks: the comparison is over
    ORIGINS with the default port filled in, so the two spellings are one value.
    A string comparison would accept `https://x` and refuse `https://x:443`,
    which is a rule disagreeing with itself about the same destination.
    """
    settings = _settings(ENVIRONMENT=environment, DAINTREE_ENTITLEMENT_URL=url)
    assert settings.entitlement_problems() == []


@pytest.mark.parametrize(
    ("environment", "url", "why"),
    [
        pytest.param(
            "staging",
            "https://preview-42.daintree.org/api/assistant/entitlement",
            "a preview deployment is a subdomain and is not the website",
            id="a_preview_host",
        ),
        pytest.param(
            "staging",
            "https://daintree.org/api/assistant/entitlement",
            "the apex is not the staging website",
            id="staging_given_the_apex",
        ),
        pytest.param(
            "production",
            _STAGING_URL,
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
def test_an_unapproved_origin_fails_readiness(environment: str, url: str, why: str) -> None:
    """Every one of these satisfies the suffix rule and the path rule.

    That is what makes them worth a test: nothing about them is malformed, and
    the only thing wrong is which host receives a credential that belongs to
    somebody else.
    """
    problems = _settings(
        ENVIRONMENT=environment, DAINTREE_ENTITLEMENT_URL=url
    ).entitlement_problems()
    assert any("approved" in p for p in problems), why


def test_the_environment_decides_the_rule_not_the_hostname() -> None:
    """One URL, two verdicts, and the declared environment is the only difference.

    Inferring the environment from the value being validated would let the value
    choose which rules it is judged by, which is not a check at all.
    """
    accepted = _settings(
        ENVIRONMENT="staging", DAINTREE_ENTITLEMENT_URL=_STAGING_URL
    ).entitlement_problems()
    refused = _settings(
        ENVIRONMENT="production", DAINTREE_ENTITLEMENT_URL=_STAGING_URL
    ).entitlement_problems()
    assert accepted == []
    assert refused != []


@pytest.mark.parametrize("environment", ["development", "test"])
def test_an_unhardened_environment_keeps_the_suffix_rule(environment: str) -> None:
    """Absent from the approved table, not empty in it — and the difference is
    the whole behaviour.

    Absent means "fall through to the suffix rule and the loopback carve-out",
    which is what lets somebody run the website locally against a subdomain of
    their choosing. An empty entry would mean "no origin is approved" and refuse
    every value, turning a development convenience into an unusable one.
    """
    for url in (
        "https://anything.daintree.org/api/assistant/entitlement",
        "http://127.0.0.1:5173/api/assistant/entitlement",
    ):
        settings = _settings(ENVIRONMENT=environment, DAINTREE_ENTITLEMENT_URL=url)
        assert settings.entitlement_problems() == [], url


def test_a_bad_origin_fails_readiness_rather_than_being_ignored() -> None:
    """The rule this whole file exists to hold, applied to the new check.

    An unapproved destination is refused at the door rather than quietly demoted
    to `off`. An operator who asked for enforcement and got an open till has no
    way to find out, and a destination that is silently dropped is worse still:
    the mode binds, every lookup fails, and nothing says why.
    """
    settings = _settings(
        ENVIRONMENT="staging",
        ENTITLEMENT_MODE="enforce",
        DAINTREE_ENTITLEMENT_URL="https://preview-42.daintree.org/api/assistant/entitlement",
        **_ENFORCED_AUTH,
    )
    assert settings.entitlement_mode == "enforce"
    assert settings.entitlement_problems() != []
