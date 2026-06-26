"""Assemble the upstream main-model request.

Layer order (section 9.1):

1. permanent base system message
2. runtime-context system message (server-rendered typed local facts)
3. loaded-skills system message (active skill bodies, or a stable fallback)
4. client conversation (user / assistant / tool only)
5. synthetic skill-load assistant/tool exchange (newly loaded skills only)
6. trailing turn-context system message

All local context is treated as untrusted data: control characters are
stripped and every field has an explicit byte cap.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..contracts.extensions import RuntimeContext, TurnContext
from ..contracts.openai import (
    ChatCompletionRequest,
    ChatMessage,
    ImageContentPart,
    NamedToolChoice,
    TextContentPart,
    ToolCall,
    ToolDef,
)
from ..prompts.loader import (
    TEMPLATE_LOADED_SKILLS,
    TEMPLATE_RUNTIME_CONTEXT,
    TEMPLATE_TURN_FOOTER,
    PromptLoader,
)
from ..providers.base import ProviderChatRequest
from ..skills.load_events import SkillLoadExecution, synthetic_transcript
from ..skills.models import Skill
from ..text_utils import cap_bytes, message_text, sanitize_field, strip_control_chars

PROMPT_CACHE_KEY = "daintree-main"

_TIER_DESCRIPTIONS = {
    "operator": "you act on the user's behalf; mutating actions require per-action confirmation.",
    "autopilot": "you may take mutating actions automatically within this tier's limits.",
    "guarded": "mutating actions require explicit confirmation each time.",
    "readonly": "you may only read state; mutating actions are not permitted.",
}
_TIER_FALLBACK = "mutating actions require confirmation according to this tier."


def _cap_list_combined(
    items: list[str], max_bytes: int, max_count: int | None = None
) -> list[str]:
    out: list[str] = []
    total = 0
    for raw in items:
        if max_count is not None and len(out) >= max_count:
            break
        value = strip_control_chars(str(raw))
        if len(value.encode("utf-8")) > max_bytes:
            value, _ = cap_bytes(value, max_bytes)
        size = len(value.encode("utf-8")) + 1
        if out and total + size > max_bytes:
            break
        total += size
        out.append(value)
    return out


class PromptAssembler:
    """Builds the normalized upstream request from validated inputs."""

    def __init__(self, prompt_loader: PromptLoader, settings: Settings) -> None:
        self.prompts = prompt_loader
        self.settings = settings

    # ---- system message rendering --------------------------------------

    def render_runtime_context(self, runtime: RuntimeContext | None) -> str:
        rc = runtime or RuntimeContext()
        tier = rc.permission_tier or "unknown"
        tier_description = _TIER_DESCRIPTIONS.get(tier, _TIER_FALLBACK)

        mcp = rc.mcp
        mcp_connected = bool(mcp and mcp.connected)
        if mcp and mcp.connected:
            mcp_status_line = (
                f"connected ({mcp.transport or 'unknown'} transport, "
                f"{mcp.tool_count or 0} tools)"
            )
        elif mcp and mcp.status:
            mcp_status_line = strip_control_chars(mcp.status)
        else:
            mcp_status_line = "not connected"

        project_instructions, _ = sanitize_field(
            rc.project_instructions, self.settings.cap_project_instructions
        )
        project_path, _ = sanitize_field(rc.project_path, 4096)

        return self.prompts.render(
            TEMPLATE_RUNTIME_CONTEXT,
            tier=tier,
            tier_description=tier_description,
            project_path=project_path or "(unknown)",
            project_id=strip_control_chars(rc.project_id or ""),
            configured_agents=[strip_control_chars(a) for a in rc.configured_agent_ids],
            mcp_status_line=mcp_status_line,
            mcp_connected=mcp_connected,
            large_model=self.settings.deepseek_main_model,
            small_model=self.settings.deepseek_utility_model,
            scheduler_active=rc.scheduler_active,
            project_instructions=project_instructions,
        )

    def render_loaded_skills(self, skills: list[Skill]) -> str:
        return self.prompts.render(TEMPLATE_LOADED_SKILLS, loaded_skills=skills)

    def render_turn_footer(
        self, turn: TurnContext | None, active_worktree: str | None = None
    ) -> str:
        if turn is None and not active_worktree:
            return self.prompts.render(TEMPLATE_TURN_FOOTER, turn_context=None)
        turn = turn or TurnContext()

        goal, _ = sanitize_field(turn.goal, self.settings.cap_goal)
        worktree, _ = sanitize_field(active_worktree, self.settings.cap_active_worktree)
        workflow_runs = _cap_list_combined(turn.workflow_runs, self.settings.cap_workflow_runs)
        pinned = _cap_list_combined(turn.memories.pinned, self.settings.cap_pinned_memories)
        relevant = _cap_list_combined(turn.memories.relevant, self.settings.cap_relevant_memories)
        watchers = _cap_list_combined(
            turn.session_ended_watchers,
            self.settings.cap_watchers_bytes,
            max_count=self.settings.cap_watchers_count,
        )

        turn_context = {
            "goal": goal,
            "workflow_runs": workflow_runs,
            "memories": {"pinned": pinned, "relevant": relevant},
            "active_worktree": worktree,
            "session_ended_watchers": watchers,
        }
        return self.prompts.render(TEMPLATE_TURN_FOOTER, turn_context=turn_context)

    # ---- conversation translation --------------------------------------

    def _part_to_wire(self, part: Any) -> dict[str, Any]:
        if isinstance(part, TextContentPart):
            text, _ = cap_bytes(part.text, self.settings.cap_message_text_part)
            return {"type": "text", "text": text}
        if isinstance(part, ImageContentPart):
            image: dict[str, Any] = {"url": part.image_url.url}
            if part.image_url.detail:
                image["detail"] = part.image_url.detail
            return {"type": "image_url", "image_url": image}
        return {"type": "text", "text": str(part)}

    def _tool_call_to_wire(self, call: ToolCall) -> dict[str, Any]:
        return {
            "id": call.id,
            "type": "function",
            "function": {"name": call.function.name, "arguments": call.function.arguments},
        }

    def _message_to_wire(self, msg: ChatMessage) -> dict[str, Any]:
        wire: dict[str, Any] = {"role": msg.role}
        if msg.role == "tool":
            wire["content"] = message_text(msg.content)
            if msg.tool_call_id:
                wire["tool_call_id"] = msg.tool_call_id
            return wire

        if isinstance(msg.content, str):
            wire["content"] = msg.content
        elif isinstance(msg.content, list):
            wire["content"] = [self._part_to_wire(p) for p in msg.content]
        elif msg.content is None and msg.role == "assistant" and msg.tool_calls:
            wire["content"] = None
        else:
            wire["content"] = ""

        if msg.name:
            wire["name"] = msg.name
        if msg.tool_calls:
            wire["tool_calls"] = [self._tool_call_to_wire(tc) for tc in msg.tool_calls]
        return wire

    # ---- tool projection ------------------------------------------------

    def _project_tools(self, tools: list[ToolDef] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        out: list[dict[str, Any]] = []
        for tool in tools:
            fn = tool.function
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": fn.name,
                        "description": fn.description or "",
                        "parameters": fn.parameters or {"type": "object", "properties": {}},
                    },
                }
            )
        return out

    def _project_tool_choice(self, tool_choice: Any) -> Any:
        if tool_choice is None:
            return None
        if isinstance(tool_choice, NamedToolChoice):
            return {"type": "function", "function": {"name": tool_choice.function.name}}
        return tool_choice

    # ---- assembly -------------------------------------------------------

    def build_main_request(
        self,
        *,
        public_request: ChatCompletionRequest,
        runtime: RuntimeContext | None,
        turn: TurnContext | None,
        skills: list[Skill],
        synthetic_executions: list[SkillLoadExecution],
        upstream_model: str,
    ) -> ProviderChatRequest:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.prompts.base_prompt()},
            {"role": "system", "content": self.render_runtime_context(runtime)},
            {"role": "system", "content": self.render_loaded_skills(skills)},
        ]
        messages.extend(self._message_to_wire(m) for m in public_request.messages)
        messages.extend(synthetic_transcript(synthetic_executions))

        footer = self.render_turn_footer(turn, runtime.active_worktree if runtime else None)
        if footer.strip():
            messages.append({"role": "system", "content": footer})

        response_format = None
        if public_request.response_format is not None:
            response_format = {"type": public_request.response_format.type}

        return ProviderChatRequest(
            model=upstream_model,
            messages=messages,
            tools=self._project_tools(public_request.tools),
            tool_choice=self._project_tool_choice(public_request.tool_choice),
            temperature=public_request.temperature,
            max_tokens=public_request.resolved_max_tokens(),
            response_format=response_format,
            stop=public_request.stop,
            stream=public_request.stream,
            include_usage=bool(
                public_request.stream_options and public_request.stream_options.include_usage
            ),
            thinking_disabled=True,
            prompt_cache_key=PROMPT_CACHE_KEY,
            timeout=self.settings.main_timeout_seconds,
        )
