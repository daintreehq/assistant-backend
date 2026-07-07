"""Unit tests for schema-guided tool-call argument repair.

The terminal tool schemas here are the real shapes the backend receives in
``input.tools`` — copied from the vendored CLI's registry projection
(``Registry.OpenAITools``) minus descriptions. The terminal family is the
motivating case: most tools take a single ``terminalId`` string while the
batch tools declare ``terminalIds`` (an array), and the model pattern-matches
the wrong sibling.
"""

from __future__ import annotations

import json

from daintree_assistant_server.contracts.openai import FunctionDef, ToolDef
from daintree_assistant_server.services.tool_call_repair import (
    StreamToolCallBuffer,
    build_parameter_index,
    repair_arguments,
    repair_tool_call_arguments,
)

EXTRACT_PARAMS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["terminalIds"],
    "properties": {
        "instruction": {"type": "string"},
        "maxAttempts": {"type": "number"},
        "maxTokens": {"type": "number"},
        "pollIntervalMs": {"type": "number"},
        "tailBytes": {"type": "number"},
        "terminalIds": {"type": "array", "items": {"type": "string"}},
        "wait": {"type": "object"},
    },
}

SEND_PARAMS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["terminalId", "command"],
    "properties": {
        "command": {"type": "string"},
        "terminalId": {"type": "string"},
    },
}

# terminal.close declares BOTH the singular and the plural field.
CLOSE_PARAMS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "terminalId": {"type": "string"},
        "terminalIds": {"type": "array", "items": {"type": "string"}},
    },
}


def _tool(name: str, params: dict) -> ToolDef:
    return ToolDef(function=FunctionDef(name=name, parameters=params))


def _index() -> dict:
    return build_parameter_index(
        [
            _tool("terminal__extract", EXTRACT_PARAMS),
            _tool("terminal__sendCommand", SEND_PARAMS),
            _tool("terminal__close", CLOSE_PARAMS),
        ]
    )


# ---- the incident: singular field sent to a batch tool ----------------------


def test_singular_terminal_id_renamed_and_wrapped() -> None:
    args = json.dumps(
        {"terminalId": "terminal-46842310-e6ad", "instruction": "Which agent won?"}
    )
    repaired, notes = repair_tool_call_arguments("terminal__extract", args, _index())
    assert json.loads(repaired) == {
        "terminalIds": ["terminal-46842310-e6ad"],
        "instruction": "Which agent won?",
    }
    assert notes == [
        'renamed "terminalId" -> "terminalIds"',
        'wrapped "terminalIds" into a one-element array',
    ]


def test_plural_key_with_scalar_value_wrapped() -> None:
    args = json.dumps({"terminalIds": "terminal-1", "instruction": "x"})
    repaired, notes = repair_tool_call_arguments("terminal__extract", args, _index())
    assert json.loads(repaired) == {"terminalIds": ["terminal-1"], "instruction": "x"}
    assert notes == ['wrapped "terminalIds" into a one-element array']


def test_one_element_array_unwrapped_for_scalar_field() -> None:
    args = json.dumps({"terminalId": ["terminal-1"], "command": "ls"})
    repaired, notes = repair_tool_call_arguments("terminal__sendCommand", args, _index())
    assert json.loads(repaired) == {"terminalId": "terminal-1", "command": "ls"}
    assert notes


def test_case_variant_key_renamed() -> None:
    args = json.dumps({"terminal_ids": ["terminal-1"]})
    repaired, _ = repair_tool_call_arguments("terminal__extract", args, _index())
    assert json.loads(repaired) == {"terminalIds": ["terminal-1"]}


# ---- conservatism: anything ambiguous or already valid passes through -------


def test_valid_arguments_returned_verbatim() -> None:
    args = '{"terminalIds": ["terminal-1"], "instruction": "x"}'
    repaired, notes = repair_tool_call_arguments("terminal__extract", args, _index())
    assert repaired is args
    assert notes == []


def test_declared_singular_on_dual_schema_untouched() -> None:
    # terminal.close declares BOTH fields — the singular is valid, not a near miss.
    args = '{"terminalId": "terminal-1"}'
    repaired, notes = repair_tool_call_arguments("terminal__close", args, _index())
    assert repaired is args
    assert notes == []


def test_ambiguous_rename_untouched() -> None:
    # Unknown key normalizes onto TWO declared properties -> no rename.
    params = {
        "type": "object",
        "properties": {"tag": {"type": "string"}, "tags": {"type": "array"}},
    }
    args, notes = repair_arguments(params, {"Tag": "x"})
    assert args == {"Tag": "x"}
    assert notes == []


def test_unrelated_unknown_key_untouched() -> None:
    args = '{"terminalIds": ["t"], "bogus": 1}'
    repaired, notes = repair_tool_call_arguments("terminal__extract", args, _index())
    assert repaired is args
    assert notes == []


def test_wrap_skipped_on_item_type_mismatch() -> None:
    args = '{"terminalIds": 42}'
    repaired, notes = repair_tool_call_arguments("terminal__extract", args, _index())
    assert repaired is args
    assert notes == []


def test_rename_skipped_when_value_cannot_satisfy_target() -> None:
    # Renaming terminalId->terminalIds with an int value would still fail the
    # schema (array of string) — leave the original field for the model's retry.
    args = '{"terminalId": 42}'
    repaired, notes = repair_tool_call_arguments("terminal__extract", args, _index())
    assert repaired is args
    assert notes == []


def test_unknown_tool_untouched() -> None:
    args = '{"terminalId": "t"}'
    repaired, notes = repair_tool_call_arguments("nope", args, _index())
    assert repaired is args
    assert notes == []


def test_malformed_json_untouched() -> None:
    args = '{"terminalId": '
    repaired, notes = repair_tool_call_arguments("terminal__extract", args, _index())
    assert repaired is args
    assert notes == []


def test_non_object_arguments_untouched() -> None:
    for args in ("[]", '"x"', ""):
        repaired, notes = repair_tool_call_arguments("terminal__extract", args, _index())
        assert repaired is args
        assert notes == []


def test_untyped_property_untouched() -> None:
    # scratch.set's `value` declares no type — free-form JSON must pass through.
    params = {"type": "object", "properties": {"value": {}}}
    args, notes = repair_arguments(params, {"value": "anything"})
    assert args == {"value": "anything"}
    assert notes == []


def test_index_skips_tools_without_object_schema() -> None:
    index = build_parameter_index(
        [
            _tool("no_params", None),  # type: ignore[arg-type]
            _tool("empty_props", {"type": "object", "properties": {}}),
            _tool("terminal__extract", EXTRACT_PARAMS),
        ]
    )
    assert set(index) == {"terminal__extract"}


# ---- streaming buffer --------------------------------------------------------


def _fragment(idx: int, *, call_id: str = "", name: str = "", arguments: str = "") -> dict:
    frag: dict = {"index": idx}
    if call_id:
        frag["id"] = call_id
        frag["type"] = "function"
    fn: dict = {}
    if name:
        fn["name"] = name
    if arguments:
        fn["arguments"] = arguments
    if fn:
        frag["function"] = fn
    return frag


def test_buffer_consolidates_and_repairs_fragmented_call() -> None:
    buffer = StreamToolCallBuffer(_index())
    assert buffer.add([_fragment(0, call_id="call_0", name="terminal__extract")]) == []
    assert buffer.add([_fragment(0, arguments='{"terminalId": "term')]) == []
    assert buffer.add([_fragment(0, arguments='inal-1", "instruction": "x"}')]) == []
    flushed = buffer.flush()
    assert len(flushed) == 1
    call = flushed[0]
    assert call["index"] == 0
    assert call["id"] == "call_0"
    assert call["type"] == "function"
    assert call["function"]["name"] == "terminal__extract"
    assert json.loads(call["function"]["arguments"]) == {
        "terminalIds": ["terminal-1"],
        "instruction": "x",
    }
    assert buffer.repairs and buffer.repairs[0][0] == "terminal__extract"


def test_buffer_holds_all_calls_until_flush() -> None:
    # Nothing is forwarded mid-stream — a new index must NOT flush the prior
    # call (providers may interleave fragments of several calls).
    buffer = StreamToolCallBuffer(_index())
    assert buffer.add(
        [
            _fragment(
                0,
                call_id="call_0",
                name="terminal__sendCommand",
                arguments='{"terminalId": "t-1", "command": "ls"}',
            )
        ]
    ) == []
    assert buffer.add([_fragment(1, call_id="call_1", name="terminal__extract")]) == []
    assert buffer.add([_fragment(1, arguments='{"terminalIds": ["t-2"]}')]) == []
    flushed = buffer.flush()
    assert [c["id"] for c in flushed] == ["call_0", "call_1"]
    # Valid args stay byte-identical; no repair recorded.
    assert flushed[0]["function"]["arguments"] == '{"terminalId": "t-1", "command": "ls"}'
    assert buffer.repairs == []
    assert buffer.flush() == []


def test_buffer_merges_interleaved_calls() -> None:
    # One delta announces BOTH calls, later deltas interleave their argument
    # fragments — each call must still come out whole, exactly once.
    buffer = StreamToolCallBuffer(_index())
    assert buffer.add(
        [
            _fragment(0, call_id="call_0", name="terminal__extract"),
            _fragment(1, call_id="call_1", name="terminal__extract"),
        ]
    ) == []
    assert buffer.add([_fragment(1, arguments='{"terminalId": "t-b"}')]) == []
    assert buffer.add([_fragment(0, arguments='{"terminalId": "t-a"}')]) == []
    flushed = buffer.flush()
    assert [(c["index"], c["id"]) for c in flushed] == [(0, "call_0"), (1, "call_1")]
    assert json.loads(flushed[0]["function"]["arguments"]) == {"terminalIds": ["t-a"]}
    assert json.loads(flushed[1]["function"]["arguments"]) == {"terminalIds": ["t-b"]}
    assert len(buffer.repairs) == 2


def test_buffer_passthrough_on_missing_index() -> None:
    buffer = StreamToolCallBuffer(_index())
    buffer.add([_fragment(0, call_id="call_0", name="terminal__extract", arguments="{}")])
    odd = {"id": "call_x", "function": {"name": "weird", "arguments": "{}"}}
    tail = _fragment(3, call_id="call_3")
    out = buffer.add([odd, tail])
    # Pending call flushes first (consolidated), then the odd fragment and the
    # REST of the same batch verbatim.
    assert [c.get("id") for c in out] == ["call_0", "call_x", "call_3"]
    assert out[1] is odd
    assert out[2] is tail
    # From here on, everything passes through untouched.
    frag = _fragment(4, call_id="call_4")
    assert buffer.add([frag]) == [frag]
    assert buffer.flush() == []


def test_repair_exception_fails_open(monkeypatch) -> None:
    import daintree_assistant_server.services.tool_call_repair as mod

    def boom(schema, args):  # noqa: ANN001, ANN202
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "repair_arguments", boom)
    args = '{"terminalId": "t-1"}'
    repaired, notes = mod.repair_tool_call_arguments("terminal__extract", args, _index())
    assert repaired is args
    assert notes == []
