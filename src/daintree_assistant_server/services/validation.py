"""Native respond-request validation.

The native request is strictly typed (``extra="forbid"`` throughout), so most
shape policing the OpenAI-compatible surface needed is now free at the Pydantic
layer. What remains are the semantic rules: no client system authority, no
reserved tool names, bounded tool schemas, and bounded message content. The
free-form-key scanner is retained for the task-input boundary.
"""

from __future__ import annotations

import base64
import binascii
import json
import re

from ..config import Settings
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
from ..text_utils import has_lone_surrogate, utf8_len

ALLOWED_ROLES = frozenset({"user", "assistant", "tool"})
FORBIDDEN_ROLES = frozenset({"system", "developer"})

RESERVED_TOOL_NAMES = frozenset({"skill.find", "skill.load", "skill__find", "skill__load"})
RESERVED_TOOL_PREFIX = "daintree_internal__"

# OpenAI-wire function names are limited to 64 characters.
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
        if utf8_len(node) > settings.max_tool_schema_string_bytes:
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
    """Walk the transcript as a state machine, so a malformed one fails fast with
    a clear 400 instead of a confusing upstream 502 (and a wasted upstream call).

    The rule modelled here is the canonical tool-calling loop: an assistant turn
    opens a BATCH of calls, every call in that batch is answered exactly once,
    and only then may the conversation move on. Anything else is a client bug —
    and one that this server, not just the upstream, is damaged by.

    That last part is why the check is a state machine rather than the membership
    test it used to be. `skills/tool_projection._unresolved_tool_calls` builds
    `{call_id: tool_name}` to decide which tools a continuation round still needs
    offered. Under the old rule a later assistant message could reuse a call id
    that was still unanswered, and that dict silently OVERWRITES the first entry
    — so under enforcement the projection withholds the tool the pending call
    needs, and the model is left holding a call it has no legal way to make. A
    permissive validator was therefore not merely untidy; it was the precondition
    for a wrong tool inventory.

    Deliberately NOT enforced: global uniqueness of call ids across the whole
    transcript. Ids are minted by the MODEL, and one that reuses an id in a later
    turn has produced a perfectly coherent conversation — every call answered
    before the next began — that a global `seen` set would 400. Uniqueness is
    only load-bearing while a call is OPEN, which is exactly the window the
    pending map covers.

    Ids are compared VERBATIM, and one carrying surrounding whitespace is
    rejected outright. They travel upstream raw
    (`prompt_assembler._tool_call_to_wire`), so pairing on a stripped value would
    validate ` c1 ` against `c1` here and hand the provider a call nothing
    answers — the validator agreeing with itself and with nothing else.

    Order WITHIN a batch is not constrained: the CLI dispatches a parallel group
    concurrently and appends results as they settle, so requiring call order
    would reject its normal output. Every call answered before the batch closes
    is the invariant; the sequence they are answered in is not.
    """
    pending: dict[str, int] = {}  # open call id -> index of the message that opened it

    def _reject_unresolved(index: int, role: str) -> None:
        call_id, opened_at = next(iter(pending.items()))
        raise invalid_request(
            f"A {role} message arrives while tool call {call_id!r} (opened by "
            f"input.messages[{opened_at}]) has no result.",
            param=f"input.messages[{index}]",
            code="unresolved_tool_calls",
        )

    for i, msg in enumerate(messages):
        if msg.role == "tool":
            tool_call_id = msg.tool_call_id or ""
            if not tool_call_id.strip():
                raise invalid_request(
                    "A tool message requires a tool_call_id.",
                    param=f"input.messages[{i}].tool_call_id",
                    code="missing_tool_call_id",
                )
            # Symmetrical with the call side: this id is forwarded raw too, so
            # pairing on a stripped value here would hide the same mismatch.
            if tool_call_id != tool_call_id.strip():
                raise invalid_request(
                    "A tool_call_id must not be surrounded by whitespace.",
                    param=f"input.messages[{i}].tool_call_id",
                    code="malformed_tool_call_id",
                )
            if tool_call_id not in pending:
                # One code for three shapes — an id no assistant message ever
                # opened, a second result for a call already answered, and a
                # result for a call closed by an earlier batch — because from the
                # client's side they are the same bug (a reply with no open call)
                # and the message names which one it was.
                raise invalid_request(
                    "A tool message must answer an open tool call from the "
                    "immediately preceding assistant message, exactly once.",
                    param=f"input.messages[{i}].tool_call_id",
                    code="dangling_tool_message",
                )
            del pending[tool_call_id]
            continue

        # A non-tool message closes the batch, so the batch must BE closed. The
        # check runs before this message's own calls are recorded, or an
        # assistant turn would be judged against the calls it is making now.
        if pending:
            _reject_unresolved(i, msg.role)

        calls = msg.tool_calls or []
        # Only an ASSISTANT turn may open calls. The schema allows `tool_calls` on
        # any role (`role: str`, so that `system`/`developer` can be rejected with
        # a precise code), and the assembler forwards the field for every non-tool
        # role — so a user message carrying `tool_calls` reaches the provider as an
        # invalid transcript and comes back a 400 naming nothing we can act on.
        if calls and msg.role != "assistant":
            raise invalid_request(
                f"Only an assistant message may carry tool_calls; this one is {msg.role!r}.",
                param=f"input.messages[{i}].tool_calls",
                code="tool_calls_on_non_assistant_message",
            )

        for j, call in enumerate(calls):
            # Ids go upstream RAW (`prompt_assembler._tool_call_to_wire`), so an id
            # that is not already its own stripped form pairs here and mismatches
            # there: ` c1 ` opened against `c1` answered validates, then arrives at
            # the provider as a call nothing replies to. Rejecting is the only
            # reading that keeps this validator and the wire agreeing; stripping
            # would fix the pairing and leave the provider seeing two ids.
            if call.id != call.id.strip():
                raise invalid_request(
                    "A tool_call id must not be surrounded by whitespace.",
                    param=f"input.messages[{i}].tool_calls[{j}].id",
                    code="malformed_tool_call_id",
                )
            if call.id in pending:
                raise invalid_request(
                    f"Duplicate tool_call id {call.id!r} within a single message.",
                    param=f"input.messages[{i}].tool_calls[{j}].id",
                    code="duplicate_tool_call_id",
                )
            pending[call.id] = i


def _validate_shape(req: RespondRequest, settings: Settings) -> None:
    """Bound how MANY of each thing a request may carry.

    The 8 MiB body ceiling bounds total bytes and nothing else, so a request of
    a hundred thousand two-byte messages passes it and then costs CPU and
    allocations in every stage that follows. Each cap below is set far above what
    a healthy client sends (see ``config.py``): crossing one is evidence of a
    malformed request, not of an unusually busy session, so the honest answer is
    413 at the boundary rather than a slow turn nobody can explain.

    413 rather than 400 throughout: nothing here is the wrong SHAPE, there is
    simply too much of it — the same reading ``request_too_large`` already has.
    """
    messages = req.input.messages
    if len(messages) > settings.cap_messages:
        raise request_too_large(
            f"The conversation carries more than {settings.cap_messages} messages.",
            param="input.messages",
        )
    for i, msg in enumerate(messages):
        if isinstance(msg.content, list) and len(msg.content) > settings.cap_content_parts:
            raise request_too_large(
                f"A message carries more than {settings.cap_content_parts} content parts.",
                param=f"input.messages[{i}].content",
            )
        calls = msg.tool_calls or []
        if len(calls) > settings.cap_tool_calls_per_message:
            raise request_too_large(
                f"A message carries more than {settings.cap_tool_calls_per_message} tool calls.",
                param=f"input.messages[{i}].tool_calls",
            )

    stop = req.generation.stop
    sequences = [stop] if isinstance(stop, str) else (stop or [])
    if len(sequences) > settings.cap_stop_sequences:
        raise request_too_large(
            f"More than {settings.cap_stop_sequences} stop sequences were supplied.",
            param="generation.stop",
        )
    for i, sequence in enumerate(sequences):
        if utf8_len(sequence) > settings.cap_stop_sequence_bytes:
            raise request_too_large(
                "A stop sequence exceeds the size limit.",
                param=f"generation.stop[{i}]",
            )

    pinned = req.selection.pinned_skill_ids
    if len(pinned) > settings.cap_pinned_skill_ids:
        raise request_too_large(
            f"More than {settings.cap_pinned_skill_ids} skill ids were pinned.",
            param="selection.pinned_skill_ids",
        )


# Image formats the serving path will carry. An allowlist rather than a denylist
# because the interesting rejections are the ones nobody thought to name:
# `image/svg+xml` is a scripting format, and `text/html` in an `image_url` is a
# data URL that is not an image at all. Neither is a raster image the model can
# read, and both are shapes a validator that only checked `data:` would accept.
ALLOWED_IMAGE_MIME_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)

#: Case-insensitive, because the scheme is: `DATA:image/png;base64,...` is a
#: perfectly good data URL and a case-sensitive check would classify it as REMOTE
#: — harmless while remote URLs are off (it is refused), and a hole the moment an
#: operator turns them on, since the whole image policy would then be skipped.
_DATA_URL_SCHEME = "data:"

# `data:<mediatype>[;param=value]*[;base64],<data>` — the media type is optional
# in the RFC, so the group may be empty. Parameters are captured as a whole and
# split below rather than substring-matched: `;base64x` CONTAINS `;base64` and is
# not it.
_DATA_URL_RE = re.compile(r"^data:([^;,]*)((?:;[^;,]*)*),", re.IGNORECASE)


def _validate_image_data_url(url: str, *, param: str, settings: Settings) -> None:
    """Enforce a real image policy on a data URL, not merely the `data:` prefix.

    Until now any `data:` URL was accepted up to the body ceiling, so the request
    boundary knew nothing about the media type, whether the payload was valid
    base64, or how large it decoded to. Nothing currently SENDS images — the CLI
    has no production caller for its own image-part constructor — which is
    exactly why the policy lands now: writing it against a live producer means
    writing it around whatever that producer already does.

    Decoded size is bounded by bounding the ENCODED string first, so nothing
    oversized is ever decoded: base64 expands by 4/3, so an encoded cap is a
    decoded cap with no allocation required to discover it.
    """
    match = _DATA_URL_RE.match(url)
    if not match:
        raise invalid_request(
            "An image data URL is malformed.",
            param=param,
            code="invalid_image",
        )
    media_type = (match.group(1) or "").strip().lower()
    parameters = [p for p in (match.group(2) or "").lower().split(";") if p]
    if media_type not in ALLOWED_IMAGE_MIME_TYPES:
        raise invalid_request(
            f"Image media type {media_type or '(none)'!r} is not supported. "
            f"Supported: {', '.join(sorted(ALLOWED_IMAGE_MIME_TYPES))}.",
            param=param,
            code="invalid_image",
        )
    # An exact parameter, not a substring: `;base64x` contains `;base64`.
    if "base64" not in parameters:
        raise invalid_request(
            "An image data URL must be base64-encoded.",
            param=param,
            code="invalid_image",
        )
    payload = url[match.end() :]
    if not payload:
        # `data:image/png;base64,` decodes to zero bytes, which `b64decode`
        # accepts happily. An empty image is not an image.
        raise invalid_request(
            "An image data URL carries no data.",
            param=param,
            code="invalid_image",
        )
    # Bounded BEFORE decoding, which is the whole point of capping the encoded
    # form: base64 expands by 4/3, so this bounds the decoded image without
    # allocating it to find out.
    if len(payload) > settings.cap_image_encoded_bytes:
        raise request_too_large(
            "An image exceeds the size limit.",
            param=param,
        )
    # Validated, never decoded into a kept buffer: the bytes go upstream as the
    # data URL they arrived as, so all we owe the caller is an early, precise
    # rejection instead of a 400 from the provider naming nothing.
    try:
        base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        raise invalid_request(
            "An image data URL does not carry valid base64.",
            param=param,
            code="invalid_image",
        ) from None


def _validate_content(messages: list[ChatMessage], settings: Settings) -> None:
    # Counted across the WHOLE request, not per message: a hundred one-image
    # messages costs the prompt exactly what one hundred-image message does.
    images = 0
    for i, msg in enumerate(messages):
        if isinstance(msg.content, str):
            if has_lone_surrogate(msg.content):
                raise invalid_request(
                    "A message's text content is not valid UTF-8.",
                    param=f"input.messages[{i}].content",
                )
            if utf8_len(msg.content) > settings.cap_message_text:
                raise request_too_large(
                    "A message's text content exceeds the size limit.",
                    param=f"input.messages[{i}].content",
                )
            continue
        if not isinstance(msg.content, list):
            continue
        for j, part in enumerate(msg.content):
            if isinstance(part, TextContentPart):
                if has_lone_surrogate(part.text):
                    raise invalid_request(
                        "A message text part is not valid UTF-8.",
                        param=f"input.messages[{i}].content[{j}].text",
                    )
                if utf8_len(part.text) > settings.cap_message_text_part:
                    raise request_too_large(
                        "A message text part exceeds the size limit.",
                        param=f"input.messages[{i}].content[{j}].text",
                    )
            elif isinstance(part, ImageContentPart):
                url = part.image_url.url
                param = f"input.messages[{i}].content[{j}].image_url.url"
                # Counted FIRST, and for remote URLs too. The cap is about how
                # many images a prompt carries, which is true whether the bytes
                # ride the request or the provider fetches them — an early
                # `continue` past this line let the count be bypassed entirely by
                # sending remote URLs.
                images += 1
                if images > settings.cap_images:
                    raise request_too_large(
                        f"A request may carry at most {settings.cap_images} images.",
                        param=param,
                    )
                if not url.lower().startswith(_DATA_URL_SCHEME):
                    if not settings.allow_remote_image_urls:
                        raise invalid_request(
                            "Remote image URLs are not allowed; supply a data URL.",
                            param=param,
                            code="remote_image_not_allowed",
                        )
                    # A remote URL is only reachable when an operator turned that
                    # on, and the server never fetches it — the provider does. So
                    # there is nothing here to inspect and the data-URL policy
                    # below does not apply.
                    continue
                _validate_image_data_url(url, param=param, settings=settings)


def _validate_startup(req: RespondRequest, settings: Settings) -> None:
    instructions = req.startup.project_instructions
    if (
        instructions is not None
        and utf8_len(instructions) > settings.cap_project_instructions
    ):
        raise request_too_large(
            "Startup project instructions exceed the size limit.",
            param="startup.project_instructions",
        )


def _validate_generation(req: RespondRequest) -> None:
    """The model rejects JSON mode combined with function calling, so fail fast with
    a clear 400 instead of a confusing upstream 502 (and a wasted upstream call).
    ("text" is the default output mode and is simply not forwarded — see the
    assembler — so only "json_object" conflicts with tools.)"""
    if req.generation.response_format == "json_object" and req.input.tools:
        raise invalid_request(
            "generation.response_format 'json_object' cannot be combined with input.tools.",
            param="generation.response_format",
            code="response_format_with_tools",
        )


def validate_respond_request(req: RespondRequest, settings: Settings) -> None:
    """Run all semantic validation.

    Returns nothing. It used to return the one hardcoded ``SERVING_MODEL``, which
    made model resolution a side effect of validation — defensible while there
    was exactly one model, and wrong the moment a caller's MODE decides it.
    Resolution belongs at the route boundary, next to the entitlement check that
    guards it; validation is about whether the request is well-formed.
    """
    _validate_roles(req.input.messages)
    _validate_shape(req, settings)
    _validate_message_sequence(req.input.messages)
    _validate_tools(req.input.tools, req.input.tool_choice, settings)
    _validate_content(req.input.messages, settings)
    _validate_startup(req, settings)
    _validate_generation(req)
