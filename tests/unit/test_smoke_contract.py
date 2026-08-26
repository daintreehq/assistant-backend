"""`infra/gcp/smoke.sh` run against a deployment, rather than grepped.

This script is the PRE-PROMOTION GATE: `cloudbuild.yaml` refuses to move traffic
on a non-zero exit, so what it accepts and what it refuses is a release decision.
Until now nothing executed the accepting half. `tests/unit/test_deployment_wiring.py`
runs the argument parser's refusals — every one of those cases exits from the
option loop before a single request — and everything past that point was checked
by asserting that a flag's NAME appears somewhere in the file.

That is the shape of assertion this repo has already been burned by. A grep for
`--expect-build-sha` passes whether the script compares the value, compares the
wrong variable, or parses it and never looks again; the string is present in all
three. The only way to know a gate refuses a bad deployment is to give it one.

So this module stands up a fixture server that answers every route the default
run touches, and drives the real script against it: once with a coherent
deployment, which must pass, and then once per single-field mutation, each of
which must fail. The mutations are the release accidents this gate exists for —
a candidate serving the previous image, a revision that came up in the wrong
posture, a manifest carrying another Supabase project's endpoints.

Deliberately NOT covered: `--full`, which sends real model turns and spends
money, and the authenticated half of `/v1/daintree/account`, which needs a user
access token that nothing in this system will print.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SMOKE = _ROOT / "infra" / "gcp" / "smoke.sh"

BUILD_SHA = "4f1c9ae0deadbeef4f1c9ae0deadbeef4f1c9ae0"
ISSUER = "https://abcdefghij.supabase.co/auth/v1"
CLIENT_ID = "daintree-assistant"
ACCESS_TOKEN_SECONDS = 3600
SESSION_MAX_AGE = 604800

#: A deployment that is entirely in order. Every test below starts from this and
#: changes exactly one thing, so a failure names the field that caused it rather
#: than a fixture that drifted.
def _healthy_deployment() -> dict[str, Any]:
    return {
        "build_sha": BUILD_SHA,
        "environment": "staging",
        "auth_mode": "open",
        "entitlement_mode": "off",
        "configured": True,
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
        "client_id": CLIENT_ID,
        # A FIXED contract with the CLI rather than a deployment value. The
        # script pins it, so the fixture has to publish the real one.
        "redirect_uri": "http://127.0.0.1:42813/oauth/callback",
        "scopes": ["openid", "email"],
        # Real https daintree.org values: the script parses these and compares
        # the hostname, so a loopback URL would fail a check unrelated to the
        # one under test.
        "account_url": "https://staging.daintree.org/account",
        "subscribe_url": "https://staging.daintree.org/subscribe",
        "access_token_seconds": ACCESS_TOKEN_SECONDS,
        "session_max_age_seconds": SESSION_MAX_AGE,
        # The subject gate's SAFE projection — the only three facts /version
        # publishes about it. A deployment at `open` has no gate: it decides
        # nothing below `enforce`, because there is no subject to test.
        "gate_enabled": False,
        "gate_entries": 0,
        "gate_revision": "",
        # Whether the two FUNDED routes refuse an anonymous caller. A field
        # rather than a constant, because the mutation that matters is a
        # deployment that enforces everywhere cheap and serves the expensive
        # pair to anybody — which is not a malformed deployment, just a wrong
        # one, and looks identical from every other check in the script.
        "funded_routes_refuse_anonymous": True,
        # And how it refuses. A 401 carrying `invalid_request` means the SCHEMA
        # rejected the body, which is what makes a naive "not 200" assertion
        # vacuous: it would pass against a deployment with no door at all.
        "anonymous_refusal_code": "auth_required",
    }


class _Handler(BaseHTTPRequestHandler):
    """Answers the routes the default smoke run touches, and nothing else."""

    deployment: dict[str, Any]

    def log_message(self, *args: Any) -> None:  # noqa: D102 - silence the server
        return

    def _send(self, code: int, body: Any, *, echo_request_id: bool = False) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        if echo_request_id:
            incoming = self.headers.get("X-Request-Id")
            if incoming:
                self.send_header("X-Request-Id", incoming)
        self.end_headers()
        self.wfile.write(payload)

    def _auth_config(self) -> dict[str, Any]:
        d = self.deployment
        body: dict[str, Any] = {
            "version": 1,
            "environment": d["environment"],
            "configured": d["configured"],
            # The client's question — "must I hold a credential" — which is
            # `configured and mode == enforce` and so cannot tell open from
            # observe. That collapse is exactly why /version reports the mode.
            "required": bool(d["configured"]) and d["auth_mode"] == "enforce",
        }
        if not d["configured"]:
            return body
        body.update(
            {
                "issuer": d["issuer"],
                "authorization_endpoint": d["authorization_endpoint"],
                "token_endpoint": d["token_endpoint"],
                "jwks_uri": d["jwks_uri"],
                "client_id": d["client_id"],
                "redirect_uri": d["redirect_uri"],
                "scopes": d["scopes"],
                "account_url": d["account_url"],
                "subscribe_url": d["subscribe_url"],
                "session_policy": {
                    "access_token_seconds": d["access_token_seconds"],
                    "session_max_age_seconds": d["session_max_age_seconds"],
                },
            }
        )
        return body

    def _enforcing(self) -> bool:
        return bool(self.deployment["configured"]) and self.deployment["auth_mode"] == "enforce"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        d = self.deployment
        path = self.path.split("?")[0]
        if path == "/health":
            self._send(200, {"status": "ok"})
        elif path == "/readyz":
            self._send(200, {"status": "ready", "runbooks": 39, "catalog_revision": "cat-1"})
        elif path == "/version":
            self._send(
                200,
                {
                    "build_sha": d["build_sha"],
                    "protocol": {"min": 3, "max": 3},
                    "posture": {
                        "auth": d["auth_mode"],
                        "entitlement": d["entitlement_mode"],
                        "subject_gate": {
                            "enabled": d["gate_enabled"],
                            "entries": d["gate_entries"],
                            "revision": d["gate_revision"],
                        },
                    },
                },
            )
        elif path == "/v1/daintree/auth/config":
            self._send(200, self._auth_config())
        elif path == "/v1/daintree/capabilities":
            if self._enforcing() and not self.headers.get("Authorization"):
                self._send(401, {"error": {"code": "auth_required"}})
                return
            if self._enforcing():
                # A stale OpenRouter bearer is a credential this deployment
                # cannot verify, which under enforcement is not a credential.
                self._send(401, {"error": {"code": "auth_token_invalid"}})
                return
            self._send(200, {"protocol": {"min": 3, "max": 3}}, echo_request_id=True)
        elif path == "/v1/daintree/runbooks":
            self._send(200, {"data": [{"id": "daintree.foundation"}]})
        elif path == "/v1/daintree/account":
            # 401 under every posture this fixture serves: with no credential
            # there is no principal, and the route requires a real one.
            self._send(401, {"error": {"code": "auth_required"}})
        else:
            self._send(404, {"error": {"code": "not_found"}})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        d = self.deployment
        path = self.path.split("?")[0]
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if self.headers.get("X-Daintree-Protocol") == "999":
            self._send(426, {"error": {"code": "unsupported_daintree_protocol"}})
            return
        # The two routes that SPEND. Under enforcement an anonymous caller must
        # be refused BY THE DOOR — and the code says which: `auth_required` is a
        # refusal, `invalid_request` is the schema objecting to the body, and
        # only the first proves anything about authentication.
        funded = path in ("/v1/daintree/respond", "/v1/daintree/tasks")
        if funded and self._enforcing() and not self.headers.get("Authorization"):
            if d["funded_routes_refuse_anonymous"]:
                self._send(401, {"error": {"code": d["anonymous_refusal_code"]}})
            else:
                self._send(200, {"ok": True})
            return
        if path == "/v1/daintree/auth/verify":
            self._send(
                200,
                {
                    "valid": True,
                    "usable": True,
                    "reason": "ok",
                    "detail": "The provider accepted this deployment's API key.",
                    "server_holds_credential": True,
                },
            )
        elif path == "/v1/daintree/tasks":
            try:
                task = json.loads(raw).get("task")
            except ValueError:
                task = None
            if task == "no-such-task":
                self._send(404, {"error": {"code": "unknown_task"}})
            else:
                self._send(200, {"output": {}})
        elif path == "/v1/daintree/respond":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": {"code": "not_found"}})


def _serve(deployment: dict[str, Any]) -> Iterator[str]:
    handler = type("_Bound", (_Handler,), {"deployment": deployment})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _expectation_args(intended: dict[str, Any]) -> list[str]:
    """The flags `cloudbuild.yaml` would pass for the deployment it MEANT to make.

    Derived from an INTENDED deployment rather than from the one actually
    serving, which is the whole distinction the script is built on: expectations
    are stated by the release, never discovered from the host. A mutation below
    changes what is served and leaves this alone, so the gate is asked exactly
    the question a release asks it.
    """
    args = [
        "--expect-build-sha", intended["build_sha"],
        "--expect-env", intended["environment"],
        "--expect-posture", intended["auth_mode"],
        "--expect-entitlement", intended["entitlement_mode"],
    ]
    if intended["configured"]:
        args += [
            "--expect-client-id", intended["client_id"],
            "--expect-issuer", intended["issuer"],
            "--expect-authorize-url", intended["authorization_endpoint"],
            "--expect-token-url", intended["token_endpoint"],
            "--expect-jwks-url", intended["jwks_uri"],
            "--expect-scopes", ",".join(intended["scopes"]),
            "--expect-account-url", intended["account_url"],
            "--expect-subscribe-url", intended["subscribe_url"],
            "--expect-access-token-seconds", str(intended["access_token_seconds"]),
            "--expect-session-max-age", str(intended["session_max_age_seconds"]),
        ]
    else:
        args.append("--expect-no-oauth")
    # Both directions, like the OAuth pair: an unstated gate expectation is an
    # assertion that silently does not run on the one deployment the gate is for.
    if intended["gate_enabled"]:
        args += ["--expect-gate-revision", intended["gate_revision"]]
    else:
        args.append("--expect-no-gate")
    return args


def _run_smoke_against(
    deployment: dict[str, Any],
    *extra: str,
    intended: dict[str, Any] | None = None,
) -> subprocess.CompletedProcess[str]:
    """The real script, against a live fixture, with the release's expectations.

    The flags mirror what `cloudbuild.yaml`'s smoke step passes, because the
    point is to exercise the gate as the release runs it rather than a reduced
    version of it.
    """
    gen = _serve(deployment)
    url = next(gen)
    try:
        args = [
            "bash",
            str(_SMOKE),
            "--url",
            url,
            *_expectation_args(intended or _healthy_deployment()),
            *extra,
        ]
        return subprocess.run(  # noqa: S603 - a script this repo owns, on fixed input
            args,
            env={"PATH": os.environ["PATH"]},
            capture_output=True,
            text=True,
            timeout=180,
        )
    finally:
        for _ in gen:  # drive the generator's cleanup
            break


def test_a_coherent_deployment_passes_the_whole_gate() -> None:
    """The half nothing executed before.

    Every previously executed case exits from the argument loop before a request
    is made, so the script's accepting behaviour — the thing a release depends on
    — was asserted entirely by the presence of flag names in the file. If this
    fails, the gate is refusing a deployment that is correct, which blocks every
    release rather than one bad one.
    """
    result = _run_smoke_against(_healthy_deployment())
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "FAIL" not in result.stdout, result.stdout


#: One field wrong, one release accident each. Written as (label, mutation) so a
#: failure names the accident rather than a dictionary key.
_MUTATIONS = [
    pytest.param(
        {"build_sha": "0000000000000000000000000000000000000000"},
        id="the_candidate_is_serving_a_different_image",
    ),
    pytest.param({"environment": "production"}, id="a_staging_release_reached_production"),
    pytest.param({"auth_mode": "observe"}, id="the_revision_came_up_in_another_auth_mode"),
    pytest.param(
        {"entitlement_mode": "observe"}, id="the_revision_came_up_in_another_entitlement_mode"
    ),
    pytest.param({"client_id": "some-other-client"}, id="another_supabase_projects_client"),
    pytest.param(
        {"issuer": "https://zzzzzzzzzz.supabase.co/auth/v1"},
        id="another_supabase_projects_issuer",
    ),
    pytest.param(
        {"jwks_uri": "https://zzzzzzzzzz.supabase.co/auth/v1/.well-known/jwks.json"},
        id="another_projects_jwks_uri",
    ),
    pytest.param(
        {"token_endpoint": "https://zzzzzzzzzz.supabase.co/auth/v1/token"},
        id="another_projects_token_endpoint",
    ),
    pytest.param(
        {"authorization_endpoint": "https://zzzzzzzzzz.supabase.co/auth/v1/authorize"},
        id="another_projects_authorize_endpoint",
    ),
    pytest.param({"scopes": ["openid"]}, id="a_scope_was_dropped"),
    pytest.param({"scopes": ["email", "openid"]}, id="the_scopes_were_reordered"),
    pytest.param(
        {"account_url": "https://daintree.org/account"}, id="productions_account_page"
    ),
    pytest.param({"access_token_seconds": 900}, id="a_different_access_token_lifetime"),
    pytest.param({"session_max_age_seconds": 3600}, id="a_different_session_max_age"),
    pytest.param(
        {"redirect_uri": "http://127.0.0.1:9999/oauth/callback"},
        id="the_cli_callback_drifted",
    ),
]


@pytest.mark.parametrize("mutation", _MUTATIONS)
def test_the_gate_refuses_a_deployment_that_is_wrong_in_one_field(
    mutation: dict[str, Any],
) -> None:
    """Each of these publishes a body that is typed, coherent and decodable.

    That is the whole difficulty: none of them is malformed, so no shape check
    can see them. `configured=true` with the right client id is not evidence that
    the endpoints beside it belong to the same project, and a wrong `jwks_uri`
    fails later and far less legibly than a wrong client id — sign-in completes,
    and then every verification fails against keys from somewhere else.
    """
    deployment = _healthy_deployment()
    deployment.update(mutation)
    result = _run_smoke_against(deployment)
    assert result.returncode != 0, (
        f"the gate accepted a deployment with {mutation}\nstdout:\n{result.stdout}"
    )
    assert "FAIL" in result.stdout


def test_an_unconfigured_deployment_is_refused_a_manifest_expectation() -> None:
    """Asked about a manifest this deployment does not publish.

    The branch that would have checked it simply does not run, so without this
    the release reports a pass count short of what it asked for and looks like
    success — the failure mode `--expect-no-oauth` was introduced to close for
    the client id, applied to every field beside it.
    """
    deployment = _healthy_deployment()
    deployment["configured"] = False
    result = _run_smoke_against(deployment)
    assert result.returncode != 0
    assert "configured=false" in result.stdout


def test_a_deployment_with_no_oauth_passes_when_that_is_what_was_asked_for() -> None:
    """Production ships with no OAuth client, and that is a correct deployment.

    The mirror of the test above: the same unconfigured manifest must PASS when
    the release states it expects one, or the gate would refuse the posture
    production is actually in.
    """
    deployment = _healthy_deployment()
    deployment["configured"] = False
    result = _run_smoke_against(deployment, intended=deployment)
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


# --- the enforcing deployment, which is where the funded routes are guarded ---
#
# Everything above runs against the anonymous door, because that is the posture
# every deployment is in today. It leaves the enforcing half — the one the whole
# staging rollout is heading for — checked by nothing, and the hole is specific:
# under `enforce` the script skipped its entire content section, so the only
# routes that SPEND Daintree's credential were never asked anything at all.


def _enforcing_deployment() -> dict[str, Any]:
    """A private staging deployment as it is supposed to come up: identity
    enforced, entitlement enforced, a non-empty subject gate under a label."""
    deployment = _healthy_deployment()
    deployment.update(
        {
            "auth_mode": "enforce",
            "entitlement_mode": "enforce",
            "gate_enabled": True,
            "gate_entries": 4,
            "gate_revision": "beta-1",
        }
    )
    return deployment


def test_an_enforcing_deployment_passes_the_whole_gate() -> None:
    """The accepting half of the posture the rollout ends in.

    If this fails the gate refuses the cutover release, which blocks the rollout
    rather than a bad deployment.
    """
    result = _run_smoke_against(
        _enforcing_deployment(), intended=_enforcing_deployment()
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "FAIL" not in result.stdout, result.stdout


#: One field wrong on an ENFORCING deployment. Each is a release accident the
#: anonymous fixture above structurally cannot reach.
_ENFORCING_MUTATIONS = [
    pytest.param(
        {"funded_routes_refuse_anonymous": False},
        id="the_routes_that_spend_serve_anybody",
    ),
    pytest.param(
        {"anonymous_refusal_code": "invalid_request"},
        id="the_body_was_rejected_rather_than_the_caller",
    ),
    pytest.param({"gate_enabled": False}, id="the_private_beta_came_up_ungated"),
    pytest.param({"gate_entries": 0}, id="the_gate_admits_nobody"),
    pytest.param({"gate_revision": "beta-0"}, id="an_older_membership_list"),
]


@pytest.mark.parametrize("mutation", _ENFORCING_MUTATIONS)
def test_the_gate_refuses_an_enforcing_deployment_that_is_wrong_in_one_field(
    mutation: dict[str, Any],
) -> None:
    """The first two are the pair that makes the funded-route check worth having.

    `funded_routes_refuse_anonymous: False` is a deployment that enforces on
    everything cheap and serves `/respond` and `/tasks` to anybody — typed,
    coherent, and an open Daintree-funded model endpoint.

    `anonymous_refusal_code: invalid_request` is the subtler one and the reason
    the bodies the script sends are valid: a 401 that came from schema validation
    would satisfy any "did not succeed" assertion while proving nothing about
    whether a door exists at all.
    """
    deployment = _enforcing_deployment()
    deployment.update(mutation)
    result = _run_smoke_against(deployment, intended=_enforcing_deployment())
    assert result.returncode != 0, (
        f"the gate accepted an enforcing deployment with {mutation}\n"
        f"stdout:\n{result.stdout}"
    )
    assert "FAIL" in result.stdout


def test_an_unexpected_gate_on_an_anonymous_deployment_is_refused() -> None:
    """The other direction, and it is not symmetric noise.

    `allowed_subject_digests()` parses whatever is mounted whether or not the
    gate decides anything, so a membership secret attached to the wrong
    deployment shows up in the entry count and nowhere else. Production may not
    hold one at all.
    """
    deployment = _healthy_deployment()
    deployment["gate_entries"] = 4
    result = _run_smoke_against(deployment)
    assert result.returncode != 0
    assert "FAIL" in result.stdout
