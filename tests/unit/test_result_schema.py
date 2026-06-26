"""Unit tests for the result_schema validator and value conformance."""

from __future__ import annotations

import pytest

from daintree_assistant_server.config import Settings
from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.services.result_schema import conforms, validate_result_schema


@pytest.fixture
def settings() -> Settings:
    return Settings(DAINTREE_API_KEY="k", DEEPSEEK_API_KEY="d", STATE_SIGNING_KEY_CURRENT="s")


def test_validate_accepts_safe_subset(settings: Settings) -> None:
    validate_result_schema(
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "status": {"enum": ["ok", "fail"]},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        settings,
    )


@pytest.mark.parametrize("bad_key", ["description", "title", "$ref", "examples", "default", "$comment"])
def test_validate_rejects_annotation_keys(settings: Settings, bad_key: str) -> None:
    with pytest.raises(DaintreeError) as exc:
        validate_result_schema({"type": "object", bad_key: "x"}, settings)
    assert exc.value.code == "invalid_result_schema"


def test_validate_rejects_unknown_type(settings: Settings) -> None:
    with pytest.raises(DaintreeError) as exc:
        validate_result_schema({"type": "function"}, settings)
    assert exc.value.code == "invalid_result_schema"


def test_validate_depth_bound() -> None:
    s = Settings(DAINTREE_API_KEY="k", DEEPSEEK_API_KEY="d", STATE_SIGNING_KEY_CURRENT="s", MAX_RESULT_SCHEMA_DEPTH=2)
    node: dict = {"type": "object"}
    cur = node
    for _ in range(5):
        child: dict = {"type": "object"}
        cur["properties"] = {"x": child}
        cur = child
    with pytest.raises(DaintreeError) as exc:
        validate_result_schema(node, s)
    assert exc.value.status_code == 413


def test_conforms_types_and_required() -> None:
    schema = {"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]}
    assert conforms({"count": 5}, schema) is True
    assert conforms({"count": "five"}, schema) is False  # wrong type
    assert conforms({}, schema) is False                  # missing required
    assert conforms({"count": True}, schema) is False      # bool is not integer


def test_conforms_additional_properties_false() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False}
    assert conforms({"a": "x"}, schema) is True
    assert conforms({"a": "x", "b": 1}, schema) is False


def test_conforms_enum_const_array_anyof() -> None:
    assert conforms("ok", {"enum": ["ok", "fail"]}) is True
    assert conforms("nope", {"enum": ["ok", "fail"]}) is False
    assert conforms(7, {"const": 7}) is True
    assert conforms(["a", "b"], {"type": "array", "items": {"type": "string"}}) is True
    assert conforms(["a", 1], {"type": "array", "items": {"type": "string"}}) is False
    assert conforms(5, {"anyOf": [{"type": "integer"}, {"type": "string"}]}) is True
    assert conforms(1.5, {"anyOf": [{"type": "integer"}, {"type": "boolean"}]}) is False
