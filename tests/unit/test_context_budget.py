"""The aggregate context budget.

Every other limit in the server bounds one input. These pin the one that bounds
the whole assembled prompt — and, just as importantly, pin how RELUCTANT it is:
the estimate is characters/4 against a window nothing has verified on the live
host, so a refusal that fired at the reservation boundary would fail turns the
upstream would have answered.
"""

from __future__ import annotations

from daintree_assistant_server.config import EXECUTOR_CONTEXT_TOKENS
from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.providers.base import ProviderChatRequest
from daintree_assistant_server.services.context_budget import (
    ContextMeasurement,
    context_window_exceeded,
    exceeds_window,
    measure,
)


def _request(
    *,
    system: str = "",
    user: str = "",
    tools: list[dict] | None = None,
    max_tokens: int | None = None,
) -> ProviderChatRequest:
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    if user:
        messages.append({"role": "user", "content": user})
    return ProviderChatRequest(
        model="m", messages=messages, tools=tools, max_tokens=max_tokens
    )


def test_the_split_follows_role_not_position() -> None:
    """ROLE decides the bucket, and position does not — the same two messages in
    either order must produce the same split. Asserted by actually reordering
    them: a test that only ever sends system-then-user would pass just as well
    against an implementation that split on index."""
    system_first = ProviderChatRequest(
        model="m",
        messages=[
            {"role": "system", "content": "s" * 4000},
            {"role": "user", "content": "u" * 800},
        ],
    )
    user_first = ProviderChatRequest(
        model="m",
        messages=[
            {"role": "user", "content": "u" * 800},
            {"role": "system", "content": "s" * 4000},
        ],
    )
    a, b = measure(system_first), measure(user_first)
    assert a.system_tokens == b.system_tokens > a.conversation_tokens
    assert a.conversation_tokens == b.conversation_tokens > 0


def test_the_system_region_carries_the_template_overhead() -> None:
    """Chat templates add role markers and separators no payload measurement can
    see. An empty request must therefore still cost something."""
    assert measure(_request()).system_tokens > 0


def test_tool_call_arguments_are_counted() -> None:
    """Serialized rather than walked field by field. A walker that reads only
    `content` misses arguments, `reasoning_content` and image data URLs — which
    are exactly the fields a turn gets too big on."""
    bare = ProviderChatRequest(
        model="m", messages=[{"role": "assistant", "content": None}]
    )
    with_args = ProviderChatRequest(
        model="m",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "t", "arguments": "x" * 4000},
                    }
                ],
            }
        ],
    )
    assert measure(with_args).conversation_tokens > measure(bare).conversation_tokens + 900


def test_reserved_output_counts_against_headroom_but_not_against_the_refusal() -> None:
    """The asymmetry is the whole design. `max_tokens` is a CEILING on the reply,
    not a promise the model will use it, so it belongs in the headroom a reader
    watches and not in the test that fails a paid turn."""
    without = measure(_request(user="hi"), context_window=1000)
    with_reservation = measure(_request(user="hi", max_tokens=8000), context_window=1000)
    # Exactly the reservation, not merely "less" — a headroom that moved by some
    # other amount would mean something else changed too.
    assert with_reservation.headroom_tokens == without.headroom_tokens - 8000
    assert with_reservation.headroom_tokens < 0
    assert not exceeds_window(with_reservation)
    # And the input estimate itself is untouched by it.
    assert with_reservation.input_tokens == without.input_tokens


def test_a_turn_is_refused_only_when_the_input_alone_clears_the_window() -> None:
    m = measure(_request(user="u" * 40_000), context_window=1000)
    assert exceeds_window(m)


def test_an_estimate_just_over_the_window_is_not_refused() -> None:
    """The margin, pinned at the boundary it exists to move.

    `input > window` was the original test and it broke the promise this module
    makes: one token over is precisely the case a characters/4 estimate against
    an unverified window gets wrong. Everything between the window and the margin
    is left to the upstream, whose own refusal now maps to the same code.
    """
    window = 10_000
    just_over = measure(_request(user="u" * 44_000), context_window=window)
    assert just_over.input_tokens > window, "the fixture must actually exceed the window"
    assert not exceeds_window(just_over)

    hopeless = measure(_request(user="u" * 200_000), context_window=window)
    assert exceeds_window(hopeless)


def test_non_ascii_text_is_not_counted_at_its_escaped_length() -> None:
    """`json.dumps` escapes a non-ASCII character to six characters by default,
    so a CJK conversation measured that way reads ~5.2x its real size — and an
    ordinary Japanese session would have been refused at about a fifth of the
    window. The characters the model sees are the characters to count."""
    cjk = measure(_request(user="日本語" * 1000))
    ascii_same_length = measure(_request(user="abc" * 1000))
    # Same character count, so the estimates should be within a hair of each
    # other rather than a factor of five apart.
    assert cjk.conversation_tokens < ascii_same_length.conversation_tokens * 1.2

    # And the case that matters: a wholly ordinary CJK session must not be refused.
    assert not exceeds_window(measure(_request(user="日本語" * 30_000)))


def test_an_image_is_charged_per_image_rather_than_per_byte() -> None:
    """A base64 payload is not text and a vision model does not tokenize it as
    text. Counting it at characters/4 made the validator and the budget
    contradict outright: `cap_image_encoded_bytes` allows 6 MiB, which read as
    1.57 MILLION tokens against a 131k window — so every legal image was an
    automatic 413."""
    six_mib = "A" * (6 * 1024 * 1024)
    request = ProviderChatRequest(
        model="m",
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{six_mib}"}},
                {"type": "text", "text": "describe this"},
            ],
        }],
    )
    m = measure(request)
    assert not exceeds_window(m), "a legal image must not be an automatic refusal"
    assert m.conversation_tokens < 5_000

    # Two images cost about twice one — the charge is per image, not per request.
    one = measure(ProviderChatRequest(model="m", messages=[{
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}}],
    }]))
    two = measure(ProviderChatRequest(model="m", messages=[{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}},
        ],
    }]))
    assert two.conversation_tokens > one.conversation_tokens * 1.8


def test_an_ordinary_turn_is_nowhere_near_the_window() -> None:
    m = measure(_request(system="s" * 20_000, user="u" * 4_000, max_tokens=8000))
    assert not exceeds_window(m)
    assert m.headroom_ratio > 0.8, "an ordinary turn should barely dent the window"


def test_the_dominant_layer_names_what_grew() -> None:
    tools_heavy = measure(_request(user="u", tools=[{"schema": "x" * 100_000}]))
    assert tools_heavy.dominant_layer == "tools"
    chat_heavy = measure(_request(user="u" * 100_000))
    assert chat_heavy.dominant_layer == "conversation"


def test_the_log_fields_carry_no_prompt_content() -> None:
    """A token count describes a prompt without carrying one; that is the point
    of logging it. Every value must be a number or a layer name."""
    fields = measure(_request(system="secret system prompt", user="private text")).log_fields()
    for value in fields.values():
        assert isinstance(value, (int, float, str))
    assert "secret" not in repr(fields)
    assert "private" not in repr(fields)
    assert fields["context_window"] == EXECUTOR_CONTEXT_TOKENS


def test_the_error_is_a_413_naming_the_dimensions() -> None:
    m = measure(_request(user="u" * 40_000), context_window=1000)
    exc = context_window_exceeded(m)
    assert isinstance(exc, DaintreeError)
    assert exc.status_code == 413
    assert exc.code == "context_window_exceeded"
    assert exc.param == "input.messages"
    # Not retryable: replaying an oversized prompt produces an oversized prompt.
    assert exc.retryable is False
    # The numbers a caller needs are in the message, because the envelope has
    # nowhere else to put them.
    assert str(m.input_tokens) in exc.message
    assert "1000" in exc.message
    assert m.dominant_layer in exc.message


def test_a_zero_window_does_not_divide_by_zero() -> None:
    m = ContextMeasurement(
        system_tokens=1,
        tool_tokens=0,
        conversation_tokens=0,
        reserved_output_tokens=0,
        context_window=0,
    )
    assert m.headroom_ratio == 0.0


def test_a_non_serializable_value_does_not_break_the_measurement() -> None:
    """A lost estimate must never be a lost turn. The orchestrator guards the
    call as well, but the measurement should not need saving from itself."""

    class Odd:
        pass

    request = ProviderChatRequest(
        model="m", messages=[{"role": "user", "content": Odd()}]
    )
    assert measure(request).conversation_tokens > 0


def test_headroom_ratio_is_bounded_reporting_not_clamped_reporting() -> None:
    """A negative ratio is the finding, not something to hide behind a floor."""
    m = measure(_request(user="u" * 100_000), context_window=1000)
    assert m.headroom_ratio < 0
