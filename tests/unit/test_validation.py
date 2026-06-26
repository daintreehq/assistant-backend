"""Public request validation tests."""

from __future__ import annotations

import pytest

from daintree_assistant_server.config import Settings
from daintree_assistant_server.contracts.openai import ChatCompletionRequest
from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.services.validation import validate_chat_request


@pytest.fixture
def settings() -> Settings:
    return Settings(DAINTREE_API_KEY="k", DEEPSEEK_API_KEY="d", STATE_SIGNING_KEY_CURRENT="s")


def _req(**kwargs: object) -> ChatCompletionRequest:
    base: dict[str, object] = {
        "model": "daintree-assistant",
        "messages": [{"role": "user", "content": "hi"}],
    }
    base.update(kwargs)
    return ChatCompletionRequest.model_validate(base)


def test_valid_request_returns_upstream_model(settings: Settings) -> None:
    assert validate_chat_request(_req(), settings) == settings.deepseek_main_model


@pytest.mark.parametrize("role", ["system", "developer"])
def test_system_messages_rejected(settings: Settings, role: str) -> None:
    req = _req(messages=[{"role": role, "content": "x"}, {"role": "user", "content": "y"}])
    with pytest.raises(DaintreeError) as exc:
        validate_chat_request(req, settings)
    assert exc.value.code == "system_messages_not_allowed"
    assert exc.value.param == "messages[0].role"


def test_system_message_scanned_anywhere(settings: Settings) -> None:
    req = _req(messages=[{"role": "user", "content": "a"}, {"role": "system", "content": "b"}])
    with pytest.raises(DaintreeError) as exc:
        validate_chat_request(req, settings)
    assert exc.value.param == "messages[1].role"


def test_unknown_role_rejected(settings: Settings) -> None:
    req = _req(messages=[{"role": "robot", "content": "x"}])
    with pytest.raises(DaintreeError) as exc:
        validate_chat_request(req, settings)
    assert exc.value.code == "unsupported_role"


@pytest.mark.parametrize("name", ["skill.find", "skill.load", "skill__find", "skill__load", "daintree_internal__x"])
def test_reserved_tools_rejected(settings: Settings, name: str) -> None:
    req = _req(tools=[{"type": "function", "function": {"name": name}}])
    with pytest.raises(DaintreeError) as exc:
        validate_chat_request(req, settings)
    assert exc.value.code == "reserved_tool_name"


def test_allowed_wire_safe_tools(settings: Settings) -> None:
    req = _req(
        tools=[
            {"type": "function", "function": {"name": "skill__run__get"}},
            {"type": "function", "function": {"name": "skill__step__advance"}},
        ]
    )
    validate_chat_request(req, settings)


def test_duplicate_tool_rejected(settings: Settings) -> None:
    req = _req(
        tools=[
            {"type": "function", "function": {"name": "git__status"}},
            {"type": "function", "function": {"name": "git__status"}},
        ]
    )
    with pytest.raises(DaintreeError) as exc:
        validate_chat_request(req, settings)
    assert exc.value.code == "duplicate_tool_name"


def test_tool_choice_must_be_offered(settings: Settings) -> None:
    req = _req(
        tools=[{"type": "function", "function": {"name": "git__status"}}],
        tool_choice={"type": "function", "function": {"name": "other"}},
    )
    with pytest.raises(DaintreeError) as exc:
        validate_chat_request(req, settings)
    assert exc.value.code == "tool_choice_not_offered"


def test_conflicting_max_tokens(settings: Settings) -> None:
    req = _req(max_tokens=10, max_completion_tokens=20)
    with pytest.raises(DaintreeError) as exc:
        validate_chat_request(req, settings)
    assert exc.value.code == "conflicting_parameters"


@pytest.mark.parametrize("param", ["top_p", "frequency_penalty", "logit_bias", "seed", "functions"])
def test_unsupported_params_rejected(settings: Settings, param: str) -> None:
    req = _req(**{param: 1})
    with pytest.raises(DaintreeError) as exc:
        validate_chat_request(req, settings)
    assert exc.value.code == "unsupported_parameter"


def test_n_equal_one_allowed(settings: Settings) -> None:
    validate_chat_request(_req(n=1), settings)


def test_n_greater_than_one_rejected(settings: Settings) -> None:
    with pytest.raises(DaintreeError):
        validate_chat_request(_req(n=2), settings)


def test_unknown_model_rejected(settings: Settings) -> None:
    with pytest.raises(DaintreeError) as exc:
        validate_chat_request(_req(model="gpt-4"), settings)
    assert exc.value.code == "model_not_found"


def test_migration_alias_accepted(settings: Settings) -> None:
    validate_chat_request(_req(model="deepseek-v4-flash"), settings)


def test_remote_image_rejected_by_default(settings: Settings) -> None:
    req = _req(
        messages=[
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}],
            }
        ]
    )
    with pytest.raises(DaintreeError) as exc:
        validate_chat_request(req, settings)
    assert exc.value.code == "remote_image_not_allowed"


def test_data_url_image_allowed(settings: Settings) -> None:
    req = _req(
        messages=[
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}],
            }
        ]
    )
    validate_chat_request(req, settings)


def test_too_many_tools(settings: Settings) -> None:
    tools = [{"type": "function", "function": {"name": f"t{i}"}} for i in range(settings.max_tools + 1)]
    with pytest.raises(DaintreeError) as exc:
        validate_chat_request(_req(tools=tools), settings)
    assert exc.value.status_code == 413
