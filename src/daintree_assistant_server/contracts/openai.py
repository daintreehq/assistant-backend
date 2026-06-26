"""OpenAI-compatible Chat Completions wire models.

A single Pydantic layer models the request and response exactly as they travel
on the wire. Role validation is intentionally permissive at the schema level
(``role: str``) so the orchestrator can reject ``system``/``developer`` roles
with the precise error code the specification requires, rather than a generic
schema failure.
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
    model_config = ConfigDict(extra="ignore")

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
    model_config = ConfigDict(extra="ignore")

    # OpenAI/DeepSeek function names are limited to 64 characters; this applies
    # to tool calls echoed back in assistant message history too.
    name: str = Field(min_length=1, max_length=64)
    arguments: str = Field(default="", max_length=1_048_576)


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=256)
    type: Literal["function"] = "function"
    function: FunctionCall


class FunctionDef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # OpenAI/DeepSeek function names are limited to 64 characters.
    name: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=8192)
    parameters: dict[str, Any] | None = None


class ToolDef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["function"] = "function"
    function: FunctionDef


class NamedToolChoiceFunction(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class NamedToolChoice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["function"]
    function: NamedToolChoiceFunction


ToolChoice = Literal["none", "auto", "required"] | NamedToolChoice


# --------------------------------------------------------------------------
# Request.
# --------------------------------------------------------------------------


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    content: str | list[ContentPart] | None = None
    name: str | None = Field(default=None, max_length=256)
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = Field(default=None, max_length=256)


class ResponseFormat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["text", "json_object"] = "text"


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="ignore")

    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    # ``extra="allow"`` so vendor metadata fields pass through harmlessly; the
    # orchestrator explicitly rejects parameters that would change semantics.
    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1, max_length=256)
    messages: list[ChatMessage] = Field(min_length=1)
    tools: list[ToolDef] | None = None
    tool_choice: ToolChoice | None = None
    stream: bool = False
    stream_options: StreamOptions | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)
    response_format: ResponseFormat | None = None
    stop: str | list[str] | None = None
    user: str | None = Field(default=None, max_length=512)
    # The Daintree extension is captured as a raw mapping and normalized later,
    # so a generic OpenAI client that omits it still validates.
    daintree: dict[str, Any] | None = None

    def resolved_max_tokens(self) -> int | None:
        """Return the effective max output tokens (the two fields are aliases)."""
        return self.max_completion_tokens or self.max_tokens


# --------------------------------------------------------------------------
# Response (non-streaming).
# --------------------------------------------------------------------------


class PromptTokensDetails(BaseModel):
    cached_tokens: int | None = None


class Usage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_tokens_details: PromptTokensDetails | None = None


class ResponseMessage(BaseModel):
    role: str = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None


class Choice(BaseModel):
    index: int = 0
    message: ResponseMessage
    finish_reason: str | None = None


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage | None = None
    # The Daintree metadata extension (also returned in the X-Daintree-Meta header).
    daintree: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# Response (streaming).
# --------------------------------------------------------------------------


class Delta(BaseModel):
    role: str | None = None
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class StreamChoice(BaseModel):
    index: int = 0
    delta: Delta
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[StreamChoice]
    usage: Usage | None = None


# --------------------------------------------------------------------------
# Models listing.
# --------------------------------------------------------------------------


class Model(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str = "daintree"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[Model]
