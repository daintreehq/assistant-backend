"""Internal canonical message / tool / content models.

These mirror the OpenAI wire shape because that is what the provider adapters
speak *upstream*. They are reused as the internal conversation representation
by the selector, the prompt assembler, and the native respond contract — so the
public protocol can be Daintree-native while the provider stays unchanged.
Role validation is intentionally permissive at the schema level (``role: str``)
so validation can reject ``system``/``developer`` with a precise error code.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------
# Multimodal content parts.
# --------------------------------------------------------------------------


class TextContentPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"]
    text: str


class ImageUrl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    detail: str | None = None


class ImageContentPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["image_url"]
    image_url: ImageUrl


ContentPart = Annotated[
    TextContentPart | ImageContentPart,
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------
# Tool calls and definitions.
# --------------------------------------------------------------------------


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # OpenAI-wire function names are limited to 64 characters; this applies
    # to tool calls echoed back in assistant message history too.
    name: str = Field(min_length=1, max_length=64)
    arguments: str = Field(default="", max_length=1_048_576)


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=256)
    type: Literal["function"] = "function"
    function: FunctionCall


class FunctionDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # OpenAI-wire function names are limited to 64 characters.
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=8192)
    parameters: dict[str, Any] | None = None


class ToolDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["function"] = "function"
    function: FunctionDef


# --------------------------------------------------------------------------
# Message.
# --------------------------------------------------------------------------


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    content: str | list[ContentPart] | None = None
    name: str | None = Field(default=None, max_length=256)
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = Field(default=None, max_length=256)
    # The serving model's chain-of-thought. The model returns it alongside
    # content; on an assistant turn that performed tool calls it MUST be replayed
    # verbatim in later requests or the upstream returns 400 (it is ignored on
    # turns without tool calls). This is Daintree's canonical name for the field:
    # it happens to be Baseten's own spelling too, in both the message and the
    # streaming delta, which is why `providers/baseten.py` undoes the parent
    # adapter's outbound rename to `reasoning`. Prompt assembly forwards it
    # verbatim on tool-call turns only and drops it on final-answer turns, where
    # it would be pure prompt cost.
    reasoning_content: str | None = Field(default=None, max_length=1_048_576)
