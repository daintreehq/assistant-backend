"""Native respond-request validation.

The native request is strictly typed (``extra="forbid"`` throughout), so most
shape policing the OpenAI-compatible surface needed is now free at the Pydantic
layer. What remains are the semantic rules: no client system authority, no
reserved tool names, bounded tool schemas, and bounded message content. The
free-form-key scanner is retained for the task-input boundary.
"""

from __future__ import annotations

import json
import re

from ..config import DEEPSEEK_MODEL, Settings
from ..contracts.daintree_api import RespondRequest, RespondToolChoice
from ..contracts.openai import (
    ChatMessage,
    ImageContentPart,
    TextContentPart,
    ToolDef,
)
from ..errors import (
    invalid_request,
    request_too_large,
    reserved_tool_name,
    system_messages_not_allowed,
)

ALLOWED_ROLES = frozenset({"user", "assistant", "tool"})
FORBIDDEN_ROLES = frozenset({"system", "developer"})

RESERVED_TOOL_NAMES = frozenset({"skill.find", "skill.load", "skill__find", "skill__load"})
RESERVED_TOOL_PREFIX = "daintree_internal__"

# OpenAI/DeepSeek function names are limited to 64 characters.
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Fields that would smuggle in a free-form system prompt or message stream at the
# task-input boundary (the native respond request cannot carry these at all).
FORBIDDEN_TOP_LEVEL_FIELDS = frozenset({"system_prompt", "system", "developer", "instructions"})
FORBIDDEN_NESTED_KEYS = FORBIDDEN_TOP_LEVEL_FIELDS | {"messages"}

# Maximum nesting depth scanned; deeper structures fail closed (rejected).
MAX_SCAN_DEPTH = 32


def reject_forbidden_prompt_keys(data: object, *, param: str, _depth: int = 0) -> None:
    """Recursively reject free-form prompt keys in an untrusted structure.

    Used at the task-input boundary so a client cannot smuggle ``system``/
    ``system_prompt``/``developer``/``instructions``/``messages`` past the
    server-owned prompt model — at any nesting depth, and inside lists.
    Over-deep structures are rejected (fail closed) rather than skipped.
    """
    if _depth > MAX_SCAN_DEPTH:
        raise invalid_request(
            "Request structure is too deeply nested.",
            param=param,
            code="request_too_nested",
        )
    if isinstance(data, dict):
        for key, value in data.items():
            if key in FORBIDDEN_NESTED_KEYS:
                raise invalid_request(
                    f"Field {param}.{key!r} is not accepted; Daintree system "
                    "instructions are server-owned.",
                    param=f"{param}.{key}",
                    code="system_prompt_not_allowed",
                )
            reject_forbidden_prompt_keys(value, param=f"{param}.{key}", _depth=_depth + 1)
    elif isinstance(data, (list, tuple)):
        for i, item in enumerate(data):
            reject_forbidden_prompt_keys(item, param=f"{param}[{i}]", _depth=_depth + 1)


def _is_reserved(name: str) -> bool:
    return name in RESERVED_TOOL_NAMES or name.startswith(RESERVED_TOOL_PREFIX)


def _validate_roles(messages: list[ChatMessage]) -> None:
    for i, msg in enumerate(messages):
        if msg.role in FORBIDDEN_ROLES:
            raise system_messages_not_allowed(param=f"input.messages[{i}].role")
        if msg.role not in ALLOWED_ROLES:
            raise invalid_request(
                f"Unsupported message role {msg.role!r}.",
                param=f"input.messages[{i}].role",
                code="unsupported_role",
            )
        # Reserved tool names must not be smuggled in via prior assistant tool
        # calls; the server-generated synthetic load events are added after
        # validation and never appear in client-supplied messages.
        for j, call in enumerate(msg.tool_calls or []):
            if _is_reserved(call.function.name):
                raise reserved_tool_name(
                    call.function.name,
                    param=f"input.messages[{i}].tool_calls[{j}].function.name",
                )


def _walk_tool_schema(
    node: object, *, settings: Settings, param: str, depth: int, counter: list[int]
) -> None:
    """Recursively bound a tool's JSON-Schema: depth, property count, strings."""
    if depth > settings.max_tool_schema_depth:
        raise request_too_large(f"Tool schema {param} is too deeply nested.", param=param)
    if isinstance(node, dict):
        props = node.get("properties")
        if props is not None:
            if not isinstance(props, dict):
                raise invalid_request(
                    f"Tool schema {param}.properties must be an object.",
                    param=f"{param}.properties",
                    code="invalid_tool_schema",
                )
            counter[0] += len(props)
            if counter[0] > settings.max_tool_schema_properties:
                raise request_too_large(
                    "Tool schema declares too many properties.", param=param
                )
        required = node.get("required")
        if required is not None and (
            not isinstance(required, list) or not all(isinstance(x, str) for x in required)
        ):
            raise invalid_request(
                f"Tool schema {param}.required must be a list of strings.",
                param=f"{param}.required",
                code="invalid_tool_schema",
            )
        for key, value in node.items():
            _walk_tool_schema(
                value, settings=settings, param=f"{param}.{key}", depth=depth + 1, counter=counter
            )
    elif isinstance(node, (list, tuple)):
        for idx, item in enumerate(node):
            _walk_tool_schema(
                item, settings=settings, param=f"{param}[{idx}]", depth=depth + 1, counter=counter
            )
    elif isinstance(node, str):
        if len(node.encode("utf-8")) > settings.max_tool_schema_string_bytes:
            raise request_too_large(
                f"A string in tool schema {param} exceeds the size limit.", param=param
            )


def _validate_tool_schema(parameters: object, *, settings: Settings, param: str) -> None:
    if parameters is None:
        return
    if not isinstance(parameters, dict):
        raise invalid_request(
            f"Tool parameters {param} must be a JSON object schema.",
            param=param,
            code="invalid_tool_schema",
        )
    schema_type = parameters.get("type")
    if schema_type is not None and schema_type != "object":
        raise invalid_request(
            f"Tool parameters {param} root schema must be type 'object'.",
            param=f"{param}.type",
            code="invalid_tool_schema",
        )
    _walk_tool_schema(parameters, settings=settings, param=param, depth=0, counter=[0])


def _validate_tools(
    tools: list[ToolDef] | None,
    tool_choice: RespondToolChoice | str | None,
    settings: Settings,
) -> None:
    tools = tools or []
    if len(tools) > settings.max_tools:
        raise request_too_large(
            f"Too many tools: {len(tools)} > {settings.max_tools}.", param="input.tools"
        )

    total_bytes = 0
    seen: set[str] = set()
    offered: set[str] = set()
    for i, tool in enumerate(tools):
        name = tool.function.name
        if _is_reserved(name):
            raise reserved_tool_name(name, param=f"input.tools[{i}].function.name")
        if not TOOL_NAME_RE.match(name):
            raise invalid_request(
                f"Invalid tool name {name!r}.",
                param=f"input.tools[{i}].function.name",
                code="invalid_tool_name",
            )
        if name in seen:
            raise invalid_request(
                f"Duplicate tool name {name!r}.",
                param=f"input.tools[{i}].function.name",
                code="duplicate_tool_name",
            )
        seen.add(name)
        offered.add(name)

        _validate_tool_schema(
            tool.function.parameters,
            settings=settings,
            param=f"input.tools[{i}].function.parameters",
        )
        total_bytes += len(
            json.dumps(tool.model_dump(), separators=(",", ":"), default=str).encode("utf-8")
        )
        if total_bytes > settings.max_tool_definition_bytes:
            raise request_too_large(
                "Tool definitions exceed the total size limit.", param="input.tools"
            )

    if isinstance(tool_choice, RespondToolChoice):
        chosen = tool_choice.name
        if _is_reserved(chosen):
            raise reserved_tool_name(chosen, param="input.tool_choice.name")
        if chosen not in offered:
            raise invalid_request(
                f"tool_choice references tool {chosen!r} which is not offered.",
                param="input.tool_choice.name",
                code="tool_choice_not_offered",
            )


def _validate_message_sequence(messages: list[ChatMessage]) -> None:
    """OpenAI-style sequencing checks, so a malformed transcript fails fast with a
    clear 400 instead of a confusing upstream 502 (and a wasted upstream call)."""
    opened: set[str] = set()  # tool_call ids introduced by earlier assistant messages
    for i, msg in enumerate(messages):
        if msg.role == "tool":
            tool_call_id = (msg.tool_call_id or "").strip()
            if not tool_call_id:
                raise invalid_request(
                    "A tool message requires a tool_call_id.",
                    param=f"input.messages[{i}].tool_call_id",
                    code="missing_tool_call_id",
                )
            if tool_call_id not in opened:
                raise invalid_request(
                    "A tool message must follow an assistant tool call with the same id.",
                    param=f"input.messages[{i}].tool_call_id",
                    code="dangling_tool_message",
                )
        ids_in_msg: set[str] = set()
        for j, call in enumerate(msg.tool_calls or []):
            if call.id in ids_in_msg:
                raise invalid_request(
                    f"Duplicate tool_call id {call.id!r} within a single message.",
                    param=f"input.messages[{i}].tool_calls[{j}].id",
                    code="duplicate_tool_call_id",
                )
            ids_in_msg.add(call.id)
            opened.add(call.id)


def _validate_content(messages: list[ChatMessage], settings: Settings) -> None:
    for i, msg in enumerate(messages):
        if isinstance(msg.content, str):
            if len(msg.content.encode("utf-8")) > settings.cap_message_text:
                raise request_too_large(
                    "A message's text content exceeds the size limit.",
                    param=f"input.messages[{i}].content",
                )
            continue
        if not isinstance(msg.content, list):
            continue
        for j, part in enumerate(msg.content):
            if isinstance(part, TextContentPart):
                if len(part.text.encode("utf-8")) > settings.cap_message_text_part:
                    raise request_too_large(
                        "A message text part exceeds the size limit.",
                        param=f"input.messages[{i}].content[{j}].text",
                    )
            elif isinstance(part, ImageContentPart):
                url = part.image_url.url
                if not settings.allow_remote_image_urls and not url.startswith("data:"):
                    raise invalid_request(
                        "Remote image URLs are not allowed; supply a data URL.",
                        param=f"input.messages[{i}].content[{j}].image_url.url",
                        code="remote_image_not_allowed",
                    )


def _validate_generation(req: RespondRequest) -> None:
    """DeepSeek rejects JSON mode combined with function calling, so fail fast with
    a clear 400 instead of a confusing upstream 502 (and a wasted upstream call).
    ("text" is the default output mode and is simply not forwarded — see the
    assembler — so only "json_object" conflicts with tools.)"""
    if req.generation.response_format == "json_object" and req.input.tools:
        raise invalid_request(
            "generation.response_format 'json_object' cannot be combined with input.tools.",
            param="generation.response_format",
            code="response_format_with_tools",
        )


def validate_respond_request(req: RespondRequest, settings: Settings) -> str:
    """Run all semantic validation. Returns the (hardcoded) upstream model name."""
    _validate_roles(req.input.messages)
    _validate_message_sequence(req.input.messages)
    _validate_tools(req.input.tools, req.input.tool_choice, settings)
    _validate_content(req.input.messages, settings)
    _validate_generation(req)
    return DEEPSEEK_MODEL


def validate_turn_submission(
    messages: list[ChatMessage],
    tools: list[ToolDef] | None,
    settings: Settings,
) -> None:
    """Semantic validation for a v2 run-turn submission.

    Stricter than v1: the client submits only NEW user messages — assistant
    and tool messages are server-recorded facts in the durable log, and system
    authority stays server-owned. Tool definitions get the same reserved-name
    and schema bounds as v1.
    """
    for i, msg in enumerate(messages):
        if msg.role in FORBIDDEN_ROLES:
            raise system_messages_not_allowed(param=f"turn.messages[{i}].role")
        if msg.role != "user":
            raise invalid_request(
                f"Only 'user' messages may be submitted to a run turn, got {msg.role!r}.",
                param=f"turn.messages[{i}].role",
                code="unsupported_role",
            )
        if msg.tool_calls:
            raise invalid_request(
                "A submitted turn message cannot carry tool_calls.",
                param=f"turn.messages[{i}].tool_calls",
                code="invalid_request",
            )
    _validate_tools(tools, None, settings)
    _validate_content(messages, settings)
