"""One request id, one protocol rule — asserted across every endpoint at once.

Two invariants that are cheap to state and were both quietly broken on `/tasks`:

**One identity.** The request-context middleware resolves exactly one correlation
id per request — adopting a safe inbound ``X-Request-Id`` or minting one — binds it
to every structured log line, and echoes it in the response header. A route that
echoes anything else hands the tester an id that matches nothing in the logs, which
is strictly worse than no id: they quote it in a bug report and the operator's
search comes back empty. `/tasks` used to echo a client-supplied *body* field,
which won the header (the middleware only ``setdefault``s) while the logs kept the
middleware id.

**One protocol rule.** Every endpoint that runs the caller's content through the
model checks ``X-Daintree-Protocol``. `/tasks` did not, so a client too old to
parse a task result got a 200 and a shape it could not read instead of a 426.

Both are asserted as MATRICES rather than one case per route, because the failure
mode is drift between endpoints, and a per-route test is exactly what lets one
route diverge unnoticed.
"""

from __future__ import annotations

import json

import pytest
from conftest import respond_body
from fakes import make_result, selection_result

from daintree_assistant_server import PROTOCOL_MAX, PROTOCOL_MIN

# Bodies for the endpoints that require the protocol header. Each is otherwise
# valid, so a non-200 can only come from the protocol check itself.
PROTOCOL_ROUTES: list[tuple[str, dict[str, object]]] = [
    ("/v1/daintree/respond", respond_body([{"role": "user", "content": "hi"}])),
    (
        "/v1/daintree/runbooks/resolve",
        {"protocol_version": 3, "session_id": "s-protocol", "action": "clear"},
    ),
    (
        "/v1/daintree/tasks",
        {"task": "terminal_judge", "input": {"question": "pass?", "tail": "OK"}},
    ),
]


@pytest.fixture
def ready_provider(fake_provider):
    """A provider that answers both a turn and a JSON utility task."""
    fake_provider.selection_result = selection_result([])
    fake_provider.complete_result = make_result(
        content=json.dumps({"reason": "ok", "confidence": 0.8, "matched": True})
    )
    return fake_provider


# ---------------------------------------------------------------------------
# One protocol rule.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,body", PROTOCOL_ROUTES, ids=lambda v: v if isinstance(v, str) else "")
def test_an_unsupported_protocol_header_is_426_on_every_content_route(
    client, auth, ready_provider, path, body
) -> None:
    r = client.post(
        path, headers={**auth, "X-Daintree-Protocol": str(PROTOCOL_MAX + 1)}, json=body
    )
    assert r.status_code == 426, f"{path} accepted a future protocol"
    assert r.json()["error"]["code"] == "unsupported_daintree_protocol"


@pytest.mark.parametrize("path,body", PROTOCOL_ROUTES, ids=lambda v: v if isinstance(v, str) else "")
def test_a_malformed_protocol_header_is_426_on_every_content_route(
    client, auth, ready_provider, path, body
) -> None:
    """Not an int at all. Rejected rather than ignored: a client sending garbage
    here has a bug we should surface, and silently treating it as "unspecified"
    is how a genuine mismatch stays invisible."""
    r = client.post(path, headers={**auth, "X-Daintree-Protocol": "two"}, json=body)
    assert r.status_code == 426, f"{path} accepted a malformed protocol header"


@pytest.mark.parametrize("path,body", PROTOCOL_ROUTES, ids=lambda v: v if isinstance(v, str) else "")
def test_the_supported_protocol_header_is_accepted_on_every_content_route(
    client, auth, ready_provider, path, body
) -> None:
    """The negative control. Without it, a route that rejected EVERY header value
    would pass the two tests above."""
    r = client.post(
        path, headers={**auth, "X-Daintree-Protocol": str(PROTOCOL_MIN)}, json=body
    )
    assert r.status_code == 200, f"{path} rejected a supported protocol: {r.text}"


@pytest.mark.parametrize("path,body", PROTOCOL_ROUTES, ids=lambda v: v if isinstance(v, str) else "")
def test_an_unsupported_protocol_beats_an_unparseable_body(
    client, auth, ready_provider, path, body
) -> None:
    """426 must win over 400, and this is the realistic case rather than a corner.

    A client one protocol AHEAD is precisely the client whose body shape this build
    cannot parse — that is what a protocol bump means. Checking the header inside
    the handler would never reach this request: FastAPI validates the body first,
    so the caller gets a 400 itemising fields they believe are correct, and no hint
    that the version is the problem. Making it a dependency reverses that, because
    dependencies resolve before body validation.
    """
    r = client.post(
        path,
        headers={**auth, "X-Daintree-Protocol": str(PROTOCOL_MAX + 1)},
        json={**body, "a_field_from_the_future": {"nested": True}},
    )
    assert r.status_code == 426, f"{path} blamed the body instead of the protocol"


@pytest.mark.parametrize("path,body", PROTOCOL_ROUTES, ids=lambda v: v if isinstance(v, str) else "")
def test_an_omitted_protocol_header_stays_permitted(
    client, auth, ready_provider, path, body
) -> None:
    """The header is a declaration, not a requirement. Omitting it is how a
    caller says "whatever you serve" — only a PRESENT, out-of-range value fails."""
    r = client.post(path, headers=auth, json=body)
    assert r.status_code == 200, f"{path} required the protocol header: {r.text}"


# ---------------------------------------------------------------------------
# One request identity.
# ---------------------------------------------------------------------------

# Every route that answers with a body a tester might quote an id from, paired
# with a request that succeeds.
ID_ROUTES: list[tuple[str, str, dict[str, object] | None]] = [
    *[("POST", path, body) for path, body in PROTOCOL_ROUTES],
    ("GET", "/v1/daintree/capabilities", None),
    ("GET", "/v1/daintree/runbooks", None),
]


def _send(client, method: str, path: str, body: dict[str, object] | None, headers: dict[str, str]):
    if method == "GET":
        return client.get(path, headers=headers)
    return client.post(path, headers=headers, json=body)


@pytest.mark.parametrize("method,path,body", ID_ROUTES, ids=[r[1] for r in ID_ROUTES])
def test_every_route_mints_a_request_id_when_none_is_supplied(
    client, auth, ready_provider, method, path, body
) -> None:
    r = _send(client, method, path, body, auth)
    assert r.status_code == 200, r.text
    assert r.headers.get("X-Request-Id"), f"{path} answered without a request id"


@pytest.mark.parametrize("method,path,body", ID_ROUTES, ids=[r[1] for r in ID_ROUTES])
def test_every_route_adopts_a_safe_inbound_request_id(
    client, auth, ready_provider, method, path, body
) -> None:
    r = _send(client, method, path, body, {**auth, "X-Request-Id": "cli-abc-123"})
    assert r.status_code == 200, r.text
    assert r.headers["X-Request-Id"] == "cli-abc-123", f"{path} discarded the caller's id"


@pytest.mark.parametrize("method,path,body", ID_ROUTES, ids=[r[1] for r in ID_ROUTES])
def test_every_route_replaces_an_id_that_carries_the_bearer(
    client, auth, ready_provider, method, path, body
) -> None:
    """A caller's own OpenRouter key is *structurally* a valid request id, so a
    script that pastes it into the wrong variable would have us log and echo their
    spend-authorizing credential. Refused everywhere, not just where a route
    happened to remember to."""
    token = auth["Authorization"].split(" ", 1)[1]
    r = _send(client, method, path, body, {**auth, "X-Request-Id": token})
    assert r.status_code == 200, r.text
    assert r.headers["X-Request-Id"] != token, f"{path} echoed the bearer as a request id"


def test_a_task_cannot_supply_its_own_request_id(client, auth, ready_provider) -> None:
    """The specific regression this suite exists for.

    `/tasks` accepted a `request_id` body field and echoed it as the response
    header, while the logs kept the middleware id — two identities for one
    request. The field is gone, and ``extra="forbid"`` turns a client still
    sending it into a loud 400 rather than a silent ignore.
    """
    r = client.post(
        "/v1/daintree/tasks",
        headers=auth,
        json={
            "task": "terminal_judge",
            "input": {"question": "pass?", "tail": "OK"},
            "request_id": "client-invented-id",
        },
    )
    assert r.status_code == 400
    assert "request_id" in r.text


# ---------------------------------------------------------------------------
# The invariant proper: the header id is the SAME id everywhere else.
#
# The tests above only prove each route emits *an* id. That is the weaker half —
# two channels can each carry a well-formed id and still disagree, which is
# exactly the bug that shipped. These compare them.
# ---------------------------------------------------------------------------


def test_a_respond_body_reports_the_same_id_as_its_header(client, auth, ready_provider) -> None:
    r = client.post(
        "/v1/daintree/respond",
        headers={**auth, "X-Request-Id": "cli-body-match"},
        json=respond_body([{"role": "user", "content": "hi"}]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["request_id"] == r.headers["X-Request-Id"] == "cli-body-match"


def test_a_resolve_body_reports_the_same_id_as_its_header(client, auth, ready_provider) -> None:
    r = client.post(
        "/v1/daintree/runbooks/resolve",
        headers={**auth, "X-Request-Id": "cli-resolve-match"},
        json={"protocol_version": 3, "session_id": "s", "action": "clear"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["request_id"] == r.headers["X-Request-Id"] == "cli-resolve-match"


def test_a_minted_id_also_matches_between_body_and_header(client, auth, ready_provider) -> None:
    """The adopted-id cases above would pass even if body and header independently
    echoed the caller's string. With nothing supplied, agreeing on a random value
    can only happen if there is genuinely one id."""
    r = client.post(
        "/v1/daintree/respond",
        headers=auth,
        json=respond_body([{"role": "user", "content": "hi"}]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["request_id"] == r.headers["X-Request-Id"]


def test_a_streaming_meta_event_reports_the_same_id_as_its_header(
    client, auth, ready_provider
) -> None:
    with client.stream(
        "POST",
        "/v1/daintree/respond",
        headers={**auth, "X-Request-Id": "cli-stream-match"},
        json=respond_body(
            [{"role": "user", "content": "hi"}], generation={"stream": True}
        ),
    ) as r:
        assert r.status_code == 200
        header_id = r.headers["X-Request-Id"]
        meta = next(
            json.loads(line[6:])
            for line in r.iter_lines()
            if line.startswith("data: ") and "request_id" in line
        )
    assert meta["request_id"] == header_id == "cli-stream-match"


def test_a_streaming_error_before_meta_still_carries_the_request_id(
    client, auth, fake_provider
) -> None:
    """The case the SSE contract exists for.

    A preparation failure emits `error` as the FIRST event — there is no `meta`
    yet — so without an id on the error event the one stream a tester reports is
    the one whose identity never reached them. The HTTP status is 200 by then
    (headers are long committed), which is why this cannot be reported any other
    way.
    """
    from daintree_assistant_server.errors import upstream_unavailable

    fake_provider.selection_result = selection_result([])
    fake_provider.stream_error = upstream_unavailable("provider is down")

    with client.stream(
        "POST",
        "/v1/daintree/respond",
        headers={**auth, "X-Request-Id": "cli-stream-error"},
        json=respond_body(
            [{"role": "user", "content": "hi"}], generation={"stream": True}
        ),
    ) as r:
        assert r.status_code == 200
        header_id = r.headers["X-Request-Id"]
        events = [
            json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")
        ]
    errors = [e for e in events if "error" in e]
    assert errors, f"expected a terminal error event, got {events}"
    assert errors[0]["request_id"] == header_id == "cli-stream-error"


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("unsupported_protocol", 426),
        ("unknown_task", 404),
        ("body_too_large", 413),
    ],
)
def test_every_failure_status_still_carries_a_request_id(client, auth, kind, expected) -> None:
    """The id matters MOST on the responses a tester actually reports, and the
    failure paths are where it is easiest to lose — each is handled by different
    code (an auth dependency, a route dependency, a validator, and the outermost
    body-cap middleware, which answers ABOVE the middleware that mints the id)."""
    task = {"task": "terminal_judge", "input": {"question": "q", "tail": "t"}}
    # There is no auth-shaped failure left to test here: the Authorization header
    # is ignored entirely, so no request is refused for its credential. What
    # remains are the paths that still fail, each handled by different code — a
    # route dependency, a validator, and the outermost body-cap middleware, which
    # answers ABOVE the middleware that mints the id.
    if kind == "unsupported_protocol":
        r = client.post(
            "/v1/daintree/tasks",
            headers={**auth, "X-Daintree-Protocol": "999"},
            json=task,
        )
    elif kind == "unknown_task":
        r = client.post("/v1/daintree/tasks", headers=auth, json={"task": "nope", "input": {}})
    else:
        r = client.post(
            "/v1/daintree/tasks",
            headers=auth,
            json={"task": "terminal_judge", "input": {"tail": "x" * (9 * 1024 * 1024)}},
        )
    assert r.status_code == expected, r.text
    assert r.headers.get("X-Request-Id"), f"{kind} ({expected}) answered without a request id"


def test_an_unhandled_500_carries_the_same_id_it_logged(client, auth, caplog) -> None:
    """The outermost handler runs ABOVE the request-context middleware, after it
    has unwound: its contextvars are cleared and its `return response` never ran.
    Left alone, the single failure a tester is certain to report is the one they
    can quote no id for and whose log line nobody can find. The id survives on
    request.state, and this asserts it reaches BOTH places.
    """
    from fastapi.testclient import TestClient

    app = client.app

    @app.get("/_test_unhandled_500")
    def _boom() -> dict[str, str]:  # pragma: no cover - raises by design
        raise RuntimeError("kaboom")

    try:
        with TestClient(app, raise_server_exceptions=False) as raw:
            r = raw.get("/_test_unhandled_500", headers={**auth, "X-Request-Id": "cli-500"})
    finally:
        app.router.routes = [
            route
            for route in app.router.routes
            if getattr(route, "path", None) != "/_test_unhandled_500"
        ]

    assert r.status_code == 500
    assert r.headers.get("X-Request-Id") == "cli-500"


# ---------------------------------------------------------------------------
# Duplicate Authorization headers, through the real ASGI stack.
#
# Unit tests cover the extractor; these cover the WIRING, which is where the bug
# actually lived — a `Headers.get` in one middleware and a `dict(raw_headers)` in
# the other, each dropping a different one of two headers.
# ---------------------------------------------------------------------------


def test_a_second_authorization_header_is_masked_on_an_ordinary_request(client) -> None:
    """`Headers.get` returns the first value, so a credential in the second was
    invisible to the request-id overlap check and would have been echoed back."""
    second = "sk-or-v1-second-header-credential"
    response = client.get(
        "/v1/daintree/capabilities",
        headers=[
            ("Authorization", "Bearer sk-or-v1-first-header"),
            ("Authorization", f"Bearer {second}"),
            ("X-Request-Id", second),
        ],
    )
    assert second not in response.headers["X-Request-Id"]


def test_a_second_authorization_header_is_masked_on_the_413_path(client) -> None:
    """The body-limit middleware is OUTERMOST and builds its own id from
    `dict(scope["headers"])`, which keeps the LAST value — so it dropped the
    opposite header from the one the inner middleware dropped. Both paths mint
    their own id, so both had to be fixed, and only an oversized body reaches this
    one."""
    first = "sk-or-v1-first-header-credential"
    response = client.post(
        "/v1/daintree/respond",
        headers=[
            ("Authorization", f"Bearer {first}"),
            ("Authorization", "Bearer sk-or-v1-second-header"),
            ("X-Request-Id", first),
            ("Content-Type", "application/json"),
        ],
        content=b'{"x":"' + b"a" * (9 * 1024 * 1024) + b'"}',
    )
    assert response.status_code == 413
    assert first not in response.headers["X-Request-Id"]
