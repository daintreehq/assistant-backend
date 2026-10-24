"""Validate runbook argument examples against the Daintree app's captured MCP schemas.

Only fences explicitly tagged ``json tool=<action id>`` opt in; the JSON is the
action's arguments alone. An optional ``surface=<name>`` annotation (a test
annotation, never an MCP argument) says which caller the example is written for —
any captured surface of the runbook's generation (``pane-core``, ``help-full``,
``external``, ...) — and it is validated against THAT caller's schema. Without one,
the example is validated against the first surface offering the action among the
runbook's own callers (``untagged_example_surfaces``): for most runbooks every surface
but the non-pane ones, pane first, so the ordinary pane path is the default reading;
for a ``daintree.hosts`` runbook only its own callers, because no other caller ever
follows it.

A ``host.call`` example is validated twice: the call against the caller's surface,
and its ``tool`` and ``args`` against what the host offers inside the call, the
relayed surface of the same Tool set. So a Core example cannot name the Full-only
command runner on a host, and an inner argument a relay does not take fails here.

Every current example is read against the current app captures.

The surfaces differ at the property level, which is the point of validating per
caller: an API-key ``external`` client is not offered ``notify`` at all, so an
example annotated for it that sends ``notify`` fails here exactly as it would
fail there. Schema validity proves structure only — the angle-bracket values in
examples are runtime substitutions, and nothing here proves they were resolved.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from daintree_mcp_surfaces import (
    MODELS,
    RELAYED_SURFACE,
    SURFACES_BY_MODEL,
    Model,
    input_schemas,
    untagged_example_surfaces,
)
from jsonschema import Draft202012Validator

from daintree_assistant_server.runbooks.catalog import load_catalog

EXAMPLE = re.compile(r"^```json tool=([\w.]+)(?: surface=([\w-]+))?\n(.*?)\n```$", re.M | re.S)


def _examples() -> list[tuple[Model, str, str, str | None, str]]:
    catalog = load_catalog()
    return [
        (model, runbook.id, tool, surface or None, text)
        for model in MODELS
        for runbook in catalog.runbooks
        for tool, surface, text in EXAMPLE.findall(runbook.body)
    ]


_EXAMPLES = _examples()


def test_tagged_examples_are_present_and_well_formed() -> None:
    assert _EXAMPLES, "No executable runbook examples were collected"
    catalog = load_catalog()
    for _model in MODELS:
        for runbook in catalog.runbooks:
            declared = re.findall(r"^```[^\n]*tool=[^\n]*$", runbook.body, re.M)
            parsed = EXAMPLE.findall(runbook.body)
            assert len(declared) == len(parsed), f"Malformed example fence in {runbook.id}"


def _resolve(model: Model, runbook_id: str, tool: str, surface: str | None) -> str:
    if surface is not None:
        assert surface in SURFACES_BY_MODEL[model], (
            f"{runbook_id}: unknown surface annotation {surface!r}"
        )
        assert tool in input_schemas(surface, model), (
            f"{runbook_id}: {tool} is not offered to {surface}"
        )
        return surface
    order = untagged_example_surfaces(runbook_id, model)
    offering = [s for s in order if tool in input_schemas(s, model)]
    assert offering, f"{runbook_id}: {tool} is not offered to any of its callers {order}"
    return offering[0]


def _errors(schema: dict[str, Any], arguments: Any) -> list[str]:
    return [e.message for e in Draft202012Validator(schema).iter_errors(arguments)]


def validate_example(
    model: Model, runbook_id: str, tool: str, surface: str | None, arguments: Any
) -> None:
    surface = _resolve(model, runbook_id, tool, surface)
    assert isinstance(arguments, dict), f"{runbook_id}: tool arguments must be an object"
    errors = _errors(input_schemas(surface, model)[tool], arguments)
    assert not errors, f"{runbook_id}: invalid {tool} example for {surface}: {errors}"
    if tool != "host.call":
        return
    relayed = RELAYED_SURFACE.get(surface)
    assert relayed is not None, f"{runbook_id}: host.call from {surface} reaches no relay"
    inner = arguments["tool"]
    offered = input_schemas(relayed, model)
    assert inner in offered, f"{runbook_id}: a host offers no {inner} to {surface} ({relayed})"
    errors = _errors(offered[inner], arguments.get("args", {}))
    assert not errors, f"{runbook_id}: invalid {inner} inside host.call for {relayed}: {errors}"


@pytest.mark.parametrize(
    ("model", "runbook_id", "tool", "surface", "text"),
    _EXAMPLES,
    ids=[f"{model}:{rid}:{tool}:{i}" for i, (model, rid, tool, _, _) in enumerate(_EXAMPLES)],
)
def test_example_matches_the_callers_captured_schema(
    model: Model, runbook_id: str, tool: str, surface: str | None, text: str
) -> None:
    validate_example(model, runbook_id, tool, surface, json.loads(text))


def test_untagged_examples_follow_the_caller_policy() -> None:
    """Retired tools resolve nowhere; hosts examples never borrow the pane surface."""

    def resolves(runbook_id: str, tool: str, model: Model) -> bool:
        return any(
            tool in input_schemas(s, model) for s in untagged_example_surfaces(runbook_id, model)
        )

    for tool in ("host.sendMessage", "assistant.sendMessage", "assistant.getInbox"):
        assert not resolves("daintree.release.example", tool, "control"), tool
    assert resolves("daintree.release.example", "terminal.sendCommand", "control")
    # Control: a hosts runbook is the Shell's assistant's; the relay is only ever
    # reached inside host.call, never as an untagged example's surface.
    assert resolves("daintree.hosts.example", "host.call", "control")
    assert not resolves("daintree.hosts.example", "terminal.sendCommandOwned", "control")
    assert "relayed-core" not in untagged_example_surfaces("daintree.release.example")


def _host_call(tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    call: dict[str, Any] = {"hostId": "<hostId>", "projectId": "<projectId>", "tool": tool}
    if args is not None:
        call["args"] = args
    return call


def test_a_host_call_is_validated_inside_as_well() -> None:
    hosts = "daintree.hosts.example"
    send = {"terminalId": "<id>", "command": "<brief>", "generation": 3, "handback": True}
    validate_example("control", hosts, "host.call", None, _host_call("assistant.list"))
    validate_example(
        "control", hosts, "host.call", None, _host_call("terminal.sendCommandOwned", send)
    )
    command = {"projectId": "<projectId>", "command": "npm test"}
    for tier in ("help-core", "help-full"):
        validate_example(
            "control", hosts, "host.call", tier, _host_call("project.runCommand", command)
        )
    check = {"projectId": "<projectId>", "runnerId": "<runnerId>"}
    validate_example(
        "control", hosts, "host.call", "help-full", _host_call("project.runCheck", check)
    )

    failures: list[tuple[str | None, dict[str, Any]]] = [
        # The check runner is Full only, on the host as here.
        (None, _host_call("project.runCheck", check)),
        # The relay has the owned twin only.
        (None, _host_call("terminal.sendCommand", send)),
        # A call never chains.
        (None, _host_call("host.list")),
        # The generation is the number assistant.list returned.
        (None, _host_call("terminal.sendCommandOwned", {**send, "generation": "<generation>"})),
        # A removed inbox tool is no tool at all.
        (None, _host_call("host.sendMessage", {})),
    ]
    for surface, arguments in failures:
        with pytest.raises(AssertionError):
            validate_example("control", hosts, "host.call", surface, arguments)
