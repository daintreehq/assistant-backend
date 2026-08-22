"""Assemble the cache-aware upstream main-model request.

The DeepSeek chat template serializes every system message before tools and non-system
messages (unchanged by routing through OpenRouter — it is the model's template, not the
API's), so the system role is reserved for content that is static for EVERY caller. The
rows below it are ordered by how widely each can be shared, widest first, so a change to
one never re-encodes anything above it. The effective order is:

1. base prompt → response contract (the stable system region; fully static)
2. tool schemas (the top-level ``tools`` field, ~21.5k tokens — the largest reusable run)
3. active skill bodies (one framed user-role data row, omitted when empty) — shared by
   TASK, so every session running the same runbook shares this much of the prefix
4. integrations → stable startup context (framed user-role data rows, omitted when empty)
   — per session, instance and project
5. visible append-only conversation
6. volatile runtime + display + turn context (one framed user-role tail; the display
   fragment always renders, so the tail is always present)

Rows 3-5 are non-system precisely so they land AFTER the tool schemas: a selector topic
shift or a per-machine MCP endpoint URL then costs its own bytes and the rows below it,
never the 21.5k tokens of schemas above (issue #39). Skill bodies stay ahead of the
conversation rather than riding the volatile tail — a large runbook in the tail is re-paid
uncached on every round of every turn, far worse than one conversation re-encode on the
turns where the active set genuinely changes.

Stable startup facts therefore sit on the cache-friendly side of the conversation without
masquerading as a user-authored message in the public transcript, selector, or learner.
All client context is untrusted: fields are sanitized and bounded before rendering.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from ..config import INTERACTIVE_REASONING_EFFORT, INTERACTIVE_THINKING_ENABLED, Settings
from ..contracts.daintree_api import (
    COMPACTION_BLOCK_NAME,
    RespondRequest,
    RespondToolChoice,
)
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
    TEMPLATE_DISPLAY_CONTEXT,
    TEMPLATE_DOCS_CONTEXT,
    TEMPLATE_INTEGRATIONS,
    TEMPLATE_LOADED_SKILLS,
    TEMPLATE_RESPONSE_CONTRACT,
    TEMPLATE_RUNTIME_CONTEXT,
    TEMPLATE_STARTUP_CONTEXT,
    TEMPLATE_TURN_FOOTER,
    PromptLoader,
    RenderCache,
)
from ..providers.base import ProviderChatRequest
from ..skills.models import Skill
from ..text_utils import (
    cap_bytes,
    frame_data_block,
    frame_value,
    message_text,
    sanitize_line,
    utf8_len,
)
from .docs_mcp import DocsLookup, render_docs_sections
from .output_policy import WIDE_TERMINAL_WIDTH, max_table_columns, resolve_content_width

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
    """THE canonical order the loaded-skills block is rendered in.

    Foundations first, so a domain's base judgment reads before the workflow
    steps that build on it; id order within each tier, so EVERY permutation of
    the same active set renders identical bytes. The tie-break is what makes
    that true. Skills arrive in the selector's raw output order, and sorting on
    the foundation flag alone is a stable sort on one boolean: it passed that
    order straight through, so the same set classified in a different order
    re-encoded everything from the loaded-skills row down — the integrations,
    startup and conversation rows behind it — for zero semantic difference. (The
    tool schemas now sit ABOVE that row and survive it; before issue #39 moved the
    block below them, a bare permutation cost those too.)

    Id order is also catalog order — ``Catalog`` stores its skills id-sorted —
    so this invents no new convention.

    Render order only — the active LIST keeps selector rank, which is load-
    bearing for the state token, the response metadata, and the next turn's
    selection. Nothing here deduplicates: the active set is already unique by
    construction, and quietly repairing a duplicate would hide that break.

    """
    return sorted(skills, key=lambda s: (not s.meta.foundation, s.id))


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

    def __init__(
        self,
        prompt_loader: PromptLoader,
        settings: Settings,
        *,
        response_declaration: bool = False,
    ) -> None:
        self.prompts = prompt_loader
        self.settings = settings
        # Whether the base prompt carries the `[[DAINTREE:FINAL]]`/`[[DAINTREE:TOOLS]]`
        # rule. Defaults OFF to match the shipped compactor (`NullCompactor`): the
        # control line is only worth its bytes when something reads it, and the
        # container turns it on from `compactor.available` when a producer exists.
        # Deployment-wide, never per request — a prompt that varied by caller would
        # split the prefix cache on its very first block.
        self.response_declaration = response_declaration
        # Bounded: a runaway key space would be a slow memory leak in a process
        # that is meant to be stateless. Sized well above the number of distinct
        # active-skill sets a busy instance sees.
        self._loaded_skills_cache = RenderCache(maxsize=64)

    # ---- prompt fragment rendering --------------------------------------

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

        def prose(value: str | None, cap: int) -> str:
            """Frame a worktree field that carries free text.

            Issue titles, PR titles and commit subjects are written by whoever
            opened the issue or pushed the branch — not necessarily the user — so
            they are the worktree's genuinely attacker-reachable fields, each with
            512-1024 bytes of room. Branch names and paths join them because a
            branch can be named anything. Short enumerated values (`status`, the
            numeric issue/PR ids) stay bare: no room for an imperative, and
            framing them would only add noise to a line the model reads every turn.
            """
            return frame_value(sanitize_line(value, cap), tag="w")

        if branch := prose(wt.branch, 512):
            parts.append(f"branch {branch}")
        if wt.is_main:
            parts.append("main worktree")
        if wt.issue_number is not None:
            issue = f"issue #{wt.issue_number}"
            if title := prose(wt.issue_title, 512):
                issue += f" {title}"
            parts.append(issue)
        elif title := prose(wt.issue_title, 512):
            parts.append(f"issue {title}")
        if wt.pr_number is not None:
            pr = f"PR #{wt.pr_number}"
            if title := prose(wt.pr_title, 512):
                pr += f" {title}"
            parts.append(pr)
        elif title := prose(wt.pr_title, 512):
            parts.append(f"PR {title}")
        for label, value, cap, framed in (
            ("PR URL", wt.pr_url, 4096, True),
            ("status", wt.status, 64, False),
            ("last commit", wt.last_commit, 1024, True),
            ("id", wt.id, 4096, True),
            ("path", wt.path, 4096, True),
        ):
            clean = prose(value, cap) if framed else sanitize_line(value, cap)
            if clean:
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
                # An agent can rename its OWN terminal, so a title is the one
                # runtime field an attacker downstream of the user controls
                # directly — it writes into the supervisor's prompt. Same for the
                # waiting reason. Both carry 512 bytes of free text; the short
                # enumerated fields beside them do not and stay bare.
                "title": frame_value(sanitize_line(t.title, 512), tag="t"),
                "agent_id": sanitize_line(t.agent_id, 256),
                "agent_state": sanitize_line(t.agent_state, 64),
                "waiting_reason": frame_value(
                    sanitize_line(t.waiting_reason, 512), tag="t"
                ),
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
        sending its integration surface) produces no integrations row at all.

        Rides a framed user-role row BELOW the tool schemas (issue #39). The CLI
        leads each server description with its MCP endpoint URL — a per-machine,
        per-instance value — so while this was system-role every distinct endpoint
        re-encoded the ~21.5k tokens of schemas behind it. It sits after the skill
        bodies too: those are shared by task, this only by session and instance.
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

    def render_loaded_skills(self, skills: list[Skill], *, step_tracking: bool = True) -> str:
        """The active skill bodies, rendered — memoised by content.

        This is the largest per-request render in the assembler (skill bodies run
        to tens of kilobytes), and it is a pure function of WHICH skills are
        active: `skills_in_render_order` canonicalizes first, so every
        permutation of one set produces the same bytes and shares one entry
        rather than paying for a duplicate. Sessions converge on a handful of
        skill sets, so the hit rate is high.

        Keyed on each skill's CONTENT hash, not its id. Ids are stable across a
        skill-learning rewrite while the body changes underneath, so an id-keyed
        cache would serve the pre-rewrite body until the process restarted — the
        one failure mode a cache here could actually cause.

        Rides a framed user-role row placed directly below the tool schemas and
        above every other injected row (issue #39). This is the most widely shared
        of the variable rows — sessions converge on a handful of skill sets, and any
        two running the same runbook share the prefix through it — so it earns the
        position nearest the schemas. Role and position are decided at the
        ``build_main_request`` call site; the cache key here is a pure function of
        WHICH skills are active and is unaffected by either.
        """
        ordered = skills_in_render_order(skills)
        # Flat tuple of strings, which is what RenderCache is keyed on. The
        # step_tracking marker leads: it is part of the KEY, not just the render,
        # and a key that ignored it would serve whichever variant was rendered
        # first to both profiles.
        key = ("step" if step_tracking else "nostep", *(s.content_hash for s in ordered))
        cached = self._loaded_skills_cache.get(key)
        if cached is not None:
            return cached
        rendered = self.prompts.render(
            TEMPLATE_LOADED_SKILLS, loaded_skills=ordered, step_tracking=step_tracking
        )
        self._loaded_skills_cache.put(key, rendered)
        return rendered

    def render_response_contract(self) -> str:
        """The output-shape RULES — the FINAL system block, and so the last thing
        before the tool schemas (see build_main_request's cache-critical layout).

        Request-independent by signature and by body: identical bytes for every
        caller, every turn. It used to interpolate the resolved width, the
        narrow/wide branch and the table-column cap, which put a per-request value
        in the system region — ahead of ~21.5k tokens of tool schemas — so a window
        resize re-encoded all of them and two clients on different widths shared
        nothing past the base prompt (issue #38). The concrete geometry now rides
        the volatile tail (``render_display_context``); the rules that reference it
        stay here, phrased without a number so they never move.
        """
        return self.prompts.render(TEMPLATE_RESPONSE_CONTRACT)

    def render_display_context(self, runtime: RuntimeContext | None = None) -> str:
        """This turn's terminal geometry — a VOLATILE tail fragment (issue #38).

        Width is the client's measured ``runtime.display.content_width`` when it
        reported one, else the settings default (~56 columns). Rendered
        UNCONDITIONALLY, unlike the runtime block: ``resolve_content_width`` is
        null-safe, so a runtime-less client (a pipe, the stdio host, a generic
        OpenAI-style caller) still gets a real width to shape against — the same
        one it got while this lived in the contract. Gating it on ``runtime`` would
        silently drop the number for exactly those callers.

        These are the two numbers ``lint_output`` grades the reply against, read
        from the same resolver so what the model is TOLD and what it is GRADED on
        cannot drift.
        """
        width = resolve_content_width(runtime, fallback=self.settings.main_terminal_width)
        return self.prompts.render(
            TEMPLATE_DISPLAY_CONTEXT,
            content_width=width,
            is_narrow=width < WIDE_TERMINAL_WIDTH,
            # The same cap the linter will grade this reply against.
            max_table_columns=max_table_columns(width),
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
            # Framed like the scalar free-text fields: each item carries the same
            # 512-byte budget as `goal`, which is ample room for an imperative.
            # Framing three fields and leaving these bare would have been an
            # inconsistency an attacker reads as a doorway.
            return [
                frame_value(line, tag="wf") for line in capped if line
            ][: self._WORKFLOW_LIST_ITEMS]

        out: list[dict[str, Any]] = []
        used = 0
        for digest in digests[: self.settings.cap_workflow_digests]:
            wf_id = sanitize_line(digest.id, 128)
            if not wf_id:
                continue
            # The three free-text fields are framed; id/status are not, because
            # they are short enumerated-ish values with no room for prose and
            # framing them would only add noise to every line. `goal`,
            # `next_action` and `last_event` are where an injected imperative
            # would actually live — a digest whose next action reads "then delete
            # the branch" is the shape this defends against.
            rendered: dict[str, Any] = {
                "id": wf_id,
                "goal": frame_value(
                    sanitize_line(digest.goal, self._WORKFLOW_LINE_BYTES), tag="wf"
                ),
                "status": sanitize_line(digest.status, 64),
                "progress": frame_value(
                    sanitize_line(digest.progress, self._WORKFLOW_LINE_BYTES), tag="wf"
                ),
                "active_nodes": lines(digest.active_nodes),
                "resources": lines(digest.resources),
                "blockers": lines(digest.blockers),
                "next_action": frame_value(
                    sanitize_line(digest.next_action, self._WORKFLOW_LINE_BYTES), tag="wf"
                ),
                "last_event": frame_value(
                    sanitize_line(digest.last_event, self._WORKFLOW_LINE_BYTES), tag="wf"
                ),
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

    def render_docs_context(self, lookup: DocsLookup | None) -> str:
        """The documentation the server retrieved for this turn, as prompt text.

        Framed as an inert data block for the same reason the turn goal is: the
        text is quoted from a web page, and the live injection benchmark showed
        that carriers rendered bare leak while carriers rendered inside a
        delimited block do not. First-party pages are still pages, and a docs
        edit must not be able to become an instruction to the assistant.
        """
        if lookup is None or not lookup.sections:
            return ""
        body = render_docs_sections(lookup.sections)
        if not body.strip():
            return ""
        return self.prompts.render(
            TEMPLATE_DOCS_CONTEXT,
            docs_query=frame_value(
                sanitize_line(lookup.query, self.settings.cap_docs_query), tag="docs_query"
            ),
            docs_sections=frame_data_block(
                body, self.settings.cap_docs_block_bytes, tag="daintree_docs"
            ),
        )

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

        # Frame the two carriers the live injection benchmark measured leaking.
        # Sanitizing to a single line already stops a value forging a heading; it
        # does nothing about the value simply reading as prose the model obeys.
        # The two carriers rendered inside a delimited block (project and MCP
        # instructions) held across trials, and the two rendered bare — the turn
        # goal and the workflow digests — did not. This puts them behind the same
        # boundary.
        turn_context = {
            "goal": frame_value(goal, tag="goal"),
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

        # A compacted block the client spliced back in wears a reserved name. That
        # name is a marker BETWEEN the server and the client — it tells the next
        # turn's span selector where frozen history ends — and it has no business
        # upstream, where `name` reads as a speaker identity. Dropped here, so the
        # block travels as ordinary user content and costs nothing extra.
        if msg.name and msg.name != COMPACTION_BLOCK_NAME:
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

    def _build_subagent_request(
        self,
        *,
        request: RespondRequest,
        skills: list[Skill],
        upstream_model: str,
    ) -> ProviderChatRequest:
        """Assemble the upstream request for a read-only sub-agent turn.

        Deliberately the SHORT prompt. A sub-agent answers one question with
        read-only tools and reports back to a machine, so every block the
        orchestrator needs is either irrelevant to it or actively misleading:

        * **integrations / runtime roster / turn footer** — memories, workflow
          runs, async operations, and the open-terminal list describe work the
          orchestrator owns and the sub-agent cannot touch.
        * **display context** — the report is read by the orchestrator, not
          rendered in anyone's terminal, so a wrap width is noise.
        * **response contract** — the orchestrator's output rules are written for
          a human reading a cockpit; the sub-agent's report contract lives in its
          own persona prompt instead.

        Skills DO reach a sub-agent, from a menu narrowed to the ones it can
        actually execute (``Catalog.executable_with``). They are the reason the
        profile has a real tool inventory at all: a sub-agent sent to find one
        issue among thousands should get the runbook for searching issues, not
        rediscover it every run.

        The cache layout rule from :meth:`build_main_request` still holds, and
        the ordering below is the same one, minus the layers this profile drops:
        the ONLY system row is the static persona, so every sub-agent in the
        fleet shares a prefix of persona + its own narrow tool schemas, and the
        skill bodies sit below the schemas (user-role) where a menu shift
        re-encodes its own bytes instead of theirs. The startup block stays and
        sits last before the conversation, because a sub-agent sent to find a
        file needs to know which project it is standing in.
        """
        gen = request.generation
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.prompts.subagent_prompt()},
        ]
        if skills:
            # step_tracking OFF: the paragraph instructs `skill.step.advance` and
            # `skill.run.get`, both of which are denylisted from a sub-agent's
            # inventory. Shipping it would spend context telling a read-only worker
            # to make a call that can only come back TOOL_NOT_OFFERED — and a
            # sub-agent has no turn boundary to checkpoint across anyway; it runs to
            # completion or reports partial.
            messages.append(
                {"role": "user", "content": self.render_loaded_skills(skills, step_tracking=False)}
            )
        startup = self.render_startup_context(request.startup)
        if startup.strip():
            messages.append({"role": "user", "content": startup})
        messages.extend(self._message_to_wire(m) for m in request.input.messages)

        max_tokens = gen.max_tokens or self.settings.main_default_max_tokens
        max_tokens = min(max_tokens, self.settings.main_max_tokens_ceiling)

        return ProviderChatRequest(
            model=upstream_model,
            messages=messages,
            tools=self._project_tools(request.input.tools),
            tool_choice=self._project_tool_choice(request.input.tool_choice),
            temperature=gen.temperature,
            max_tokens=max_tokens,
            # Same rule as the assistant path rather than a hardcoded None: "text"
            # is the upstream default so forwarding it is a no-op, and validation
            # already rejects json_object alongside tools. Dropping a param the
            # validator ACCEPTED would leave a caller asking for JSON and quietly
            # getting prose.
            response_format=(
                {"type": "json_object"} if gen.response_format == "json_object" else None
            ),
            stop=gen.stop,
            stream=gen.stream,
            include_usage=gen.stream,
            # Same posture as the main call: this workload searches and reports,
            # and reasoning bought nothing measurable for the seconds it costs.
            thinking_disabled=True,
            reasoning_effort=None,
            timeout=self.settings.main_timeout_seconds,
        )

    def build_main_request(
        self,
        *,
        request: RespondRequest,
        skills: list[Skill],
        upstream_model: str,
        docs: DocsLookup | None = None,
    ) -> ProviderChatRequest:
        """Assemble the upstream request so the upstream prefix cache covers the base
        prompt, the response contract, the tool schemas, and then as much of the
        task/session/conversation prefix as each caller actually shares.

        CACHE-CRITICAL LAYOUT (measured 2026-07-08, latency benchmark, against
        DeepSeek directly; the template is the model's, so it survives the move to
        OpenRouter): the prompt serializes as [ALL system messages, in array order] →
        [tools] → [non-system conversation]. A system message ANYWHERE in the array
        therefore lands in the leading system region, BEFORE the ~21.5k-token tool
        schemas and the conversation. The old layout put the volatile runtime/turn
        context after the conversation as system messages — the template hoisted them
        into the system region, so every new session (and any mid-turn roster change)
        re-encoded tools + conversation: ~36% cache hit on identical back-to-back
        turns. The
        rule that follows: ONLY stable content may use role "system"; per-request
        variable context must ride a NON-system role at the very end, where it costs
        exactly its own tokens.

        The corollary the system region is now held to (issue #39): "stable" means
        stable for EVERY caller, not merely for one session. The integration surface
        carries a per-machine MCP endpoint URL and the skill bodies change on every
        selector topic shift, so while both were system-role each of those changes
        re-encoded all 21.5k tokens of schemas behind them. Both are data the model
        reads, exactly like the startup block, so both now ride framed user-role rows
        BELOW the schemas — ordered by shareability, widest first: skill bodies (shared
        by task) before integrations and startup (per session, instance and project).
        That grows the invariant prefix from the base prompt alone to base + contract +
        schemas, and makes a skill load cost its own bytes instead of the tool region.

        Skill bodies stay ahead of the conversation rather than joining the volatile
        tail: a 5k-token runbook in the tail is re-paid uncached on every round of every
        turn (~40k tokens across an eight-round turn), which is far worse than one
        conversation re-encode on the turns where the set genuinely changes.
        """
        runtime = request.runtime
        turn = request.turn
        gen = request.generation

        if request.profile == "subagent":
            return self._build_subagent_request(
                request=request, skills=skills, upstream_model=upstream_model
            )

        # 1. static base prompt → cacheable across all sessions.
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": self.prompts.base_prompt(
                    response_declaration=self.response_declaration
                ),
            },
        ]
        # 2. response contract: static output-shape rules, and the LAST system row.
        #    Genuinely static — the per-request width it used to carry moved to the
        #    tail (issue #38). Everything appended below must stay non-system, or the
        #    template hoists it back above the schemas and undoes the whole layout.
        contract = self.render_response_contract()
        if contract.strip():
            messages.append({"role": "system", "content": contract})

        # --- the top-level `tools` field serializes HERE: after the complete system
        # --- region and before every non-system row below. It is never a message.

        # 3. active skill bodies: shared by TASK, so they sit closest to the schemas —
        #    many sessions running the same runbook share the prefix up to this point.
        #    User-role (issue #39), so a selector topic shift re-encodes this row and
        #    what follows, and leaves the ~21.5k tokens of schemas above it cached.
        #    Kept ahead of the conversation rather than in the volatile tail: a large
        #    runbook in the tail is re-paid uncached every round of every turn.
        if skills:
            messages.append({"role": "user", "content": self.render_loaded_skills(skills)})
        # 4. integration surface (MCP servers): client-reported data carrying a
        #    per-machine endpoint URL, so it is per session and instance — narrower
        #    than the skill bodies and therefore below them, where a differing MCP
        #    config cannot split the task-shared prefix above.
        integrations = self.render_integrations(runtime)
        if integrations.strip():
            messages.append({"role": "user", "content": integrations})
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
        # This turn's terminal geometry (issue #38). ALWAYS rendered, unlike the
        # runtime block above: the width resolver falls back to the server default,
        # so a runtime-less client still gets the number it needs to shape output.
        # It is the one fragment that guarantees the tail exists at all.
        variable_parts.append(self.render_display_context(runtime))
        footer = self.render_turn_footer(turn)
        if footer.strip():
            variable_parts.append(footer)
        # Retrieved documentation rides the volatile tail with everything else
        # per-request. It is emphatically NOT a system message: it exists for
        # exactly one turn, and a system-role block would land ahead of the tool
        # schemas and re-encode ~21.5k tokens of them for a question the next turn
        # will not ask again (see this method's docstring).
        docs_block = self.render_docs_context(docs)
        if docs_block.strip():
            variable_parts.append(docs_block)
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
            reasoning_effort=INTERACTIVE_REASONING_EFFORT if thinking_enabled else None,
            timeout=self.settings.main_timeout_seconds,
        )
