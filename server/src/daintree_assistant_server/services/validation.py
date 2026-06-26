"""Public Chat Completions request validation (specification section 6 & 9.5).

These checks run before orchestration and produce precise OpenAI-style errors:
no client system authority, no reserved tools, no silent semantic parameter
changes, and bounded context.
"""

from __future__ import annotations

import re

from ..config import Settings
from ..contracts.openai import (
    ChatCompletionRequest,
    ImageContentPart,
    NamedToolChoice,
    TextContentPart,
)
from ..errors import (
    invalid_request,
    model_not_found,
    request_too_large,
    reserved_tool_name,
    system_messages_not_allowed,
    unsupported_parameter,
)

ALLOWED_ROLES = frozenset({"user", "assistant", "tool"})
FORBIDDEN_ROLES = frozenset({"system", "developer"})

RESERVED_TOOL_NAMES = frozenset({"skill.find", "skill.load", "skill__find", "skill__load"})
RESERVED_TOOL_PREFIX = "daintree_internal__"

# Parameters that would silently change generation semantics; reject them.
UNSUPPORTED_PARAMS = frozenset(
    {
        "top_p",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "seed",
        "functions",
        "function_call",
        "parallel_tool_calls",
    }
)

# OpenAI/DeepSeek function names are limited to 64 characters.
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Fields that would smuggle in a free-form system prompt or message stream.
FORBIDDEN_TOP_LEVEL_FIELDS = frozenset(
    {"system_prompt", "system", "developer", "instructions"}
)
FORBIDDEN_NESTED_KEYS = FORBIDDEN_TOP_LEVEL_FIELDS | {"messages"}

# Maximum nesting depth scanned; deeper structures fail closed (rejected).
MAX_SCAN_DEPTH = 32


def reject_forbidden_prompt_keys(data: object, *, param: str, _depth: int = 0) -> None:
    """Recursively reject free-form prompt keys in an untrusted structure.

    Used at the Daintree extension and task-input boundaries so a client cannot
    smuggle ``system``/``system_prompt``/``developer``/``instructions``/
    ``messages`` past the server-owned prompt model — at any nesting depth, and
    inside lists. Over-deep structures are rejected (fail closed) rather than
    skipped, so the scan can never be bypassed by burying a key.
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


def _validate_roles(req: ChatCompletionRequest) -> None:
    for i, msg in enumerate(req.messages):
        if msg.role in FORBIDDEN_ROLES:
            raise system_messages_not_allowed(param=f"messages[{i}].role")
        if msg.role not in ALLOWED_ROLES:
            raise invalid_request(
                f"Unsupported message role {msg.role!r}.",
                param=f"messages[{i}].role",
                code="unsupported_role",
            )
        # Reserved tool names must not be smuggled in via prior assistant
        # tool calls; the server-generated synthetic load events are added
        # after validation and never appear in client-supplied messages.
        for j, call in enumerate(msg.tool_calls or []):
            if _is_reserved(call.function.name):
                raise reserved_tool_name(
                    call.function.name,
                    param=f"messages[{i}].tool_calls[{j}].function.name",
                )


def _validate_tools(req: ChatCompletionRequest, settings: Settings) -> None:
    tools = req.tools or []
    if len(tools) > settings.max_tools:
        raise request_too_large(
            f"Too many tools: {len(tools)} > {settings.max_tools}.", param="tools"
        )
    seen: set[str] = set()
    offered: set[str] = set()
    for i, tool in enumerate(tools):
        name = tool.function.name
        if _is_reserved(name):
            raise reserved_tool_name(name, param=f"tools[{i}].function.name")
        if not TOOL_NAME_RE.match(name):
            raise invalid_request(
                f"Invalid tool name {name!r}.",
                param=f"tools[{i}].function.name",
                code="invalid_tool_name",
            )
        if name in seen:
            raise invalid_request(
                f"Duplicate tool name {name!r}.",
                param=f"tools[{i}].function.name",
                code="duplicate_tool_name",
            )
        seen.add(name)
        offered.add(name)

    choice = req.tool_choice
    if isinstance(choice, NamedToolChoice):
        chosen = choice.function.name
        if _is_reserved(chosen):
            raise reserved_tool_name(chosen, param="tool_choice.function.name")
        if chosen not in offered:
            raise invalid_request(
                f"tool_choice references tool {chosen!r} which is not offered.",
                param="tool_choice.function.name",
                code="tool_choice_not_offered",
            )


def _validate_parameters(req: ChatCompletionRequest, settings: Settings) -> None:
    if req.max_tokens is not None and req.max_completion_tokens is not None:
        raise invalid_request(
            "Provide only one of max_tokens or max_completion_tokens.",
            param="max_tokens",
            code="conflicting_parameters",
        )
    extra = req.model_extra or {}
    for key in extra:
        if key in FORBIDDEN_TOP_LEVEL_FIELDS:
            raise invalid_request(
                f"Field {key!r} is not accepted; Daintree system instructions are "
                "server-owned.",
                param=key,
                code="system_prompt_not_allowed",
            )
        if key in UNSUPPORTED_PARAMS:
            raise unsupported_parameter(key)
        if key == "n" and extra[key] not in (None, 1):
            raise unsupported_parameter("n", "Only n=1 is supported.")


def _validate_content(req: ChatCompletionRequest, settings: Settings) -> None:
    for i, msg in enumerate(req.messages):
        if not isinstance(msg.content, list):
            continue
        for j, part in enumerate(msg.content):
            if isinstance(part, TextContentPart):
                if len(part.text.encode("utf-8")) > settings.cap_message_text_part:
                    raise request_too_large(
                        "A message text part exceeds the size limit.",
                        param=f"messages[{i}].content[{j}].text",
                    )
            elif isinstance(part, ImageContentPart):
                url = part.image_url.url
                if not settings.allow_remote_image_urls and not url.startswith("data:"):
                    raise invalid_request(
                        "Remote image URLs are not allowed; supply a data URL.",
                        param=f"messages[{i}].content[{j}].image_url.url",
                        code="remote_image_not_allowed",
                    )


def validate_model(req: ChatCompletionRequest, settings: Settings) -> str:
    """Validate the requested model and return the upstream model to use."""
    allowed = {settings.public_model_id, settings.deepseek_public_alias}
    if req.model not in allowed:
        raise model_not_found(req.model)
    return settings.deepseek_main_model


def validate_chat_request(req: ChatCompletionRequest, settings: Settings) -> str:
    """Run all validation. Returns the resolved upstream model name."""
    _validate_roles(req)
    _validate_parameters(req, settings)
    _validate_tools(req, settings)
    _validate_content(req, settings)
    return validate_model(req, settings)
