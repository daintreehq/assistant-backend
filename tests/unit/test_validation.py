"""Native respond-request validation tests."""

from __future__ import annotations

import pytest

from daintree_assistant_server.config import CEREBRAS_MODEL, Settings
from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.services.validation import validate_respond_request


@pytest.fixture
def settings() -> Settings:
    return Settings(CEREBRAS_API_KEY="csk", STATE_SIGNING_KEY_CURRENT="s")


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "CEREBRAS_API_KEY": "csk", "STATE_SIGNING_KEY_CURRENT": "s",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _req(
    *,
    messages: list | None = None,
    tools: list | None = None,
    tool_choice: object = None,
    generation: dict | None = None,
    startup: dict | None = None,
) -> RespondRequest:
    inp: dict = {"messages": messages or [{"role": "user", "content": "hi"}]}
    if tools is not None:
        inp["tools"] = tools
    if tool_choice is not None:
        inp["tool_choice"] = tool_choice
    body: dict = {
        "protocol_version": 2,
        "session": {"id": "s", "turn_id": "t"},
        "input": inp,
        "startup": startup if startup is not None else {},
    }
    if generation is not None:
        body["generation"] = generation
    return RespondRequest.model_validate(body)


def test_valid_request_returns_upstream_model(settings: Settings) -> None:
    assert validate_respond_request(_req(), settings) == CEREBRAS_MODEL


@pytest.mark.parametrize("instructions", ["12345678", "éééé"])
def test_startup_project_instructions_accept_exact_byte_limit(instructions: str) -> None:
    settings = _settings(CAP_PROJECT_INSTRUCTIONS=8)
    validate_respond_request(
        _req(startup={"project_instructions": instructions}),
        settings,
    )


@pytest.mark.parametrize("instructions", ["123456789", "ééééé"])
def test_startup_project_instructions_reject_over_byte_limit(instructions: str) -> None:
    settings = _settings(CAP_PROJECT_INSTRUCTIONS=8)
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(
            _req(startup={"project_instructions": instructions}),
            settings,
        )
    assert exc.value.status_code == 413
    assert exc.value.param == "startup.project_instructions"


@pytest.mark.parametrize("role", ["system", "developer"])
def test_system_messages_rejected(settings: Settings, role: str) -> None:
    req = _req(messages=[{"role": role, "content": "x"}, {"role": "user", "content": "y"}])
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(req, settings)
    assert exc.value.code == "system_messages_not_allowed"
    assert exc.value.param == "input.messages[0].role"


def test_system_message_scanned_anywhere(settings: Settings) -> None:
    req = _req(messages=[{"role": "user", "content": "a"}, {"role": "system", "content": "b"}])
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(req, settings)
    assert exc.value.param == "input.messages[1].role"


def test_unknown_role_rejected(settings: Settings) -> None:
    req = _req(messages=[{"role": "robot", "content": "x"}])
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(req, settings)
    assert exc.value.code == "unsupported_role"


@pytest.mark.parametrize("name", ["skill.find", "skill.load", "skill__find", "skill__load", "daintree_internal__x"])
def test_reserved_tools_rejected(settings: Settings, name: str) -> None:
    req = _req(tools=[{"type": "function", "function": {"name": name}}])
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(req, settings)
    assert exc.value.code == "reserved_tool_name"


def test_allowed_wire_safe_tools(settings: Settings) -> None:
    req = _req(tools=[
        {"type": "function", "function": {"name": "skill__run__get"}},
        {"type": "function", "function": {"name": "skill__step__advance"}},
    ])
    validate_respond_request(req, settings)


def test_duplicate_tool_rejected(settings: Settings) -> None:
    req = _req(tools=[
        {"type": "function", "function": {"name": "git__status"}},
        {"type": "function", "function": {"name": "git__status"}},
    ])
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(req, settings)
    assert exc.value.code == "duplicate_tool_name"


def test_tool_choice_must_be_offered(settings: Settings) -> None:
    req = _req(
        tools=[{"type": "function", "function": {"name": "git__status"}}],
        tool_choice={"name": "other"},
    )
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(req, settings)
    assert exc.value.code == "tool_choice_not_offered"


def test_tool_message_requires_tool_call_id(settings: Settings) -> None:
    req = _req(messages=[{"role": "tool", "content": "x"}])
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(req, settings)
    assert exc.value.code == "missing_tool_call_id"


def test_dangling_tool_message_rejected(settings: Settings) -> None:
    # A tool message with no preceding assistant tool_call of the same id.
    req = _req(messages=[
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "ok"},
        {"role": "tool", "content": "result", "tool_call_id": "c1"},
    ])
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(req, settings)
    assert exc.value.code == "dangling_tool_message"


def test_valid_tool_call_sequence_accepted(settings: Settings) -> None:
    req = _req(messages=[
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "git__status", "arguments": "{}"}}
        ]},
        {"role": "tool", "content": "clean", "tool_call_id": "c1"},
    ])
    validate_respond_request(req, settings)


def test_duplicate_tool_call_id_in_message_rejected(settings: Settings) -> None:
    req = _req(messages=[{
        "role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "git__status", "arguments": "{}"}},
            {"id": "c1", "type": "function", "function": {"name": "fs__read", "arguments": "{}"}},
        ],
    }])
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(req, settings)
    assert exc.value.code == "duplicate_tool_call_id"


def test_remote_image_rejected_by_default(settings: Settings) -> None:
    req = _req(messages=[{
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}],
    }])
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(req, settings)
    assert exc.value.code == "remote_image_not_allowed"


def test_data_url_image_allowed(settings: Settings) -> None:
    req = _req(messages=[{
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}],
    }])
    validate_respond_request(req, settings)


def test_too_many_tools(settings: Settings) -> None:
    tools = [{"type": "function", "function": {"name": f"t{i}"}} for i in range(settings.max_tools + 1)]
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(_req(tools=tools), settings)
    assert exc.value.status_code == 413


def test_tool_parameters_root_must_be_object_type(settings: Settings) -> None:
    req = _req(tools=[{"type": "function", "function": {"name": "t", "parameters": {"type": "array"}}}])
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(req, settings)
    assert exc.value.code == "invalid_tool_schema"


def test_tool_schema_required_must_be_string_list(settings: Settings) -> None:
    req = _req(tools=[{
        "type": "function",
        "function": {"name": "t", "parameters": {"type": "object", "required": [1, 2]}},
    }])
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(req, settings)
    assert exc.value.code == "invalid_tool_schema"


def test_tool_schema_depth_exceeded_413() -> None:
    settings = _settings(MAX_TOOL_SCHEMA_DEPTH=3)
    deep: dict[str, object] = {"type": "object"}
    node = deep
    for _ in range(8):
        child: dict[str, object] = {"type": "object"}
        node["properties"] = {"x": child}
        node = child
    req = _req(tools=[{"type": "function", "function": {"name": "t", "parameters": deep}}])
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(req, settings)
    assert exc.value.status_code == 413


def test_tool_schema_too_many_properties_413() -> None:
    settings = _settings(MAX_TOOL_SCHEMA_PROPERTIES=2)
    params = {"type": "object", "properties": {"a": {}, "b": {}, "c": {}}}
    req = _req(tools=[{"type": "function", "function": {"name": "t", "parameters": params}}])
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(req, settings)
    assert exc.value.status_code == 413


def test_tool_schema_oversized_string_413() -> None:
    settings = _settings(MAX_TOOL_SCHEMA_STRING_BYTES=16)
    params = {"type": "object", "properties": {"a": {"type": "string", "title": "x" * 50}}}
    req = _req(tools=[{"type": "function", "function": {"name": "t", "parameters": params}}])
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(req, settings)
    assert exc.value.status_code == 413


def test_valid_nested_tool_schema_accepted(settings: Settings) -> None:
    params = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "A file path."},
            "opts": {"type": "object", "properties": {"deep": {"type": "boolean"}}},
        },
        "required": ["path"],
    }
    validate_respond_request(
        _req(tools=[{"type": "function", "function": {"name": "fs__read", "parameters": params}}]),
        settings,
    )


def test_json_object_response_format_with_tools_rejected(settings: Settings) -> None:
    """DeepSeek rejects JSON mode + function calling; catch it at validation."""
    req = _req(
        tools=[{"type": "function", "function": {"name": "git__status"}}],
        generation={"response_format": "json_object"},
    )
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(req, settings)
    assert exc.value.code == "response_format_with_tools"
    assert exc.value.param == "generation.response_format"
    assert exc.value.status_code == 400


def test_json_object_response_format_without_tools_accepted(settings: Settings) -> None:
    validate_respond_request(_req(generation={"response_format": "json_object"}), settings)


def test_json_object_response_format_with_empty_tools_accepted(settings: Settings) -> None:
    # tools: [] never reaches upstream as a tools array, so JSON mode is fine.
    validate_respond_request(
        _req(tools=[], generation={"response_format": "json_object"}), settings
    )


def test_text_response_format_with_tools_accepted(settings: Settings) -> None:
    """'text' is the default output mode — fine alongside tools (never forwarded)."""
    req = _req(
        tools=[{"type": "function", "function": {"name": "git__status"}}],
        generation={"response_format": "text"},
    )
    validate_respond_request(req, settings)
