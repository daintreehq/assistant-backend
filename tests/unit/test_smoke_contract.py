"""`infra/gcp/smoke.sh` run against a deployment, rather than grepped.

This script is the PRE-PROMOTION GATE: `cloudbuild.yaml` refuses to move traffic
on a non-zero exit, and `bootstrap.sh` refuses the public cutover on one. So what
it accepts and what it refuses is a release decision, and the only way to know a
gate refuses a bad deployment is to give it one.

This module stands up a fixture server that answers every route the script
touches, in the shapes the real service answers them, and drives the real script
against it: once with a coherent deployment, which must pass, and then once per
single-field mutation, each of which must fail. The mutations are the release
accidents the gate exists for — a candidate serving the previous image, a
selector that did not load, a tool renamed out from under every client.

The argument parser's refusals are executed too. They exit before any request,
and a blank `--expect-build-sha` is the one that matters: the build passes a
substitution there, and a flag that is present with the check switched off is
the single failure a gate must never have.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SMOKE = _ROOT / "infra" / "gcp" / "smoke.sh"

BUILD_SHA = "4f1c9ae0deadbeef4f1c9ae0deadbeef4f1c9ae0"
REVISION = "sha256:" + "ab" * 32
OTHER_REVISION = "sha256:" + "cd" * 32


def _healthy_deployment() -> dict[str, Any]:
    """A deployment that is entirely in order. Every mutation below starts from
    this and changes exactly one thing, so a failure names the field that caused
    it rather than a fixture that drifted."""
    return {
        "health_status": 200,
        "ready_status": 200,
        "ready": "ready",
        "runbooks": 40,
        "catalog_revision": REVISION,
        "version_status": 200,
        "build_sha": BUILD_SHA,
        "protocol_version": "2025-06-18",
        "tools": ["search_runbooks", "get_runbook"],
        "search_is_error": False,
        "search_results": [
            {
                "id": "daintree.terminals.rename",
                "selected": True,
                "instructions": "1. List the terminals.",
            },
            {"id": "daintree.foundation", "selected": False},
        ],
        "search_revision": REVISION,
        # A manually configured client gets the same current catalog.
        "headerless_search_revision": REVISION,
        "headerless_search_results": None,  # None: the same ranking as the control search
        "get_is_error": False,
        "get_runbook": {"id": "daintree.terminals.rename", "instructions": "1. List them."},
        "get_supporting": [{"id": "daintree.foundation", "instructions": "Shared judgement."}],
        # A host-control caller reporting a connected host.
        "get_supporting_connected": [
            {"id": "daintree.foundation", "instructions": "Shared judgement."},
            {"id": "daintree.hosts.foundation", "instructions": "Another machine."},
        ],
        "mcp_get_status": 405,
    }


class _Handler(BaseHTTPRequestHandler):
    deployment: ClassVar[dict[str, Any]]
    seen_auth: ClassVar[list[str | None]]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - base signature
        pass

    def _send(self, status: int, body: Any) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        self.seen_auth.append(self.headers.get("Authorization"))
        d = self.deployment
        path = self.path.split("?")[0]
        if path == "/health":
            self._send(d["health_status"], {"status": "ok"})
        elif path == "/readyz":
            self._send(
                d["ready_status"],
                {
                    "status": d["ready"],
                    "catalog_revision": d["catalog_revision"],
                    "runbooks": d["runbooks"],
                },
            )
        elif path == "/version":
            self._send(
                d["version_status"],
                {"server_version": "1.0.0", "build_sha": d["build_sha"]},
            )
        elif path == "/v1/daintree/mcp":
            self._send(d["mcp_get_status"], {"detail": "Method Not Allowed"})
        else:
            self._send(404, {"detail": "Not Found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        self.seen_auth.append(self.headers.get("Authorization"))
        d = self.deployment
        length = int(self.headers.get("content-length") or 0)
        message = json.loads(self.rfile.read(length) or b"{}")
        if self.path.split("?")[0] != "/v1/daintree/mcp":
            self._send(404, {"detail": "Not Found"})
            return
        method = message.get("method")
        control = (self.headers.get("Daintree-Host-Control") or "").strip() == "1"
        connected = (self.headers.get("Daintree-Hosts") or "").strip() == "connected"
        revision = d["search_revision"] if control else d["headerless_search_revision"]
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": d["protocol_version"],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "daintree-runbooks", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": name} for name in d["tools"]]}
        elif method == "tools/call" and message["params"]["name"] == "get_runbook":
            structured = {
                "catalog_revision": revision,
                "runbook": d["get_runbook"],
                "supporting_runbooks": d[
                    "get_supporting_connected" if connected else "get_supporting"
                ],
            }
            result = {
                "content": [{"type": "text", "text": json.dumps(structured)}],
                "structuredContent": structured,
                "isError": d["get_is_error"],
            }
        elif method == "tools/call":
            limit = message["params"]["arguments"].get("max_results", 3)
            runbooks = d["search_results"]
            if not control and d["headerless_search_results"] is not None:
                runbooks = d["headerless_search_results"]
            # Honours the limit the way the real tool does, unless a mutation
            # has put more results in than any limit allows.
            if len(runbooks) <= 3:
                runbooks = runbooks[:limit]
            structured = {"catalog_revision": revision, "runbooks": runbooks}
            result = {
                "content": [{"type": "text", "text": json.dumps(structured)}],
                "structuredContent": structured,
                "isError": d["search_is_error"],
            }
        else:
            self._send(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {"code": -32601, "message": "Method not found"},
                },
            )
            return
        self._send(200, {"jsonrpc": "2.0", "id": message.get("id"), "result": result})


@contextmanager
def _serve(deployment: dict[str, Any]) -> Iterator[tuple[str, list[str | None]]]:
    seen: list[str | None] = []
    handler = type("_Bound", (_Handler,), {"deployment": deployment, "seen_auth": seen})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        server.server_close()


def _run(*args: str, path: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - a script this repo owns, on fixed input
        ["bash", str(_SMOKE), *args],
        env={"PATH": path or os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=120,
    )


def _run_against(deployment: dict[str, Any], *extra: str) -> subprocess.CompletedProcess[str]:
    """The real script against a live fixture, with the release's expectation.

    `--expect-build-sha` is what `cloudbuild.yaml`'s smoke step passes, so it is
    the default here: the gate is exercised as the release runs it.
    """
    with _serve(deployment) as (url, _):
        return _run("--url", url, "--expect-build-sha", BUILD_SHA, *extra)


def test_a_coherent_deployment_passes_the_whole_gate() -> None:
    """If this fails, the gate is refusing a deployment that is correct, which
    blocks every release rather than one bad one."""
    result = _run_against(_healthy_deployment())
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "FAIL" not in result.stdout, result.stdout
    assert "13 passed, 0 failed" in result.stdout, result.stdout


def test_a_trailing_slash_on_the_url_is_not_a_second_route() -> None:
    with _serve(_healthy_deployment()) as (url, _):
        result = _run("--url", url + "/", "--expect-build-sha", BUILD_SHA)
    assert result.returncode == 0, result.stdout


def test_without_an_expected_sha_any_build_is_reported_not_asserted() -> None:
    """The hand-run mode: against an unknown host the script ADAPTS, which is why
    the build must always state the sha rather than rely on this."""
    deployment = _healthy_deployment()
    deployment["build_sha"] = "0" * 40
    with _serve(deployment) as (url, _):
        result = _run("--url", url)
    assert result.returncode == 0, result.stdout
    assert f"build {'0' * 40}" in result.stdout


#: One field wrong, one release accident each. Written as (label, mutation) so a
#: failure names the accident rather than a dictionary key.
_MUTATIONS = [
    ("the candidate serves the previous image", {"build_sha": "0" * 40}),
    ("the candidate was built without a sha", {"build_sha": ""}),
    ("liveness is down", {"health_status": 500}),
    ("readiness refuses", {"ready_status": 503, "ready": "starting"}),
    ("readiness answers 200 but is not ready", {"ready": "starting"}),
    ("the catalog loaded empty", {"runbooks": 0}),
    ("/version is broken", {"version_status": 500}),
    ("the MCP protocol version moved", {"protocol_version": "2024-11-05"}),
    ("the tool was renamed", {"tools": ["runbook_search"]}),
    ("exact retrieval is missing", {"tools": ["search_runbooks"]}),
    (
        "a third tool appeared",
        {"tools": ["search_runbooks", "get_runbook", "load_runbook"]},
    ),
    ("exact retrieval reports an error", {"get_is_error": True}),
    (
        "a headerless caller is served another catalog",
        {"headerless_search_revision": OTHER_REVISION},
    ),
    ("a headerless caller's search ranks nothing", {"headerless_search_results": []}),
    ("readiness names no catalog", {"catalog_revision": None}),
    (
        "a connected host-control caller is not sent the hosts foundation",
        {"get_supporting_connected": [{"id": "daintree.foundation", "instructions": "x"}]},
    ),
    (
        "exact retrieval returns another runbook",
        {"get_runbook": {"id": "daintree.foundation", "instructions": "x"}},
    ),
    (
        "exact retrieval returns an empty procedure",
        {"get_runbook": {"id": "daintree.terminals.rename", "instructions": ""}},
    ),
    ("exact retrieval drops the foundation", {"get_supporting": []}),
    ("the search reports an error", {"search_is_error": True}),
    ("the search finds nothing", {"search_results": []}),
    (
        "the search ignores max_results",
        {
            "search_results": [
                {"id": f"r{i}", "selected": True, "instructions": "x"} for i in range(4)
            ]
        },
    ),
    (
        "a selected result carries no instructions",
        {
            "search_results": [
                {"id": "daintree.terminals.rename", "selected": True, "instructions": ""}
            ]
        },
    ),
    (
        "an unselected neighbour carries its whole procedure",
        {
            "search_results": [
                {"id": "daintree.terminals.rename", "selected": True, "instructions": "x"},
                {"id": "daintree.foundation", "selected": False, "instructions": "x"},
            ]
        },
    ),
    (
        "the search ranks the wrong runbook first",
        {"search_results": [{"id": "daintree.foundation", "selected": True, "instructions": "x"}]},
    ),
    (
        "the right runbook ranks first but would not be loaded",
        {"search_results": [{"id": "daintree.terminals.rename", "selected": False}]},
    ),
    ("search and readiness disagree on the catalog", {"search_revision": "sha256:other"}),
    ("the endpoint grew a session GET", {"mcp_get_status": 200}),
]


@pytest.mark.parametrize(
    "mutation", [m for _, m in _MUTATIONS], ids=[label for label, _ in _MUTATIONS]
)
def test_the_gate_refuses_a_deployment_that_is_wrong_in_one_field(
    mutation: dict[str, Any],
) -> None:
    deployment = {**_healthy_deployment(), **mutation}
    result = _run_against(deployment)
    assert result.returncode != 0, f"accepted a deployment where {mutation}:\n{result.stdout}"
    assert "FAIL" in result.stdout, result.stdout


def test_a_response_value_is_compared_as_data_never_run_as_code(tmp_path: Path) -> None:
    """The catalog revision comes from the target. It used to be spliced into a
    Python expression, so a hostile target could run code with the operator's
    (or Cloud Build's) credentials."""
    marker = tmp_path / "pwned"
    payload = f"x' or __import__('pathlib').Path({str(marker)!r}).touch() or '"
    deployment = {
        **_healthy_deployment(), "catalog_revision": payload, "search_revision": payload,
        "headerless_search_revision": payload,
    }
    result = _run_against(deployment)
    assert not marker.exists(), result.stdout
    assert result.returncode == 0, result.stdout


def test_an_unreachable_host_fails_rather_than_passing_nothing() -> None:
    """A gate that reports "0 failed" because every request errored is the
    quietest possible way to promote a broken revision."""
    with _serve(_healthy_deployment()) as (url, _):
        pass  # closed again: the port is now refusing connections
    result = _run("--url", url, "--expect-build-sha", BUILD_SHA)
    assert result.returncode != 0
    assert "answered 000" in result.stdout


# --- the identity token, for a service that is still private -----------------


def _fake_gcloud(tmp_path: Path, script: str) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gcloud"
    stub.write_text("#!/bin/bash\n" + script, encoding="utf-8")
    stub.chmod(0o755)
    return f"{bin_dir}:{os.environ['PATH']}"


def test_the_identity_token_rides_every_request(tmp_path: Path) -> None:
    """The public cutover smokes a service that is still IAM-private, so a
    request that went out without the token would be a 403 from Cloud Run rather
    than a verdict about the revision."""
    path = _fake_gcloud(
        tmp_path,
        'case "$*" in *"print-identity-token"*) echo tok-under-test ;; esac\n',
    )
    with _serve(_healthy_deployment()) as (url, seen):
        result = _run("--url", url, "--expect-build-sha", BUILD_SHA, "--identity-token", path=path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert seen, "the fixture saw no requests"
    assert set(seen) == {"Bearer tok-under-test"}, seen


def test_no_credential_is_sent_unless_asked_for() -> None:
    with _serve(_healthy_deployment()) as (url, seen):
        result = _run("--url", url, "--expect-build-sha", BUILD_SHA)
    assert result.returncode == 0, result.stdout
    assert set(seen) == {None}, seen


def test_a_url_that_cannot_be_discovered_is_a_usage_error(tmp_path: Path) -> None:
    """With no `--url` the script asks gcloud; an empty answer must stop it
    rather than smoking the literal path `/v1/daintree/mcp`."""
    path = _fake_gcloud(tmp_path, "exit 0\n")
    result = _run(path=path)
    assert result.returncode == 2
    assert "Pass --url" in result.stderr


def test_a_discovered_url_is_smoked(tmp_path: Path) -> None:
    with _serve(_healthy_deployment()) as (url, _):
        path = _fake_gcloud(
            tmp_path,
            f'case "$*" in *"run services describe"*) echo {url} ;; esac\n',
        )
        result = _run("--expect-build-sha", BUILD_SHA, path=path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Smoke-testing {url}" in result.stdout


# --- the argument parser ------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["--url"], id="url_missing_value"),
        pytest.param(["--url", ""], id="url_blank"),
        pytest.param(["--expect-build-sha"], id="build_sha_missing_value"),
        pytest.param(["--expect-build-sha", ""], id="build_sha_blank"),
        pytest.param(["--expect-env", "production"], id="a_removed_expectation"),
        pytest.param(["--full"], id="the_removed_paid_mode"),
        pytest.param(["https://assistant.daintree.org"], id="a_positional_url"),
    ],
)
def test_the_smoke_refuses_an_argument_it_cannot_honour(args: list[str], tmp_path: Path) -> None:
    """Exit 2, before any request.

    A BLANK value is the case worth executing rather than grepping. The natural
    call passes a build substitution, and an unset one would leave the flag
    present and the check switched off. A blank `--url` would otherwise fall
    through to discovering — and smoking — the production service. The gcloud on
    PATH records that it was called and answers with a URL, so a case that
    reached discovery could not pass quietly.
    """
    called = tmp_path / "gcloud-called"
    path = _fake_gcloud(tmp_path, f"touch {called}\necho http://127.0.0.1:9\n")
    result = _run(*args, path=path)
    assert result.returncode == 2, result.stdout + result.stderr
    assert not called.exists(), "the parser fell through to service discovery"
    assert "Smoke-testing" not in result.stdout


def test_help_documents_every_flag_and_exits_zero() -> None:
    result = _run("--help")
    assert result.returncode == 0
    for flag in ("--url", "--expect-build-sha", "--identity-token"):
        assert flag in result.stdout, f"--help does not document {flag}"
