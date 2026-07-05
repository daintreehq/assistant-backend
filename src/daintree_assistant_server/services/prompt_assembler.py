"""Assemble the upstream main-model request.

Layer order — the large APPEND-ONLY conversation is kept in the cached prefix; the small
per-request tail (skill bodies + runtime + footer) follows it. The active skill set changes
by wholesale replacement on a topic shift, so placing the bodies AFTER the conversation means
a load/evict re-encodes only that bounded tail, never the conversation (a mutable block in
front of the append-only conversation is the one layout prefix caching cannot tolerate). It
also lands the runbook near generation, which a small model follows better. See
build_main_request:

1. base system message — static; cached across all sessions
2. integrations system message — client MCP servers + tool index (omitted when absent)
3. client conversation (user / assistant / tool only) — append-only; the cached bulk
4. loaded-skills system message — active skill bodies (omitted when the active set is empty)
5. runtime-context system message — server-rendered typed local facts (per-request)
6. trailing turn-context (turn-footer) system message (per-request)
7. response-contract system message — the output-shape rules, LAST so they sit nearest
   generation (the position a small model follows best)

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
    TEMPLATE_RESPONSE_CONTRACT,
    TEMPLATE_RUNTIME_CONTEXT,
    TEMPLATE_TURN_FOOTER,
    PromptLoader,
)
from ..providers.base import ProviderChatRequest
from ..skills.models import Skill
from ..text_utils import (
    cap_bytes,
    frame_data_block,
    message_text,
    sanitize_line,
)

PROMPT_CACHE_KEY = "daintree-main"

# JSON Schema (draft 2020-12) keywords whose VALUES are, or contain, subschemas.
# The tool-schema normalizer descends ONLY through these; every other keyword
# (enum, const, default, examples, type, required, format, …) is data or an
# annotation and is copied verbatim — so a literal value that merely contains an
# "items" key is never mistaken for a tuple schema and rewritten. ``items`` is
# handled separately because its value is a subschema in 2020-12 but a tuple LIST
# in draft-07 (the case we rewrite).
_SCHEMA_MAP_KEYS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)
_SCHEMA_LIST_KEYS = frozenset({"allOf", "anyOf", "oneOf", "prefixItems"})
_SCHEMA_SINGLE_KEYS = frozenset(
    {
        "additionalProperties",
        "propertyNames",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)

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

        # Open-terminal inventory (issue #286): cap the row count, sanitize every
        # client-supplied string to one inert line, and drop any row whose id sanitizes
        # away. exit_code is passed through unsanitized (a validated int | None).
        open_terminals = [
            {
                "id": tid,
                "kind": sanitize_line(t.kind, 64),
                "worktree_id": sanitize_line(t.worktree_id, 512),
                "title": sanitize_line(t.title, 512),
                "agent_id": sanitize_line(t.agent_id, 256),
                "agent_state": sanitize_line(t.agent_state, 64),
                "waiting_reason": sanitize_line(t.waiting_reason, 512),
                "exit_code": t.exit_code,
            }
            for t in rc.open_terminals[: self.settings.cap_open_terminals]
            if (tid := sanitize_line(t.id, 256))
        ]

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
            open_terminals=open_terminals,
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

    def render_response_contract(self) -> str:
        """The output-shape contract, rendered LAST so it sits nearest generation.

        Width comes from settings (the narrow Daintree terminal is ~56 columns);
        a future runtime field can override it per-request without moving the block.
        """
        return self.prompts.render(
            TEMPLATE_RESPONSE_CONTRACT,
            terminal_width=self.settings.main_terminal_width,
        )

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
        # Replay the model's prior chain-of-thought verbatim. DeepSeek REQUIRES it
        # on assistant turns that performed tool calls (else a 400) and ignores it
        # otherwise; either way it is the model's own output, never transformed.
        if msg.role == "assistant" and msg.reasoning_content is not None:
            wire["reasoning_content"] = msg.reasoning_content
        return wire

    # ---- tool projection ------------------------------------------------

    def _project_tools(self, tools: list[ToolDef] | None) -> list[dict[str, Any]] | None:
        if not tools:
            return None
        out: list[dict[str, Any]] = []
        for tool in tools:
            fn = tool.function
            params = fn.parameters or {"type": "object", "properties": {}}
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": fn.name,
                        "description": fn.description or "",
                        "parameters": self._normalize_tool_schema(params),
                    },
                }
            )
        return out

    @staticmethod
    def _normalize_tool_schema(schema: Any) -> Any:
        """Rewrite a client tool's JSON Schema into the draft 2020-12 shape DeepSeek
        validates against.

        Client tools are authored by the CLI and may target an OLDER JSON Schema draft
        (draft-07 and earlier), where an array-valued ``items`` means tuple validation.
        Draft 2020-12 forbids that — tuple item schemas must live under ``prefixItems``
        — and DeepSeek rejects the ENTIRE request with a 400 if any one tool schema
        uses the legacy form, sinking the whole turn (an opaque 502 to the user). So a
        single legacy tool must not be able to break generation: convert array ``items``
        to ``prefixItems`` (folding draft-07 ``additionalItems`` into 2020-12 ``items``)
        wherever a subschema can appear.

        The walk is SCHEMA-AWARE: it descends only through keywords whose values are
        subschemas (see ``_SCHEMA_*_KEYS``) and copies data/annotation keywords
        (``enum``, ``const``, ``default``, ``examples`` …) verbatim — so a literal
        value that merely contains an "items" key is never mistaken for a tuple schema.
        Builds fresh structures; never mutates the caller's ``parameters`` dict.
        """
        if not isinstance(schema, dict):
            # A boolean schema (draft 2020-12 allows ``true``/``false``) or any other
            # non-object node has nothing to rewrite.
            return schema
        out: dict[str, Any] = {}
        for key, value in schema.items():
            if key == "additionalItems":
                # Draft-07 only, and meaningful ONLY next to a tuple ``items`` — it is
                # folded in by the ``items`` branch below and otherwise dropped (it is
                # dead in draft 2020-12).
                continue
            if key == "items":
                if isinstance(value, list):
                    # Draft-07 tuple → draft 2020-12 prefixItems.
                    out["prefixItems"] = [
                        PromptAssembler._normalize_tool_schema(v) for v in value
                    ]
                    # draft-07 ``additionalItems`` (schema for elements past the tuple)
                    # is spelled ``items`` in 2020-12. Preserve it when present; its
                    # absence leaves extras unconstrained, matching draft-07.
                    if "additionalItems" in schema:
                        out["items"] = PromptAssembler._normalize_tool_schema(
                            schema["additionalItems"]
                        )
                else:
                    # A plain (non-tuple) ``items`` is a single subschema — recurse.
                    out["items"] = PromptAssembler._normalize_tool_schema(value)
            elif key in _SCHEMA_MAP_KEYS and isinstance(value, dict):
                out[key] = {
                    name: PromptAssembler._normalize_tool_schema(sub)
                    for name, sub in value.items()
                }
            elif key in _SCHEMA_LIST_KEYS and isinstance(value, list):
                out[key] = [PromptAssembler._normalize_tool_schema(v) for v in value]
            elif key in _SCHEMA_SINGLE_KEYS:
                out[key] = PromptAssembler._normalize_tool_schema(value)
            else:
                # Data or annotation keyword (enum/const/default/examples/type/…):
                # arbitrary JSON — copy verbatim, never descend.
                out[key] = value
        return out

    def _project_tool_choice(self, tool_choice: Any) -> Any:
        if tool_choice is None:
            return None
        if isinstance(tool_choice, RespondToolChoice):
            return {"type": "function", "function": {"name": tool_choice.name}}
        return tool_choice

    @staticmethod
    def _is_forced_tool_choice(tool_choice: Any) -> bool:
        """A forced tool_choice ("required" or a named tool) is incompatible with
        DeepSeek thinking mode — the same constraint that keeps the selector
        non-thinking. "auto"/"none"/absent are fine."""
        if isinstance(tool_choice, RespondToolChoice):
            return True
        return bool(tool_choice == "required")

    # ---- assembly -------------------------------------------------------

    def build_main_request(
        self,
        *,
        request: RespondRequest,
        skills: list[Skill],
        upstream_model: str,
    ) -> ProviderChatRequest:
        """Assemble the upstream request so DeepSeek's prefix cache covers the base
        prompt, the integration surface, and the append-only conversation. The active
        skill bodies and the per-request runtime/turn context follow the conversation,
        so a skill-set change re-encodes only that bounded tail — never the conversation.
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
        # 3. client conversation: append-only and the largest segment → kept in the cached prefix.
        messages.extend(self._message_to_wire(m) for m in request.input.messages)
        # 4. active skill bodies AFTER the conversation: a set change (load/evict) re-encodes only
        #    this bounded block, never the conversation, and lands the runbook near generation.
        if skills:
            messages.append({"role": "system", "content": self.render_loaded_skills(skills)})
        # 5. per-request variable context LAST (kept out of the cached prefix).
        runtime_msg = self.render_runtime_context(runtime)
        if runtime_msg.strip():
            messages.append({"role": "system", "content": runtime_msg})
        footer = self.render_turn_footer(turn, runtime.active_worktree if runtime else None)
        if footer.strip():
            messages.append({"role": "system", "content": footer})
        # 6. response contract LAST — the output-shape rules sit nearest generation.
        contract = self.render_response_contract()
        if contract.strip():
            messages.append({"role": "system", "content": contract})

        # "text" is the upstream default, so forwarding it is a no-op — except that
        # DeepSeek rejects ANY response_format alongside tools ("You cannot specify
        # response format and function call at the same time") and the provider
        # withholds prompt_cache_key when response_format is set. Only "json_object"
        # changes behavior, and validation rejects that in combination with tools.
        response_format = None
        if gen.response_format == "json_object":
            response_format = {"type": "json_object"}

        # Thinking is ON by default (MAIN_THINKING_ENABLED) and only ever on the main
        # call. Force it off when the request pins a tool_choice: DeepSeek 400s on
        # thinking + forced tool_choice, so degrade to non-thinking rather than
        # fail the turn.
        thinking_enabled = self.settings.main_thinking_enabled and not self._is_forced_tool_choice(
            request.input.tool_choice
        )

        # max_tokens policy: fill a compact default when the client omits one, then clamp
        # to a hard ceiling either way so a degenerate turn (the observed prompt-recitation
        # dump) can't run unbounded. The ceiling is generous — it catches absurd runaways,
        # not a legitimately long synthesis.
        max_tokens = gen.max_tokens or self.settings.main_default_max_tokens
        max_tokens = min(max_tokens, self.settings.main_max_tokens_ceiling)

        return ProviderChatRequest(
            model=upstream_model,
            messages=messages,
            tools=self._project_tools(request.input.tools),
            tool_choice=self._project_tool_choice(request.input.tool_choice),
            temperature=gen.temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            stop=gen.stop,
            stream=gen.stream,
            include_usage=gen.stream,
            # The main call is the only server call that may use thinking (the
            # selector's forced tool_choice forbids it). ON by default; when enabled,
            # reasoning_content round-trips through the wire contract so tool-call
            # turns can replay it. Thinking mode ignores temperature/top_p.
            thinking_disabled=not thinking_enabled,
            reasoning_effort=self.settings.main_reasoning_effort if thinking_enabled else None,
            prompt_cache_key=PROMPT_CACHE_KEY,
            timeout=self.settings.main_timeout_seconds,
        )
