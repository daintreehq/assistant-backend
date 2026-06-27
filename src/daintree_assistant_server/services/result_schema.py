"""Caller-supplied ``result_schema`` validation for ``terminal_extract_json.v1``.

The schema is caller data that is BOTH rendered into a server-owned prompt and
used to check the model's output. So it is:

1. restricted to a safe JSON-Schema subset via an **allow-list** of keywords —
   annotation/prompt-like keys (``description``, ``title``, ``examples``,
   ``$comment``, ``$ref``, ``instructions``, ``system``, …) are *rejected*, not
   stripped, so nothing instruction-like can ride into the prompt; and
2. bounded in serialized size, depth, property count, enum size, and string
   length.

:func:`conforms` then checks the model's output value against the (already
validated) schema. It implements the same subset; it is a best-effort gate that
triggers one repair attempt, not a full JSON-Schema engine.
"""

from __future__ import annotations

import json
from typing import Any

from ..config import Settings
from ..errors import invalid_request, request_too_large

_ALLOWED_KEYS = frozenset(
    {
        "type", "properties", "required", "items", "enum", "const",
        "additionalProperties", "anyOf", "oneOf", "allOf",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "minLength", "maxLength", "minItems", "maxItems", "uniqueItems",
    }
)
_ALLOWED_TYPES = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)

# Appended to the "key not allowed" error so the rejection is self-correcting. The
# usual cause is a caller passing an EXAMPLE of the value (`{"answers":[{"fact":
# "..."}]}`) instead of a JSON Schema — the keys of a value example are arbitrary
# field names, none of which are schema keywords, so the first key trips the
# allow-list. The error is recoverable (the caller retries), so spelling out the
# required shape lets the model fix it on the next attempt rather than renaming
# keys blindly (which is exactly what stranded a real cohort extract: facts →
# <terminalId> → agent, three rejections, then giving up on the structured call).
_SCHEMA_HINT = (
    "result_schema must be a JSON Schema object using only these keywords: "
    + ", ".join(sorted(_ALLOWED_KEYS))
    + '. It looks like you passed an example value instead of a schema; put your '
    'fields under "properties". For one entry per terminal: '
    '{"type":"object","properties":{"answers":{"type":"array","items":'
    '{"type":"object","properties":{"terminalId":{"type":"string"},'
    '"fact":{"type":"string"}},"required":["terminalId","fact"]}}},'
    '"required":["answers"]}.'
)


def validate_result_schema(schema: Any, settings: Settings) -> None:
    """Reject a malformed/oversized/instruction-bearing ``result_schema``."""
    serialized = json.dumps(schema, separators=(",", ":"), default=str)
    if len(serialized.encode("utf-8")) > settings.max_result_schema_bytes:
        raise request_too_large("result_schema exceeds the size limit.", param="result_schema")
    _walk(schema, settings=settings, param="result_schema", depth=0, counter=[0])


def _walk(node: Any, *, settings: Settings, param: str, depth: int, counter: list[int]) -> None:
    if depth > settings.max_result_schema_depth:
        raise request_too_large("result_schema is too deeply nested.", param=param)
    if not isinstance(node, dict):
        raise invalid_request(
            f"result_schema at {param} must be an object.",
            param=param,
            code="invalid_result_schema",
        )
    for key in node:
        if key not in _ALLOWED_KEYS:
            raise invalid_request(
                f"result_schema key {key!r} is not allowed. {_SCHEMA_HINT}",
                param=f"{param}.{key}",
                code="invalid_result_schema",
            )

    # Each allow-listed key must also carry a value of the expected type — so an
    # arbitrary string can't ride into the prompt under a permitted key (e.g.
    # additionalProperties: "<instructions>").
    _validate_constraint_values(node, param)

    def child(sub: Any, sub_param: str) -> None:
        _walk(sub, settings=settings, param=sub_param, depth=depth + 1, counter=counter)

    declared = node.get("type")
    if declared is not None:
        for one in declared if isinstance(declared, list) else [declared]:
            if one not in _ALLOWED_TYPES:
                raise invalid_request(
                    f"result_schema type {one!r} is not allowed.",
                    param=f"{param}.type",
                    code="invalid_result_schema",
                )

    enum = node.get("enum")
    if enum is not None:
        if not isinstance(enum, list):
            raise invalid_request(
                "result_schema enum must be a list.",
                param=f"{param}.enum",
                code="invalid_result_schema",
            )
        if len(enum) > settings.max_result_schema_enum:
            raise request_too_large("result_schema enum is too large.", param=f"{param}.enum")
        for v in enum:
            _bound_string(v, settings=settings, param=f"{param}.enum")

    _bound_string(node.get("const"), settings=settings, param=f"{param}.const")

    required = node.get("required")
    if required is not None and (
        not isinstance(required, list) or not all(isinstance(x, str) for x in required)
    ):
        raise invalid_request(
            "result_schema required must be a list of strings.",
            param=f"{param}.required",
            code="invalid_result_schema",
        )

    props = node.get("properties")
    if props is not None:
        if not isinstance(props, dict):
            raise invalid_request(
                "result_schema properties must be an object.",
                param=f"{param}.properties",
                code="invalid_result_schema",
            )
        counter[0] += len(props)
        if counter[0] > settings.max_result_schema_properties:
            raise request_too_large("result_schema declares too many properties.", param=param)
        for key, sub in props.items():
            child(sub, f"{param}.properties.{key}")

    items = node.get("items")
    if items is not None:
        for i, it in enumerate(items if isinstance(items, list) else [items]):
            child(it, f"{param}.items[{i}]")

    addl = node.get("additionalProperties")
    if isinstance(addl, dict):
        child(addl, f"{param}.additionalProperties")

    for combiner in ("anyOf", "oneOf", "allOf"):
        sub = node.get(combiner)
        if sub is not None:
            if not isinstance(sub, list):
                raise invalid_request(
                    f"result_schema {combiner} must be a list.",
                    param=f"{param}.{combiner}",
                    code="invalid_result_schema",
                )
            for i, s in enumerate(sub):
                child(s, f"{param}.{combiner}[{i}]")


def _bound_string(value: Any, *, settings: Settings, param: str) -> None:
    if isinstance(value, str) and len(value.encode("utf-8")) > settings.max_result_schema_string:
        raise request_too_large("A string in result_schema exceeds the size limit.", param=param)


_NUMERIC_KEYS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum")
_NONNEG_INT_KEYS = ("minLength", "maxLength", "minItems", "maxItems")


def _bad(param: str, key: str, expected: str) -> None:
    raise invalid_request(
        f"result_schema {param}.{key} must be {expected}.",
        param=f"{param}.{key}",
        code="invalid_result_schema",
    )


def _validate_constraint_values(node: dict[str, Any], param: str) -> None:
    for key in _NUMERIC_KEYS:
        if key in node and (isinstance(node[key], bool) or not isinstance(node[key], (int, float))):
            _bad(param, key, "a number")
    for key in _NONNEG_INT_KEYS:
        value = node.get(key)
        if key in node and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            _bad(param, key, "a non-negative integer")
    if "uniqueItems" in node and not isinstance(node["uniqueItems"], bool):
        _bad(param, "uniqueItems", "a boolean")
    addl = node.get("additionalProperties")
    if "additionalProperties" in node and not isinstance(addl, (bool, dict)):
        _bad(param, "additionalProperties", "a boolean or an object")


# --------------------------------------------------------------------------
# Output conformance (the same subset, applied to a value).
# --------------------------------------------------------------------------


def _type_ok(value: Any, declared: str) -> bool:
    if declared == "object":
        return isinstance(value, dict)
    if declared == "array":
        return isinstance(value, list)
    if declared == "string":
        return isinstance(value, str)
    if declared == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "null":
        return value is None
    return True


def conforms(value: Any, schema: Any) -> bool:
    """Best-effort check that ``value`` satisfies the validated ``schema``."""
    if not isinstance(schema, dict):
        return True
    if "const" in schema:
        return bool(value == schema["const"])
    if "enum" in schema:
        return bool(value in schema["enum"])
    if "anyOf" in schema:
        return any(conforms(value, s) for s in schema["anyOf"])
    if "oneOf" in schema:
        return sum(1 for s in schema["oneOf"] if conforms(value, s)) == 1
    if "allOf" in schema:
        return all(conforms(value, s) for s in schema["allOf"])

    declared = schema.get("type")
    if declared is not None:
        types = declared if isinstance(declared, list) else [declared]
        if not any(_type_ok(value, one) for one in types):
            return False
    elif ("properties" in schema or "required" in schema) and not isinstance(value, dict):
        # A schema that declares object structure (properties/required) but omits
        # an explicit `type` still INTENDS an object — models routinely drop the
        # top-level `type:"object"`. Plain JSON-Schema treats required/properties
        # as no-ops on a non-object, which would let a bare array slip past this
        # gate with attribution lost (exactly the cohort-extract miss that surfaced
        # 4 bare strings instead of one {terminalId,…} object per agent). Treat
        # declared object structure as implying object type so the repair fires.
        return False

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return False
        if "maximum" in schema and value > schema["maximum"]:
            return False
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            return False
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            return False

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return False
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return False

    if isinstance(value, dict) and (schema.get("type") == "object" or "properties" in schema):
        props = schema.get("properties") or {}
        for req in schema.get("required") or []:
            if req not in value:
                return False
        addl = schema.get("additionalProperties", True)
        for key, sub in value.items():
            if key in props:
                if not conforms(sub, props[key]):
                    return False
            elif addl is False or isinstance(addl, dict) and not conforms(sub, addl):
                return False

    if isinstance(value, list) and (schema.get("type") == "array" or "items" in schema):
        if "minItems" in schema and len(value) < schema["minItems"]:
            return False
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return False
        if schema.get("uniqueItems"):
            seen: list[Any] = []
            for v in value:
                if v in seen:
                    return False
                seen.append(v)
        items = schema.get("items")
        if isinstance(items, dict):
            if not all(conforms(v, items) for v in value):
                return False
        elif isinstance(items, list):
            for v, sub in zip(value, items, strict=False):
                if not conforms(v, sub):
                    return False

    return True
