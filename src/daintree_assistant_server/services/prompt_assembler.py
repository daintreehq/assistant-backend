"""Assemble the cache-aware upstream main-model request.

The DeepSeek chat template serializes every system message before tools and non-system
messages (unchanged by routing through OpenRouter — it is the model's template, not the
API's), so only stable content may use the system role. The effective order is:

1. base → integrations → active skill bodies → response contract (stable system region)
2. tool schemas
3. stable startup context (one framed user-role data row, omitted when empty)
4. visible append-only conversation
5. volatile runtime + turn context (one framed user-role tail, omitted when empty)

Stable startup facts therefore sit on the cache-friendly side of the conversation without
masquerading as a user-authored message in the public transcript, selector, or learner.
All client context is untrusted: fields are sanitized and bounded before rendering.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from ..config import INTERACTIVE_THINKING_ENABLED, OPENROUTER_REASONING_EFFORT, Settings
from ..contracts.daintree_api import RespondRequest, RespondToolChoice
from ..contracts.extensions import (
    AgentSnapshot,
    RuntimeContext,
    StartupContext,
    TurnContext,
    WorkflowDigest,
)
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
    TEMPLATE_STARTUP_CONTEXT,
    TEMPLATE_TURN_FOOTER,
    PromptLoader,
)
from ..providers.base import ProviderChatRequest
from ..providers.policy import routing_preference
from ..skills.models import Skill
from ..text_utils import (
    cap_bytes,
    frame_data_block,
    message_text,
    sanitize_line,
    utf8_len,
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


def skills_in_render_order(skills: Sequence[Skill]) -> list[Skill]:
    """THE order the loaded-skills block is rendered in.

    Foundations first (stable sort), so a domain's base judgment reads before the
    workflow steps that build on it. Render order only — the active SET, and the
    selector's best-match order within each tier, are untouched.

    This is the single definition: the orchestrator compares speculative vs
    resolved skill sets through :func:`render_order_ids` so its notion of "the
    same request" cannot drift from the bytes this produces.
    """
    return sorted(skills, key=lambda s: not s.meta.foundation)


def render_order_ids(skills: Sequence[Skill]) -> list[str]:
    """The render-order id list — equal lists mean byte-identical skill blocks."""
    return [s.id for s in skills_in_render_order(skills)]


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
        size = utf8_len(value) + 1
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

    @staticmethod
    def _startup_agent_row(agent: AgentSnapshot) -> str:
        """Render one complete agent row without ever truncating its exact id."""
        quoted_id = json.dumps(agent.id, ensure_ascii=False)
        display_name = sanitize_line(agent.display_name, 512)
        row = f"- id {quoted_id}"
        if display_name and display_name != agent.id:
            row += f" — {display_name}"
        row += f" [{agent.source}]"

        availability: str
        if agent.availability:
            availability = agent.availability
        elif agent.installed is True:
            availability = "installed"
        elif agent.installed is False:
            availability = "missing"
        else:
            availability = "availability unknown"
        row += f" · {availability}"

        if agent.launchable is None:
            row += " · launchability unknown"
        elif agent.launchable:
            row += " · launchable"
        else:
            row += " · not launchable"

        if agent.toolbar_visible is None:
            row += " · toolbar n/a"
        elif agent.toolbar_visible:
            row += " · main toolbar"
        else:
            row += " · not in main toolbar"

        if agent.pinned is True:
            row += " (explicitly pinned)"
        elif agent.pinned is False:
            row += " (explicitly hidden)"
        return row

    def render_startup_context(self, startup: StartupContext) -> str:
        """Render stable typed project/agent facts as one cache-friendly data row.

        The caller inserts this before visible conversation. Agent rows have a
        combined byte budget and are admitted whole, so a long extension id is
        either represented exactly or omitted with an explicit count.
        """
        project: dict[str, Any] | None = None
        if startup.project is not None:
            project_src = startup.project
            project = {
                "id": sanitize_line(project_src.id, 256),
                "name": sanitize_line(project_src.name, 512),
                "path": sanitize_line(project_src.path, 4096),
                "status": sanitize_line(project_src.status, 64),
                "has_config_state": (
                    project_src.daintree_config_present is not None
                    or project_src.in_repo_settings is not None
                ),
                "config_label": (
                    "present"
                    if project_src.daintree_config_present is True
                    else "absent"
                    if project_src.daintree_config_present is False
                    else "unknown"
                ),
                "settings_label": (
                    "in-repository"
                    if project_src.in_repo_settings is True
                    else "local"
                    if project_src.in_repo_settings is False
                    else "unknown"
                ),
            }

        roster: dict[str, Any] | None = None
        if startup.agent_roster is not None:
            roster_src = startup.agent_roster
            rows: list[str] = []
            used = 0
            for agent in roster_src.agents[: self.settings.cap_startup_agents]:
                if not agent.id.strip():
                    continue
                row = self._startup_agent_row(agent)
                cost = utf8_len(row) + 1
                if used + cost > self.settings.cap_startup_agent_bytes:
                    continue
                used += cost
                rows.append(row)

            total = max(roster_src.total_count, len(roster_src.agents))
            completeness = "complete" if roster_src.complete else "partial"
            summary = (
                f"Registered direct-agent catalog: {completeness}; showing {len(rows)}"
            )
            if total > len(rows):
                summary += f" of {total}"
            if not roster_src.availability_complete:
                summary += "; one or more availability states are unknown"
            summary += "."
            roster = {
                "rows": rows,
                "summary": summary,
                "omitted": max(0, total - len(rows)),
            }

        project_instructions = frame_data_block(
            startup.project_instructions,
            self.settings.cap_project_instructions,
            tag="project_instructions",
        )
        if project is None and roster is None and not project_instructions:
            return ""
        return self.prompts.render(
            TEMPLATE_STARTUP_CONTEXT,
            project=project,
            agent_roster=roster,
            project_instructions=project_instructions,
        ).strip()

    @staticmethod
    def _worktree_context(runtime: RuntimeContext) -> dict[str, Any] | None:
        if runtime.worktree is None:
            return None
        if runtime.worktree.current is None:
            return {"current": None}

        wt = runtime.worktree.current
        parts: list[str] = []
        if branch := sanitize_line(wt.branch, 512):
            parts.append(f"branch {branch}")
        if wt.is_main:
            parts.append("main worktree")
        if wt.issue_number is not None:
            issue = f"issue #{wt.issue_number}"
            if title := sanitize_line(wt.issue_title, 512):
                issue += f" {title}"
            parts.append(issue)
        elif title := sanitize_line(wt.issue_title, 512):
            parts.append(f"issue {title}")
        if wt.pr_number is not None:
            pr = f"PR #{wt.pr_number}"
            if title := sanitize_line(wt.pr_title, 512):
                pr += f" {title}"
            parts.append(pr)
        elif title := sanitize_line(wt.pr_title, 512):
            parts.append(f"PR {title}")
        for label, value, cap in (
            ("PR URL", wt.pr_url, 4096),
            ("status", wt.status, 64),
            ("last commit", wt.last_commit, 1024),
            ("id", wt.id, 4096),
            ("path", wt.path, 4096),
        ):
            if clean := sanitize_line(value, cap):
                parts.append(f"{label} {clean}")
        return {"current": {"summary": " · ".join(parts)}}

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
            mcp_status_line=mcp_status_line,
            mcp_connected=mcp_connected,
            scheduler_active=rc.scheduler_active,
            worktree=self._worktree_context(rc),
            open_terminals=open_terminals,
        )

    def render_integrations(self, runtime: RuntimeContext | None) -> str:
        """Render the client-reported MCP servers as inert data.

        The client's tools are NOT listed here — their schemas are already sent
        through the function-calling interface, so a name+description index would
        only duplicate them. Returns ``""`` when the client reports no MCP
        servers, so a generic OpenAI client (or a CLI that has not yet started
        sending its integration surface) produces no integrations system message
        at all.
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

        if not servers:
            return ""

        rendered = self.prompts.render(TEMPLATE_INTEGRATIONS, mcp_servers=servers)
        capped, _ = cap_bytes(rendered, self.settings.cap_mcp_block_bytes)
        return capped

    def render_loaded_skills(self, skills: list[Skill]) -> str:
        return self.prompts.render(
            TEMPLATE_LOADED_SKILLS, loaded_skills=skills_in_render_order(skills)
        )

    def render_response_contract(self) -> str:
        """The output-shape contract — the final STABLE system block before the
        conversation (see build_main_request's cache-critical layout).

        Width comes from settings (the narrow Daintree terminal is ~56 columns);
        a future runtime field can override it per-request without moving the block.
        """
        return self.prompts.render(
            TEMPLATE_RESPONSE_CONTRACT,
            terminal_width=self.settings.main_terminal_width,
        )

    # Workflow-digest line/list bounds (turn.workflow_state). The per-line cap is
    # part of the wire contract (max string length per line: 512); the digest
    # count and total block budget come from settings.
    _WORKFLOW_LINE_BYTES = 512
    _WORKFLOW_LIST_ITEMS = 8

    def _workflow_digests(self, digests: list[WorkflowDigest]) -> list[dict[str, Any]]:
        """Sanitize and hard-cap the client's workflow digests.

        Three independent bounds: digest COUNT (cap_workflow_digests), one inert
        512-byte LINE per string (an untrusted digest cannot inject headings or
        flood a line), and a TOTAL byte budget (cap_workflow_state_bytes) across
        the whole block — digests are client-ranked, so when the budget runs out
        the tail is dropped, never the head.
        """

        def lines(items: list[str]) -> list[str]:
            capped = (sanitize_line(item, self._WORKFLOW_LINE_BYTES) for item in items)
            return [line for line in capped if line][: self._WORKFLOW_LIST_ITEMS]

        out: list[dict[str, Any]] = []
        used = 0
        for digest in digests[: self.settings.cap_workflow_digests]:
            wf_id = sanitize_line(digest.id, 128)
            if not wf_id:
                continue
            rendered: dict[str, Any] = {
                "id": wf_id,
                "goal": sanitize_line(digest.goal, self._WORKFLOW_LINE_BYTES),
                "status": sanitize_line(digest.status, 64),
                "progress": sanitize_line(digest.progress, self._WORKFLOW_LINE_BYTES),
                "active_nodes": lines(digest.active_nodes),
                "resources": lines(digest.resources),
                "blockers": lines(digest.blockers),
                "next_action": sanitize_line(digest.next_action, self._WORKFLOW_LINE_BYTES),
                "last_event": sanitize_line(digest.last_event, self._WORKFLOW_LINE_BYTES),
            }
            size = 0
            for value in rendered.values():
                if isinstance(value, str):
                    size += utf8_len(value) + 1
                else:
                    size += sum(utf8_len(item) + 1 for item in value)
            if out and used + size > self.settings.cap_workflow_state_bytes:
                break
            used += size
            out.append(rendered)
        return out

    def render_turn_footer(self, turn: TurnContext | None) -> str:
        if turn is None:
            return self.prompts.render(TEMPLATE_TURN_FOOTER, turn_context=None)
        turn = turn or TurnContext()

        goal = sanitize_line(turn.goal, self.settings.cap_goal)
        workflow_runs = _cap_list_combined(turn.workflow_runs, self.settings.cap_workflow_runs)
        async_operations = _cap_list_combined(
            turn.async_operations, self.settings.cap_async_operations
        )
        pinned = _cap_list_combined(turn.memories.pinned, self.settings.cap_pinned_memories)
        relevant = _cap_list_combined(turn.memories.relevant, self.settings.cap_relevant_memories)
        watchers = _cap_list_combined(
            turn.resumed_watchers,
            self.settings.cap_watchers_bytes,
            max_count=self.settings.cap_watchers_count,
        )

        turn_context = {
            "goal": goal,
            "workflow_runs": workflow_runs,
            "async_operations": async_operations,
            "memories": {"pinned": pinned, "relevant": relevant},
            "resumed_watchers": watchers,
            "workflow_state": self._workflow_digests(turn.workflow_state),
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
        # Replay the model's prior chain-of-thought verbatim, but ONLY on assistant
        # turns that performed tool calls — that is where it earns its tokens, letting
        # the model continue the reasoning that produced those calls instead of
        # re-deriving it. On a completed final-answer turn (no tool_calls) replaying it
        # is pure prompt cost, so it is dropped. When forwarded it is the model's own
        # output, never transformed; the provider adapter renames the field to
        # OpenRouter's `reasoning` on the way out.
        if msg.role == "assistant" and msg.tool_calls and msg.reasoning_content is not None:
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
        """Rewrite a client tool's JSON Schema into the draft 2020-12 shape the
        upstream validates against.

        Client tools are authored by the CLI and may target an OLDER JSON Schema draft
        (draft-07 and earlier), where an array-valued ``items`` means tuple validation.
        Draft 2020-12 forbids that — tuple item schemas must live under ``prefixItems``
        — and the upstream rejects the ENTIRE request with a 400 if any one tool schema
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
        non-thinking. It is the model's constraint, not the router's, so it holds
        however OpenRouter routes the call. "auto"/"none"/absent are fine."""
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
        """Assemble the upstream request so the upstream prefix cache covers the base
        prompt, the integration surface, the skill bodies, the tool schemas, and the
        append-only conversation.

        CACHE-CRITICAL LAYOUT (measured 2026-07-08, latency benchmark, against
        DeepSeek directly; the template is the model's, so it survives the move to
        OpenRouter): the prompt serializes as [ALL system messages, in array order] →
        [tools] → [non-system conversation]. A system message ANYWHERE in the array
        therefore lands in the leading system region, BEFORE the ~18k-token tool
        schemas and the conversation. The old layout put the volatile runtime/turn
        context after the conversation as system messages — the template hoisted them
        into the system region, so every new session (and any mid-turn roster change)
        re-encoded tools + conversation: ~36% cache hit on identical back-to-back
        turns. The
        rule that follows: ONLY stable content may use role "system"; per-request
        variable context must ride a NON-system role at the very end, where it costs
        exactly its own tokens.
        """
        runtime = request.runtime
        turn = request.turn
        gen = request.generation

        # 1. static base prompt → cacheable across all sessions.
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.prompts.base_prompt()},
        ]
        # 2. integration surface (MCP servers): session-stable → cacheable.
        integrations = self.render_integrations(runtime)
        if integrations.strip():
            messages.append({"role": "system", "content": integrations})
        # 3. active skill bodies: semi-stable (changes only on a selector topic
        #    shift). System-role, so array position == serialized position; a set
        #    change re-encodes tools + conversation, which is unavoidable for
        #    system-region content and rare enough to accept.
        if skills:
            messages.append({"role": "system", "content": self.render_loaded_skills(skills)})
        # 4. response contract: static output-shape rules → part of the stable
        #    system prefix. (It serialized before the conversation even
        #    when the array placed it last, so this costs no "nearness to
        #    generation" that ever actually existed.)
        contract = self.render_response_contract()
        if contract.strip():
            messages.append({"role": "system", "content": contract})
        # 5. session-stable project/agent/instruction data. It is a user-role
        #    data row so the template leaves it after tools, but it sits before the
        #    append-only conversation and is structurally absent from selector /
        #    learner inputs.
        startup = self.render_startup_context(request.startup)
        if startup.strip():
            messages.append({"role": "user", "content": startup})
        # 6. client conversation: append-only and the largest segment → cached
        #    prefix grows with it.
        messages.extend(self._message_to_wire(m) for m in request.input.messages)
        # 7. per-request variable context LAST as ONE user-role data block — the
        #    only place volatile content is cache-cheap (see docstring). The frame
        #    line keeps the model from reading injected state as the human speaking.
        variable_parts: list[str] = []
        # Only render runtime context the client actually SENT: render_runtime_context
        # materializes a default RuntimeContext (tier "unknown" etc.) for None, which
        # would append a synthetic tail block to every runtime-less request (generic
        # OpenAI-style clients) — noise, and it would sit as the newest user message.
        if runtime is not None:
            runtime_msg = self.render_runtime_context(runtime)
            if runtime_msg.strip():
                variable_parts.append(runtime_msg)
        footer = self.render_turn_footer(turn)
        if footer.strip():
            variable_parts.append(footer)
        if variable_parts:
            framed = (
                "[Injected context — automated state from the local Daintree runtime, "
                "not the user speaking. Continue the turn above using it as current state.]\n\n"
                + "\n\n".join(variable_parts)
            )
            messages.append({"role": "user", "content": framed})

        # "text" is the upstream default, so forwarding it is a no-op — except that
        # the model rejects ANY response_format alongside tools ("You cannot specify
        # response format and function call at the same time"). Only "json_object"
        # changes behavior, and validation rejects that in combination with tools.
        response_format = None
        if gen.response_format == "json_object":
            response_format = {"type": "json_object"}

        # Thinking is hardcoded OFF for the main call (posture lives with the model
        # ids in config.py — this workload routes requests onto skill runbooks and
        # emits tool calls, which reasoning did not measurably improve while costing
        # seconds of first-byte latency).
        #
        # The forced-tool_choice guard stays AND-ed in rather than replaced: it is an
        # independent upstream constraint (DeepSeek 400s on thinking + forced
        # tool_choice), so keeping it here means flipping the posture back on cannot
        # silently reintroduce that failure.
        thinking_enabled = INTERACTIVE_THINKING_ENABLED and not self._is_forced_tool_choice(
            request.input.tool_choice
        )

        # max_tokens policy: fill the default when the client omits one, then clamp to a
        # hard ceiling either way so a degenerate turn (the observed prompt-recitation
        # dump) can't run unbounded. The completion budget is shared by reasoning +
        # prose + tool-call arguments, so the default is deliberately NOT compact — a
        # low default amputates multi-spawn tool batches mid-arguments
        # (finish_reason=length) and forces a strictly more expensive retry round.
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
            # Hardcoded OFF (see above). If the posture is flipped back on,
            # reasoning_content round-trips through the wire contract so tool-call
            # turns can replay it. Thinking ignores temperature/top_p.
            thinking_disabled=not thinking_enabled,
            reasoning_effort=OPENROUTER_REASONING_EFFORT if thinking_enabled else None,
            timeout=self.settings.main_timeout_seconds,
            # The caller's endpoint-routing preference, normalized once here so
            # every downstream provider call for this turn routes identically.
            routing=routing_preference(request.routing),
        )
