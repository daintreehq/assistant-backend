"""Native respond-request validation tests."""

from __future__ import annotations

import pytest

from daintree_assistant_server.config import Settings
from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.services.validation import validate_respond_request


@pytest.fixture
def settings() -> Settings:
    return Settings(TOGETHER_API_KEY="sk", STATE_SIGNING_KEY_CURRENT="s")


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "TOGETHER_API_KEY": "sk", "STATE_SIGNING_KEY_CURRENT": "s",
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
    # Returns nothing: model resolution moved to the route boundary, where the
    # caller's mode and their entitlement to it are settled together. A
    # validator that also picked the model made that choice a side effect.
    assert validate_respond_request(_req(), settings) is None


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


# A real (tiny) base64 payload. The old fixture used "AA", which is not valid
# base64 at all — two characters cannot decode — and passed only because nothing
# checked. It is the exact shape the image policy exists to catch.
PNG_B64 = "iVBORw0KGgo="


def test_data_url_image_allowed(settings: Settings) -> None:
    req = _req(messages=[{
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG_B64}"}}],
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


# --------------------------------------------------------------------------
# The transcript state machine.
#
# The old rule was a membership test: a tool result was accepted whenever its id
# had appeared in ANY earlier assistant message. That let a transcript reuse an
# id while the first call was still open, and
# `skills/tool_projection._unresolved_tool_calls` keys a dict by exactly that id
# — so the second call OVERWROTE the first, and under enforcement the projection
# withheld the tool the pending call needed. These tests pin the invariant that
# makes that dict trustworthy: a call is open exactly once, and the batch closes
# before the conversation moves on.
# --------------------------------------------------------------------------


def _assistant(*ids: str) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": i, "type": "function", "function": {"name": "t", "arguments": "{}"}}
            for i in ids
        ],
    }


def _result(call_id: str) -> dict:
    return {"role": "tool", "content": "ok", "tool_call_id": call_id}


def test_a_reused_call_id_while_the_first_is_open_is_rejected(settings: Settings) -> None:
    """The bug this state machine exists for. Under the old rule this transcript
    validated, and the projection's `{id: name}` map silently lost the first
    call."""
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(
            _req(messages=[
                {"role": "user", "content": "go"},
                _assistant("c1"),
                _assistant("c1"),
            ]),
            settings,
        )
    assert exc.value.code == "unresolved_tool_calls"


def test_a_call_id_reused_after_its_result_is_accepted(settings: Settings) -> None:
    """Deliberately NOT rejected. Ids are minted by the MODEL, and one that picks
    the same id in a later turn — every call answered before the next began — has
    produced a coherent conversation. Global uniqueness would 400 it for no gain:
    the id is only load-bearing while the call is OPEN."""
    validate_respond_request(
        _req(messages=[
            {"role": "user", "content": "go"},
            _assistant("c1"),
            _result("c1"),
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "again"},
            _assistant("c1"),
            _result("c1"),
        ]),
        settings,
    )


def test_two_results_for_one_call_are_rejected(settings: Settings) -> None:
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(
            _req(messages=[
                {"role": "user", "content": "go"},
                _assistant("c1"),
                _result("c1"),
                _result("c1"),
            ]),
            settings,
        )
    assert exc.value.code == "dangling_tool_message"


def test_a_user_message_before_the_batch_resolves_is_rejected(settings: Settings) -> None:
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(
            _req(messages=[
                {"role": "user", "content": "go"},
                _assistant("c1", "c2"),
                _result("c1"),
                {"role": "user", "content": "actually, stop"},
            ]),
            settings,
        )
    assert exc.value.code == "unresolved_tool_calls"
    # The message names the call that was left open, not merely that one was.
    assert "'c2'" in exc.value.message


def test_results_may_settle_in_any_order_within_a_batch(settings: Settings) -> None:
    """The CLI dispatches a parallel group concurrently and appends each result as
    it settles, so requiring call order would reject its normal output."""
    validate_respond_request(
        _req(messages=[
            {"role": "user", "content": "go"},
            _assistant("c1", "c2", "c3"),
            _result("c3"),
            _result("c1"),
            _result("c2"),
            {"role": "assistant", "content": "done"},
        ]),
        settings,
    )


def test_a_trailing_unresolved_batch_is_still_accepted(settings: Settings) -> None:
    """The one end state that stays legal. `tool_projection._unresolved_tool_calls`
    exists to keep those tools offered on the next round, and with the rule above
    an unresolved call can now only ever be the LAST batch — which is precisely
    the input that function was written for."""
    validate_respond_request(
        _req(messages=[
            {"role": "user", "content": "go"},
            _assistant("c1"),
        ]),
        settings,
    )


@pytest.mark.parametrize("padded_call, padded_result", [(True, False), (False, True)])
def test_a_padded_tool_call_id_is_rejected_rather_than_stripped(
    padded_call: bool, padded_result: bool, settings: Settings
) -> None:
    """Stripping would have made the validator agree with itself and with nothing
    else.

    Ids travel upstream RAW (`prompt_assembler._tool_call_to_wire` and the tool
    branch of `_message_to_wire`), so pairing ' c1 ' against 'c1' here validates a
    transcript the provider then sees as a call nothing answers — a 400 from
    upstream naming a field we could have named ourselves. Rejecting is the only
    reading that keeps this check and the wire describing the same conversation.
    """
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(
            _req(messages=[
                {"role": "user", "content": "go"},
                _assistant(" c1 " if padded_call else "c1"),
                _result(" c1 " if padded_result else "c1"),
            ]),
            settings,
        )
    assert exc.value.code in {"malformed_tool_call_id", "dangling_tool_message"}


def test_only_an_assistant_message_may_carry_tool_calls(settings: Settings) -> None:
    """The schema types `role` as a bare string so `system`/`developer` can be
    rejected with a precise code, which also means `tool_calls` is structurally
    legal on a user message — and the assembler forwards the field for every
    non-tool role, so it would reach the provider as an invalid transcript."""
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(
            _req(messages=[{
                "role": "user",
                "content": "go",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
                ],
            }]),
            settings,
        )
    assert exc.value.code == "tool_calls_on_non_assistant_message"


# --------------------------------------------------------------------------
# Request shape caps.
# --------------------------------------------------------------------------


def test_too_many_messages_rejected() -> None:
    settings = _settings(CAP_MESSAGES=3)
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(
            _req(messages=[{"role": "user", "content": "hi"}] * 4), settings
        )
    assert exc.value.status_code == 413
    assert exc.value.param == "input.messages"


def test_message_count_at_the_cap_is_accepted() -> None:
    settings = _settings(CAP_MESSAGES=3)
    validate_respond_request(_req(messages=[{"role": "user", "content": "hi"}] * 3), settings)


def test_too_many_content_parts_rejected() -> None:
    settings = _settings(CAP_CONTENT_PARTS=2)
    parts = [{"type": "text", "text": "a"}] * 3
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(
            _req(messages=[{"role": "user", "content": parts}]), settings
        )
    assert exc.value.status_code == 413


def test_too_many_tool_calls_in_one_message_rejected() -> None:
    settings = _settings(CAP_TOOL_CALLS_PER_MESSAGE=2)
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(
            _req(messages=[
                {"role": "user", "content": "go"},
                _assistant("c1", "c2", "c3"),
            ]),
            settings,
        )
    assert exc.value.status_code == 413


def test_too_many_stop_sequences_rejected() -> None:
    settings = _settings(CAP_STOP_SEQUENCES=2)
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(_req(generation={"stop": ["a", "b", "c"]}), settings)
    assert exc.value.status_code == 413
    assert exc.value.param == "generation.stop"


def test_an_oversized_stop_sequence_is_rejected() -> None:
    settings = _settings(CAP_STOP_SEQUENCE_BYTES=4)
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(_req(generation={"stop": ["ééé"]}), settings)
    assert exc.value.status_code == 413


def test_a_bare_string_stop_sequence_is_measured_too() -> None:
    """`stop` is `str | list[str]`, and the string form must not skip the cap."""
    settings = _settings(CAP_STOP_SEQUENCE_BYTES=4)
    with pytest.raises(DaintreeError):
        validate_respond_request(_req(generation={"stop": "ééé"}), settings)


# --------------------------------------------------------------------------
# The image data-URL policy.
# --------------------------------------------------------------------------


def _image(url: str) -> list[dict]:
    return [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]


@pytest.mark.parametrize(
    "url",
    [
        # A scripting format, and one that is not an image at all. Both are data
        # URLs, so the old `startswith("data:")` check accepted them.
        f"data:image/svg+xml;base64,{PNG_B64}",
        f"data:text/html;base64,{PNG_B64}",
        f"data:;base64,{PNG_B64}",
        # Not base64-encoded — the RFC allows percent-encoding, we do not.
        "data:image/png,hello",
        # Not valid base64.
        "data:image/png;base64,!!!!",
        # Not a data URL at all beyond the scheme.
        "data:",
    ],
)
def test_rejected_image_data_urls(url: str, settings: Settings) -> None:
    with pytest.raises(DaintreeError):
        validate_respond_request(_req(messages=_image(url)), settings)


@pytest.mark.parametrize("mime", sorted(["image/png", "image/jpeg", "image/webp", "image/gif"]))
def test_accepted_image_media_types(mime: str, settings: Settings) -> None:
    validate_respond_request(_req(messages=_image(f"data:{mime};base64,{PNG_B64}")), settings)


def test_an_oversized_image_is_rejected_without_decoding() -> None:
    """The size check must come BEFORE `b64decode`, which is the whole reason the
    cap is on the ENCODED form: base64 expands by 4/3, so bounding the string
    bounds the image without allocating it to find out.

    Proven with a payload that is oversized AND invalid base64. A valid payload
    would pass this test against an implementation that decoded first and
    measured afterwards — which is exactly the allocation the cap exists to
    avoid — so the invalidity is what makes the ordering observable: only a
    size-first implementation can reject this without raising on the decode.
    """
    settings = _settings(CAP_IMAGE_ENCODED_BYTES=8)
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(
            _req(messages=_image("data:image/png;base64," + "!" * 100)), settings
        )
    assert exc.value.status_code == 413, (
        "an oversized payload must be refused on size, not on its base64 validity"
    )


def test_images_are_counted_across_the_whole_request() -> None:
    """Per-request, not per-message: a hundred one-image messages costs the prompt
    what one hundred-image message does."""
    settings = _settings(CAP_IMAGES=2)
    one = _image(f"data:image/png;base64,{PNG_B64}")[0]
    validate_respond_request(_req(messages=[one, one]), settings)
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(_req(messages=[one, one, one]), settings)
    assert exc.value.status_code == 413


@pytest.mark.parametrize(
    "url",
    [
        # `;base64x` CONTAINS `;base64`; a substring check accepted it.
        f"data:image/png;base64x,{PNG_B64}",
        # Decodes to zero bytes, which `b64decode` accepts happily.
        "data:image/png;base64,",
    ],
)
def test_near_miss_data_urls_are_rejected(url: str, settings: Settings) -> None:
    with pytest.raises(DaintreeError):
        validate_respond_request(_req(messages=_image(url)), settings)


def test_an_uppercase_data_scheme_is_still_a_data_url() -> None:
    """`DATA:` is a valid data URL. A case-sensitive `startswith` classified it as
    REMOTE — harmless while remote URLs are off (it is refused for the wrong
    reason) and a hole the moment an operator enables them, because the entire
    image policy would then be skipped for anyone who capitalised the scheme."""
    settings = _settings(ALLOW_REMOTE_IMAGE_URLS=True)
    with pytest.raises(DaintreeError):
        validate_respond_request(
            _req(messages=_image(f"DATA:image/svg+xml;base64,{PNG_B64}")), settings
        )


def test_remote_images_count_against_the_image_cap_too() -> None:
    """The cap is about how many images a prompt carries, which is true whether
    the bytes ride the request or the provider fetches them. Counting only data
    URLs let the cap be bypassed entirely by sending remote ones."""
    settings = _settings(ALLOW_REMOTE_IMAGE_URLS=True, CAP_IMAGES=2)
    remote = _image("https://example.com/cat.png")[0]
    validate_respond_request(_req(messages=[remote, remote]), settings)
    with pytest.raises(DaintreeError) as exc:
        validate_respond_request(_req(messages=[remote, remote, remote]), settings)
    assert exc.value.status_code == 413


def test_a_remote_url_skips_the_data_url_policy_when_allowed() -> None:
    """The server never fetches a remote URL — the provider does — so there is
    nothing here to inspect, and the media-type allowlist must not reject it."""
    settings = _settings(ALLOW_REMOTE_IMAGE_URLS=True)
    validate_respond_request(
        _req(messages=_image("https://example.com/cat.png")), settings
    )
