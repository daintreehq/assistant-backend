"""Compatibility tests using the official OpenAI Python SDK against the app.

The async SDK talks to the in-process ASGI app via ``httpx.ASGITransport``. The
``app`` fixture marks the app ready and builds the container directly, so no
lifespan run is required.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import openai
import pytest
import pytest_asyncio
from fakes import make_result, make_text_chunks, tool_call_for

API_KEY = "test-api-key"
SKILL_PREP = "daintree.workflow.prep-branch-for-review"


@pytest_asyncio.fixture
async def oai(app: Any) -> AsyncIterator[openai.AsyncOpenAI]:
    transport = httpx.ASGITransport(app=app)
    http_client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    client = openai.AsyncOpenAI(
        base_url="http://testserver/v1", api_key=API_KEY, http_client=http_client, max_retries=0
    )
    yield client
    await http_client.aclose()


@pytest.mark.asyncio
async def test_nonstream_completion(oai: openai.AsyncOpenAI, fake_provider) -> None:
    fake_provider.complete_result = make_result(content="Hello from Daintree.")
    completion = await oai.chat.completions.create(
        model="daintree-assistant",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert completion.choices[0].message.content == "Hello from Daintree."
    assert completion.model == "daintree-assistant"


@pytest.mark.asyncio
async def test_streaming_text(oai: openai.AsyncOpenAI, fake_provider) -> None:
    fake_provider.stream_chunks = make_text_chunks("hello streaming world")
    stream = await oai.chat.completions.create(
        model="daintree-assistant",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )
    text = ""
    async for c in stream:
        if c.choices and c.choices[0].delta.content:
            text += c.choices[0].delta.content
    assert "hello" in text


@pytest.mark.asyncio
async def test_main_model_tool_call(oai: openai.AsyncOpenAI, fake_provider) -> None:
    fake_provider.complete_result = make_result(
        tool_calls=[
            {"id": "call_1", "type": "function", "function": {"name": "git__status", "arguments": "{}"}}
        ],
        finish_reason="tool_calls",
        content=None,
    )
    completion = await oai.chat.completions.create(
        model="daintree-assistant",
        messages=[{"role": "user", "content": "status"}],
        tools=[{"type": "function", "function": {"name": "git__status", "parameters": {"type": "object"}}}],
    )
    call = completion.choices[0].message.tool_calls[0]
    assert call.function.name == "git__status"
    assert completion.choices[0].finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_tool_result_followup(oai: openai.AsyncOpenAI, fake_provider) -> None:
    fake_provider.complete_result = make_result(content="Branch is clean.")
    completion = await oai.chat.completions.create(
        model="daintree-assistant",
        messages=[
            {"role": "user", "content": "status"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "git__status", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "clean"},
        ],
    )
    assert "clean" in completion.choices[0].message.content.lower()


@pytest.mark.asyncio
async def test_multimodal_input(oai: openai.AsyncOpenAI, fake_provider) -> None:
    fake_provider.complete_result = make_result(content="I see an image.")
    completion = await oai.chat.completions.create(
        model="daintree-assistant",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            }
        ],
    )
    assert completion.choices[0].message.content


@pytest.mark.asyncio
async def test_extra_body_daintree_and_meta_header(oai: openai.AsyncOpenAI, fake_provider) -> None:
    fake_provider.tool_call_result = tool_call_for([SKILL_PREP])
    fake_provider.complete_result = make_result(content="ok")
    raw = await oai.chat.completions.with_raw_response.create(
        model="daintree-assistant",
        messages=[{"role": "user", "content": "prep branch"}],
        extra_body={"daintree": {"protocol_version": 1, "session_id": "s", "turn_id": "t1"}},
    )
    assert "x-daintree-meta" in {k.lower() for k in raw.headers}
    body = json.loads(raw.text)
    # The unknown top-level "daintree" field does not break SDK parsing.
    assert body["daintree"]["active_skills"][0]["id"] == SKILL_PREP


@pytest.mark.asyncio
async def test_system_message_raises_bad_request(oai: openai.AsyncOpenAI) -> None:
    with pytest.raises(openai.BadRequestError) as exc:
        await oai.chat.completions.create(
            model="daintree-assistant",
            messages=[{"role": "system", "content": "x"}, {"role": "user", "content": "y"}],
        )
    assert exc.value.body["code"] == "system_messages_not_allowed"


@pytest.mark.asyncio
async def test_models_list(oai: openai.AsyncOpenAI) -> None:
    models = await oai.models.list()
    ids = [m.id for m in models.data]
    assert "daintree-assistant" in ids


@pytest.mark.asyncio
async def test_invalid_api_key(app: Any) -> None:
    transport = httpx.ASGITransport(app=app)
    http_client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    bad = openai.AsyncOpenAI(
        base_url="http://testserver/v1", api_key="wrong", http_client=http_client, max_retries=0
    )
    with pytest.raises(openai.AuthenticationError):
        await bad.chat.completions.create(
            model="daintree-assistant", messages=[{"role": "user", "content": "x"}]
        )
    await http_client.aclose()
