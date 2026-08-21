"""Typed request-context and response-metadata models for the Daintree-native envelope.

These are first-class fields of ``POST /v1/daintree/respond`` (not an OpenAI add-on):
the request carries stable ``startup`` data, fresh ``runtime``/``turn`` context, and the
``selection`` policy. The response returns skill/selection metadata in the ``skills``
block (non-streaming) or first SSE ``meta`` event (streaming). See
``docs/DAINTREE_API.md``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------
# Request extension (section 6.4).
# --------------------------------------------------------------------------


class ClientInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=128)
    version: str | None = Field(default=None, max_length=64)
    platform: str | None = Field(default=None, max_length=64)


class McpInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connected: bool = False
    transport: str | None = Field(default=None, max_length=64)
    tool_count: int | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, max_length=64)


class McpServer(BaseModel):
    """One MCP server the CLI is connected to, as the CLI reports it.

    Server-rendered into a framed user-role row of the main prompt as inert data,
    below the tool schemas (issue #39). The request is
    strict (``extra="forbid"``): new MCP fields require a contract/protocol bump,
    not a silent drop.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=128)
    transport: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=64)
    tool_count: int | None = Field(default=None, ge=0)
    description: str | None = Field(default=None, max_length=4096)
    # Free-form operating notes for this server (e.g. quick paths, gotchas).
    instructions: str | None = Field(default=None, max_length=16384)


class OpenTerminal(BaseModel):
    """One live Daintree terminal in the per-request open-terminal inventory.

    The CLI attaches a fresh, metadata-only snapshot (no terminal output) of the
    open terminals to every request so the model always sees the live roster as
    inert data instead of tool-calling ``terminal.list`` mid-turn to discover it.
    Strict (``extra="forbid"``): a new terminal field is a contract change, not a
    silent drop. ``exit_code`` is nullable because 0 is a meaningful clean exit.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=256)
    kind: str | None = Field(default=None, max_length=64)
    worktree_id: str | None = Field(default=None, max_length=4096)
    title: str | None = Field(default=None, max_length=512)
    agent_id: str | None = Field(default=None, max_length=256)
    agent_state: str | None = Field(default=None, max_length=64)
    waiting_reason: str | None = Field(default=None, max_length=512)
    exit_code: int | None = None


class ProjectSnapshot(BaseModel):
    """Curated, session-stable project identity from ``project.getCurrent``."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, max_length=256)
    name: str | None = Field(default=None, max_length=512)
    path: str | None = Field(default=None, max_length=4096)
    status: str | None = Field(default=None, max_length=64)
    daintree_config_present: bool | None = None
    in_repo_settings: bool | None = None


class AgentSnapshot(BaseModel):
    """One registered direct-agent candidate with narrow, non-secret metadata.

    Agent ids are kept intact so the model can pass the exact identifier back to
    ``agent.launch``.  The renderer omits a whole row when the aggregate catalog
    budget is exhausted; it never truncates an id into a different identifier.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=16384)
    display_name: str | None = Field(default=None, max_length=512)
    source: Literal["built-in", "user", "plugin"]
    availability: Literal[
        "missing", "installed", "ready", "blocked", "unauthenticated"
    ] | None = None
    installed: bool | None = None
    launchable: bool | None = None
    pinned: bool | None = None
    toolbar_visible: bool | None = None


class AgentRosterSnapshot(BaseModel):
    """A successful agent-registry read, including its completeness markers."""

    model_config = ConfigDict(extra="forbid")

    agents: list[AgentSnapshot] = Field(default_factory=list, max_length=512)
    complete: bool = False
    availability_complete: bool = False
    total_count: int = Field(default=0, ge=0)


class StartupContext(BaseModel):
    """Session-stable Daintree data repeated before the visible conversation.

    This is deliberately separate from ``input.messages``: it reaches the main
    model as a framed data row but is never mistaken for a user utterance by the
    skill selector or the skill-learning transcript.
    """

    model_config = ConfigDict(extra="forbid")

    project: ProjectSnapshot | None = None
    agent_roster: AgentRosterSnapshot | None = None
    project_instructions: str | None = None


class WorktreeSnapshot(BaseModel):
    """The useful non-null portion of ``worktree.getCurrent``."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, max_length=4096)
    path: str | None = Field(default=None, max_length=4096)
    branch: str | None = Field(default=None, max_length=512)
    is_main: bool = False
    issue_number: int | None = None
    issue_title: str | None = Field(default=None, max_length=512)
    pr_number: int | None = None
    pr_title: str | None = Field(default=None, max_length=512)
    pr_url: str | None = Field(default=None, max_length=4096)
    status: str | None = Field(default=None, max_length=64)
    last_commit: str | None = Field(default=None, max_length=1024)


class CurrentWorktreeSnapshot(BaseModel):
    """A successful worktree read; ``current=None`` means definitively none."""

    model_config = ConfigDict(extra="forbid")

    current: WorktreeSnapshot | None = None


class DisplayInfo(BaseModel):
    """How wide the reply will actually render, in terminal cells (issue #33).

    ``content_width`` is the load-bearing value: the measure the assistant's own
    markdown is WRAPPED at, after the cockpit's left inset, its autowrap gutter
    and its prose cap. ``columns`` is the raw window, carried so the model can
    answer a question about the terminal it is running in without having to
    infer one number from the other.

    The CLI omits the whole block when it has no terminal to measure — a piped
    one-shot, the stdio host, the headless daemon. Absent therefore means
    UNKNOWN, never NARROW: the server falls back to ``MAIN_TERMINAL_WIDTH``
    rather than shaping a reply for a width nobody reported.

    Bounds mirror the CLI's own clamp (``displayWidthMax``, 10000): the request
    is validated BEFORE it is used, so an absurd width from a confused terminal
    probe would 422 the turn rather than quietly degrade.
    """

    model_config = ConfigDict(extra="forbid")

    columns: int | None = Field(default=None, ge=0, le=10_000)
    content_width: int | None = Field(default=None, ge=0, le=10_000)


class RuntimeContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    permission_tier: str | None = Field(default=None, max_length=64)
    mcp: McpInfo | None = None
    # The CLI's actual integration surface, sent per request so the server need
    # not hardcode (and drift from) the tool reference. Rendered as inert data.
    mcp_servers: list[McpServer] = Field(default_factory=list)
    scheduler_active: bool = True
    # ``None`` means the live read was unavailable. A present wrapper whose
    # ``current`` is None means Daintree definitively reported no active worktree.
    worktree: CurrentWorktreeSnapshot | None = None
    # Fresh per-request snapshot of the open terminals (issue #286): the live
    # roster the CLI fetched this turn, rendered as inert data so the model can
    # see what is open without a mid-turn terminal.list discovery call.
    open_terminals: list[OpenTerminal] = Field(default_factory=list)
    # The live render geometry of the surface this reply is drawn in. Republished
    # by the CLI on every resize, so a dragged window lands on the next round.
    # Read by the display-context tail block and the output lint alike — see
    # ``services.output_policy.resolve_content_width``.
    display: DisplayInfo | None = None


class Memories(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pinned: list[str] = Field(default_factory=list)
    relevant: list[str] = Field(default_factory=list)


class WorkflowDigest(BaseModel):
    """A bounded summary of one CLIENT-owned durable workflow graph.

    The CLI is the source of truth for the full execution graph (nodes, edges,
    resources, evidence); it sends only these compact digests per turn so the
    main model sees its long-running work without the server ever storing
    workflow state. Strict (``extra="forbid"``): a new digest field is a
    contract change in both repos, not a silent drop.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(max_length=128)
    goal: str = Field(max_length=512)
    status: str = Field(max_length=64)
    progress: str | None = Field(default=None, max_length=512)
    active_nodes: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    next_action: str | None = Field(default=None, max_length=512)
    last_event: str | None = Field(default=None, max_length=512)


class TurnContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str | None = None
    is_wake: bool = False
    workflow_runs: list[str] = Field(default_factory=list)
    # Live runtime-owned async operations (terminal.run.async /
    # terminal.await.async), one pre-formatted line each — rendered so the model
    # always sees its in-flight async work and never re-issues it.
    async_operations: list[str] = Field(default_factory=list)
    memories: Memories = Field(default_factory=Memories)
    # Live watchers the CLI ADOPTED from a prior owner at ownership boot
    # (project-scoped supervision resumed automatically) — sent once, on the
    # first turn. Replaces the pre-supervisor session_ended_watchers field.
    resumed_watchers: list[str] = Field(default_factory=list)
    # Workflow-intelligence digests (client-owned execution graphs), rendered
    # into the turn footer. Capped hard at assembly (count, per-line bytes, and
    # a total block budget) — see PromptAssembler.render_turn_footer.
    workflow_state: list[WorkflowDigest] = Field(default_factory=list)


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: Literal["new_instruction", "always"] = "new_instruction"
    force: bool = False
    # Skills the CALLER requires this turn, whatever the classifier thinks.
    #
    # `force` means "re-run the selector this round", never "select this skill",
    # so before this field there was no way to say the latter. The case it exists
    # for is skill DEVELOPMENT: exercising a runbook you are rewriting against a
    # real turn, instead of reverse-engineering a prompt that talks the selector
    # into picking it.
    #
    # It rides ALONGSIDE selection rather than replacing it — the realistic test
    # is the runbook plus whatever else the turn genuinely needs — and it is
    # admitted FIRST, foundation-paired, against `max_active_skills`. See
    # `resolve_active_skills`, which owns every one of those rules.
    #
    # Ids that are unknown, or not executable under this request's `profile`, are
    # dropped and REPORTED in `warnings`; they are never a 422. Whether a pin fits
    # depends on the live catalog, the foundation topology and the configured cap,
    # none of which a request-shape validator can see.
    #
    # Gated by `skills.pinned_skill_ids` in /v1/daintree/capabilities: this model
    # is extra="forbid", so sending the field to a deployment that predates it
    # would 422 the whole turn.
    pinned_skill_ids: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Shared response-metadata building blocks (reused by the native API's
# first-class skills block in :mod:`.daintree_api`).
# --------------------------------------------------------------------------


class SkillRef(BaseModel):
    id: str
    title: str


class PreludeToolCallFunction(BaseModel):
    name: str
    arguments: str


class PreludeToolCall(BaseModel):
    id: str
    type: Literal["function"] = "function"
    function: PreludeToolCallFunction


class PreludeToolResult(BaseModel):
    role: Literal["tool"] = "tool"
    tool_call_id: str
    content: str


class PreludeExecution(BaseModel):
    call: PreludeToolCall
    result: PreludeToolResult
    display_name: str


class Prelude(BaseModel):
    tool_executions: list[PreludeExecution] = Field(default_factory=list)


class SelectorUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    # USD charged to the caller for the selector call. See RespondUsage.cost —
    # this is deliberately not folded into the turn's main figure.
    cost: float | None = None


class SelectorMeta(BaseModel):
    ran: bool = False
    degraded: bool = False
    task_type: str | None = None
    confidence: float | None = None
    usage: SelectorUsage | None = None
    # Free-text reason is omitted by default; only present under a debug flag.
    reason: str | None = None

