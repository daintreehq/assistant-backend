"""Validate runbook argument examples against the Daintree app's captured MCP schemas.

Only fences explicitly tagged ``json tool=<action id>`` opt in; the JSON is the
action's arguments alone. An optional ``surface=<name>`` annotation (a test
annotation, never an MCP argument) says which caller the example is written for —
any captured surface (``pane-core``, ``help-full``, ``hub-full``, ``lane-core``,
``external``, ...) — and it is validated against THAT caller's schema. Without one, the
example is validated against the first surface offering the action among the runbook's
own callers (``baseline_callers``): for most runbooks every surface, pane first, so the
ordinary pane path is the default reading; for a ``daintree.hosts`` runbook only the hub
and lane surfaces, because no other caller ever follows it.

The surfaces differ at the property level, which is the point of validating per
caller: an API-key ``external`` client is not offered ``notify`` at all, so an
example annotated for it that sends ``notify`` fails here exactly as it would
fail there. Schema validity proves structure only — the angle-bracket values in
examples are runtime substitutions, and nothing here proves they were resolved.
"""

from __future__ import annotations

import json
import re

import pytest
from daintree_mcp_surfaces import PANE_CALLERS, SURFACES, baseline_callers, input_schemas
from jsonschema import Draft202012Validator

from daintree_assistant_server.runbooks.catalog import load_catalog

EXAMPLE = re.compile(r"^```json tool=([\w.]+)(?: surface=([\w-]+))?\n(.*?)\n```$", re.M | re.S)


def _examples() -> list[tuple[str, str, str | None, str]]:
    return [
        (runbook.id, tool, surface or None, text)
        for runbook in load_catalog().runbooks
        for tool, surface, text in EXAMPLE.findall(runbook.body)
    ]


def test_tagged_examples_are_present_and_well_formed() -> None:
    examples = _examples()
    assert examples, "No executable runbook examples were collected"
    for runbook in load_catalog().runbooks:
        declared = re.findall(r"^```[^\n]*tool=[^\n]*$", runbook.body, re.M)
        parsed = EXAMPLE.findall(runbook.body)
        assert len(declared) == len(parsed), f"Malformed example fence in {runbook.id}"


@pytest.mark.parametrize(
    ("runbook_id", "tool", "surface", "text"),
    _examples(),
    ids=[f"{rid}:{tool}:{i}" for i, (rid, tool, _, _) in enumerate(_examples())],
)
def test_example_matches_the_callers_captured_schema(
    runbook_id: str, tool: str, surface: str | None, text: str
) -> None:
    if surface is not None:
        assert surface in SURFACES, f"{runbook_id}: unknown surface annotation {surface!r}"
        schemas = input_schemas(surface)
        assert tool in schemas, f"{runbook_id}: {tool} is not offered to {surface}"
    else:
        callers = baseline_callers(runbook_id)
        order = SURFACES if callers == PANE_CALLERS else callers
        offering = [s for s in order if tool in input_schemas(s)]
        assert offering, f"{runbook_id}: {tool} is not offered to any of its callers {order}"
        surface = offering[0]
        schemas = input_schemas(surface)
    arguments = json.loads(text)
    assert isinstance(arguments, dict), f"{runbook_id}: tool arguments must be an object"
    errors = [e.message for e in Draft202012Validator(schemas[tool]).iter_errors(arguments)]
    assert not errors, f"{runbook_id}: invalid {tool} example for {surface}: {errors}"
