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

from ..config import DEEPSEEK_MODEL, Settings
from ..contracts.daintree_api import RespondRequest, RespondToolChoice
from ..contracts.extensions import RuntimeContext, TurnContext
from ..contracts.openai import (
    ChatMessage,
    ImageContentPart,
    TextContentPart,
    ToolCall,
    ToolDef,
)
from ..prompts.loader import (
    TEMPLATE_INTEGRATIONS,
    TEMPLATE_LOADED_SKILLS,
    TEMPLATE_RUNTIME_CONTEXT,
    TEMPLATE_TURN_FOOTER,
    PromptLoader,
)
from ..providers.base import ProviderChatRequest
from ..skills.load_events import SkillLoadExecution, synthetic_transcript
from ..skills.models import Skill
from ..text_utils import (
    cap_bytes,
    frame_data_block,
    message_text,
    sanitize_line,
)

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
    """Sanitize each item to a single safe line and bound the combined bytes.

    Each value is collapsed to one line (no embedded newlines) so a list item
    rendered into a heading or comma-separated run cannot inject extra lines.
    """
    out: list[str] = []
    total = 0
    for raw in items:
        if max_count is not None and len(out) >= max_count:
            break
        value = sanitize_line(str(raw), max_bytes)
        if not value:
            continue
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
                f"connected ({sanitize_line(mcp.transport, 64) or 'unknown'} transport, "
                f"{mcp.tool_count or 0} tools)"
            )
        elif mcp and mcp.status:
            mcp_status_line = sanitize_line(mcp.status, 64)
        else:
            mcp_status_line = "not connected"

        project_instructions = frame_data_block(
            rc.project_instructions,
            self.settings.cap_project_instructions,
            tag="project_instructions",
        )

        return self.prompts.render(
            TEMPLATE_RUNTIME_CONTEXT,
            tier=sanitize_line(tier, 64),
            tier_description=tier_description,
            project_path=sanitize_line(rc.project_path, 4096) or "(unknown)",
            project_id=sanitize_line(rc.project_id, 256),
            configured_agents=[
                a for a in (sanitize_line(x, 256) for x in rc.configured_agent_ids) if a
            ],
            mcp_status_line=mcp_status_line,
            mcp_connected=mcp_connected,
            large_model=DEEPSEEK_MODEL,
            small_model=DEEPSEEK_MODEL,
            scheduler_active=rc.scheduler_active,
            project_instructions=project_instructions,
        )

    def render_integrations(
        self, runtime: RuntimeContext | None, tools: list[ToolDef] | None
    ) -> str:
        """Render the client-reported MCP servers + tool index as inert data.

        Returns ``""`` when the client supplies neither, so a generic OpenAI
        client (or a CLI that has not yet started sending its integration
        surface) produces no integrations system message at all.
        """
        servers: list[dict[str, Any]] = []
        if runtime:
            for srv in runtime.mcp_servers[: self.settings.cap_mcp_servers]:
                name = sanitize_line(srv.name, 128)
                if not name:
                    continue
                servers.append(
                    {
                        "name": name,
                        "transport": sanitize_line(srv.transport, 64),
                        "status": sanitize_line(srv.status, 64),
                        "tool_count": srv.tool_count,
                        "description": sanitize_line(srv.description, 4096),
                        "instructions": frame_data_block(
                            srv.instructions, 16384, tag="mcp_instructions"
                        ),
                    }
                )

        tool_index: list[dict[str, str]] = []
        for tool in tools or []:
            fn = tool.function
            name = sanitize_line(fn.name, 64)
            if not name:
                continue
            tool_index.append(
                {
                    "name": name,
                    "description": sanitize_line(fn.description, self.settings.cap_tool_index_desc),
                }
            )

        if not servers and not tool_index:
            return ""

        rendered = self.prompts.render(
            TEMPLATE_INTEGRATIONS, mcp_servers=servers, tools=tool_index
        )
        capped, _ = cap_bytes(rendered, self.settings.cap_mcp_block_bytes)
        return capped

    def render_loaded_skills(self, skills: list[Skill]) -> str:
        return self.prompts.render(TEMPLATE_LOADED_SKILLS, loaded_skills=skills)

    def render_turn_footer(
        self, turn: TurnContext | None, active_worktree: str | None = None
    ) -> str:
        if turn is None and not active_worktree:
            return self.prompts.render(TEMPLATE_TURN_FOOTER, turn_context=None)
        turn = turn or TurnContext()

        goal = sanitize_line(turn.goal, self.settings.cap_goal)
        worktree = sanitize_line(active_worktree, self.settings.cap_active_worktree)
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
        if isinstance(tool_choice, RespondToolChoice):
            return {"type": "function", "function": {"name": tool_choice.name}}
        return tool_choice

    # ---- assembly -------------------------------------------------------

    def build_main_request(
        self,
        *,
        request: RespondRequest,
        skills: list[Skill],
        synthetic_executions: list[SkillLoadExecution],
        upstream_model: str,
    ) -> ProviderChatRequest:
        """Assemble the upstream request, ordered most-stable-first so DeepSeek's
        prefix cache covers the large, stable content (base prompt, the client's
        integration surface, and the active skill bodies) across a turn's rounds.
        Per-request variable context (runtime + turn) is appended LAST so it never
        poisons that cached prefix.
        """
        runtime = request.runtime
        turn = request.turn
        gen = request.generation

        # 1. static base prompt → cacheable across all sessions.
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.prompts.base_prompt()},
        ]
        # 2. integration surface (MCP + tools): session-stable → cacheable.
        integrations = self.render_integrations(runtime, request.input.tools)
        if integrations.strip():
            messages.append({"role": "system", "content": integrations})
        # 3. active skill bodies: turn-stable → cacheable across rounds.
        messages.append({"role": "system", "content": self.render_loaded_skills(skills)})
        # 4. conversation, then the synthetic skill-load exchange.
        messages.extend(self._message_to_wire(m) for m in request.input.messages)
        messages.extend(synthetic_transcript(synthetic_executions))
        # 5. per-request variable context LAST (kept out of the cached prefix).
        runtime_msg = self.render_runtime_context(runtime)
        if runtime_msg.strip():
            messages.append({"role": "system", "content": runtime_msg})
        footer = self.render_turn_footer(turn, runtime.active_worktree if runtime else None)
        if footer.strip():
            messages.append({"role": "system", "content": footer})

        response_format = None
        if gen.response_format is not None:
            response_format = {"type": gen.response_format}

        return ProviderChatRequest(
            model=upstream_model,
            messages=messages,
            tools=self._project_tools(request.input.tools),
            tool_choice=self._project_tool_choice(request.input.tool_choice),
            temperature=gen.temperature,
            max_tokens=gen.max_tokens,
            response_format=response_format,
            stop=gen.stop,
            stream=gen.stream,
            include_usage=gen.stream,
            thinking_disabled=True,
            prompt_cache_key=PROMPT_CACHE_KEY,
            timeout=self.settings.main_timeout_seconds,
        )
