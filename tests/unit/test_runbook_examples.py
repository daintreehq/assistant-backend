"""Validate executable argument examples, without guessing tool names from prose.

Only fences explicitly tagged ``json tool=<CLI dotted name>`` opt in. Raw MCP
arguments are not validated against a similarly named CLI wrapper: the captured
inventory describes the outer CLI contract, not the live host's nested schema.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from daintree_assistant_server.runbooks.catalog import load_catalog

INVENTORY = Path(__file__).resolve().parents[2] / (
    "benchmarks/_harness/fixtures/cli_tool_inventory.json"
)
EXAMPLE = re.compile(r"^```json tool=([\w.]+)\n(.*?)\n```$", re.M | re.S)


def _examples() -> list[tuple[str, str, str]]:
    return [
        (runbook.id, tool, text)
        for runbook in load_catalog().runbooks
        for tool, text in EXAMPLE.findall(runbook.body)
    ]


def test_tagged_examples_are_present_and_well_formed() -> None:
    examples = _examples()
    assert examples, "No executable runbook examples were collected"
    for runbook in load_catalog().runbooks:
        declared = re.findall(r"^```[^\n]*tool=[^\n]*$", runbook.body, re.M)
        parsed = EXAMPLE.findall(runbook.body)
        assert len(declared) == len(parsed), f"Malformed example fence in {runbook.id}"


@pytest.mark.parametrize(
    ("runbook_id", "tool", "text"),
    _examples(),
    ids=[f"{rid}:{tool}:{i}" for i, (rid, tool, _) in enumerate(_examples())],
)
def test_executable_example_matches_captured_cli_schema(
    runbook_id: str, tool: str, text: str
) -> None:
    schemas = {
        entry["function"]["name"].replace("__", "."): entry["function"]["parameters"]
        for entry in json.loads(INVENTORY.read_text(encoding="utf-8"))
    }
    assert tool in schemas, f"{runbook_id}: {tool} is not a captured CLI tool"
    arguments = json.loads(text)
    assert isinstance(arguments, dict), f"{runbook_id}: tool arguments must be an object"
    validator = Draft202012Validator(schemas[tool])
    errors = [error.message for error in validator.iter_errors(arguments)]
    assert not errors, f"{runbook_id}: invalid {tool} example: {errors}"

    if tool == "timer.schedule" and arguments["payload"]["type"] == "call_safe_tool":
        call = arguments["payload"]["toolCall"]
        target = call["toolName"]
        assert target in schemas, f"{runbook_id}: unknown timer target {target}"
        nested = Draft202012Validator(schemas[target])
        errors = [error.message for error in nested.iter_errors(call.get("args", {}))]
        assert not errors, f"{runbook_id}: invalid deferred {target} arguments: {errors}"
