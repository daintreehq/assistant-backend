"""The fork plan: what the main model decides when it forks a terminal spawn.

A fan-out of four agent terminals is expensive in exactly one place — the four
detailed ``taskPrompt`` values, which the main model writes one after another on
the turn's critical path. This contract is the compact thing it writes instead:
one shared goal, four short briefs, and every control-plane value a prompt
expander must not be allowed to guess.

**The split between what the plan carries and what an expander may write is the
whole safety story.** ``agentId``, ``worktreeId``, ``mode`` and the watcher
decision select where an agent runs and what authority it holds; those are the
primary model's decisions, made with the user's confirmation in view, and the
backend re-imposes them over whatever the expander returned
(``effective_controls``). Prose — the title, the task prompt, the acceptance
criteria, the watcher goal — is what expansion is for.

Every one of those four controls must RESOLVE, from the branch or from the
plan's defaults, or the plan is refused. That includes ``watch``, which has an
obvious safe default and deliberately does not get one: defaulting it would mean
the backend, not the primary model, decided a fleet of four agents runs
unsupervised. An invalid control plan fails closed; there is nothing here the
server is entitled to guess.

Identifiers are taken VERBATIM — a value carrying surrounding whitespace is
rejected rather than trimmed, matching how this server already treats a padded
tool-call id. Trimming would mean validating one string and sending another, and
here the string being sent selects a mutable checkout.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

#: Exactly four. Not a default, not a minimum — the fixed shape of the first
#: implementation. A variable count is a deliberate later decision (the expander
#: prompt, the fallback templates and the latency argument are all written for
#: four), and a bound that reads as configurable invites it to be treated as such.
FORK_BRANCH_COUNT = 4

#: Identifier bounds, taken from the contracts these values are COPIED FROM
#: rather than invented here. An agent id reaches the model through
#: ``AgentSnapshot.id`` (16384) and a worktree id through ``WorktreeSnapshot.id``
#: (4096); a tighter cap here would refuse a plan whose only fault was quoting
#: the roster exactly, which is the one thing the prompt asks it to do.
MAX_AGENT_ID_CHARS = 16_384
MAX_WORKTREE_ID_CHARS = 4_096

SpawnMode = Literal["edit", "explore"]


def _verbatim(value: str) -> str:
    """Reject a blank or whitespace-padded identifier rather than trimming it.

    ``" wt_backend "`` and ``"wt_backend"`` are different strings and the same
    worktree, so trimming would validate one and send the other — and would let
    two branches carrying different paddings of one id past the disjointness
    check below. A whitespace-only id is worse still: the CLI reads an empty
    ``worktreeId`` as absent and falls back to the ACTIVE worktree, so four
    blanks are four agents in one checkout.
    """
    if not value.strip() or value != value.strip():
        raise ValueError("must not be blank or carry surrounding whitespace")
    return value


def _prose(min_chars: int) -> AfterValidator:
    """Require ``min_chars`` of actual content, not of padding.

    ``min_length`` counts spaces, so an eight-space ``sharedGoal`` satisfies it
    and then tells the expander nothing at all. The deterministic fallback reads
    these fields when a branch's model call fails, so a blank one is a spawn with
    no instructions rather than a cosmetic problem.
    """

    def check(value: str) -> str:
        if len(value.strip()) < min_chars:
            raise ValueError(f"must carry at least {min_chars} non-whitespace characters")
        return value

    return AfterValidator(check)


AgentId = Annotated[
    str, Field(min_length=1, max_length=MAX_AGENT_ID_CHARS), AfterValidator(_verbatim)
]
WorktreeId = Annotated[
    str, Field(min_length=1, max_length=MAX_WORKTREE_ID_CHARS), AfterValidator(_verbatim)
]
ContextPath = Annotated[str, Field(min_length=1, max_length=4_096), AfterValidator(_verbatim)]


class ForkDefaults(BaseModel):
    """Values shared by every branch, so the plan states them once.

    Purely an authoring convenience for the main model: a branch's own field
    always wins, and ``effective_*`` below is the only thing that reads either.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    agent_id: AgentId | None = Field(default=None, alias="agentId")
    mode: SpawnMode | None = None
    watch: bool | None = None


class ForkBranch(BaseModel):
    """One terminal to spawn: its exact target, plus a compact brief to expand.

    ``brief`` is deliberately capped well below a real task prompt. It is meant
    to carry outcome + owned scope + exclusion + done signal — enough for the
    deterministic fallback to synthesize a usable prompt without a model, and
    not so much that the main model has written the worker prompt anyway, which
    is the cost this whole feature exists to remove.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    # A short slug rather than free text: it names the branch in the assignment
    # message, seeds the synthesized call id, and is the deterministic
    # disambiguator for duplicate titles. All three want a value that is stable,
    # comparable and safe to interpolate.
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,47}$")
    worktree_id: WorktreeId = Field(alias="worktreeId")
    brief: Annotated[str, Field(min_length=8, max_length=2_000), _prose(8)]

    agent_id: AgentId | None = Field(default=None, alias="agentId")
    mode: SpawnMode | None = None
    watch: bool | None = None

    title_hint: str | None = Field(default=None, alias="titleHint", max_length=80)
    context_file_paths: tuple[ContextPath, ...] = Field(
        default=(),
        alias="contextFilePaths",
        max_length=32,
    )
    acceptance_hint: str | None = Field(default=None, alias="acceptanceHint", max_length=2_000)
    watch_goal_hint: str | None = Field(default=None, alias="watchGoalHint", max_length=1_000)


class ForkSpawnPlan(BaseModel):
    """The complete argument object of ``daintree_internal__fork_spawn_terminals``.

    The collection fields are TUPLES rather than lists, and that is a real
    guarantee rather than tidiness: ``frozen=True`` stops attribute
    reassignment, not list mutation, so a validated plan holding a list could
    have a fifth branch appended after every invariant here had passed. The
    expansion service holds this object across four concurrent calls; it must
    still describe four branches at the end of them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    shared_goal: Annotated[str, Field(min_length=8, max_length=4_000), _prose(8)] = Field(
        alias="sharedGoal"
    )
    shared_constraints: tuple[str, ...] = Field(
        default=(),
        alias="sharedConstraints",
        max_length=16,
    )
    defaults: ForkDefaults = Field(default_factory=ForkDefaults)
    branches: tuple[ForkBranch, ...] = Field(
        min_length=FORK_BRANCH_COUNT, max_length=FORK_BRANCH_COUNT
    )

    @model_validator(mode="after")
    def _validate_semantics(self) -> ForkSpawnPlan:
        ids = [branch.id for branch in self.branches]
        if len(ids) != len(set(ids)):
            raise ValueError("branch ids must be unique")

        for branch in self.branches:
            if branch.agent_id is None and self.defaults.agent_id is None:
                raise ValueError(f"branch {branch.id!r} has no effective agentId")
            if branch.mode is None and self.defaults.mode is None:
                raise ValueError(f"branch {branch.id!r} has no effective mode")
            # No safe default, deliberately. Supervision is a decision the
            # fan-out runbook requires be made once for the whole fleet, and a
            # backend that filled it in would be choosing "four unsupervised
            # agents" on the primary model's behalf.
            if branch.watch is None and self.defaults.watch is None:
                raise ValueError(f"branch {branch.id!r} has no effective watch")

        # ONE WORKTREE PER BRANCH — all four, with no carve-out for read-only
        # branches. `mode: "explore"` is a line in the agent's PROMPT, not
        # filesystem isolation: an explore agent shares the checkout and can
        # still write to it, so "two explorers may share" would be a rule the
        # runtime does not enforce. It matches the fan-out runbook, which says
        # one worktree per unit without qualifying it by mode.
        #
        # Casefolded, because the collision this prevents is two agents in one
        # DIRECTORY and the common development filesystem (macOS) is
        # case-insensitive. It over-rejects on a case-sensitive one, and a
        # refused plan is the cheap side of that trade.
        #
        # What it CANNOT see is an alias: `"main"` and `"/repo/main"` are one
        # worktree to the CLI and two strings here. The tool description and the
        # base prompt both require already-resolved ids for that reason — this
        # check is the backstop for identical spellings, not a resolver.
        worktrees = [branch.worktree_id.casefold() for branch in self.branches]
        if len(worktrees) != len(set(worktrees)):
            raise ValueError("each branch must have its own worktree")
        return self


def effective_agent_id(plan: ForkSpawnPlan, branch: ForkBranch) -> str:
    """The branch's agent, or the plan's.

    The guard is not dead code: ``model_construct`` and ``model_copy(update=…)``
    both skip validation, so a caller inside this server can hand these helpers
    a plan ``_validate_semantics`` never saw. Raising beats returning a guess,
    because the value selects which agent gets launched.
    """
    resolved = branch.agent_id or plan.defaults.agent_id
    if resolved is None:
        raise ValueError(f"branch {branch.id!r} has no effective agentId")
    return resolved


def effective_mode(plan: ForkSpawnPlan, branch: ForkBranch) -> SpawnMode:
    """The branch's mode, or the plan's. Raises for the reason above."""
    resolved = branch.mode or plan.defaults.mode
    if resolved is None:
        raise ValueError(f"branch {branch.id!r} has no effective mode")
    return resolved


def effective_watch(plan: ForkSpawnPlan, branch: ForkBranch) -> bool:
    """Whether this branch is supervised. Raises rather than defaulting.

    Symmetrical with the other two on purpose. ``False`` is the safe direction
    for authority, which is exactly why it is tempting to default to — and
    defaulting would quietly convert "the model forgot" into "the fleet runs
    unwatched", which is a product failure rather than a security one and so
    would never show up as an alarm.
    """
    resolved = branch.watch if branch.watch is not None else plan.defaults.watch
    if resolved is None:
        raise ValueError(f"branch {branch.id!r} has no effective watch")
    return resolved


def effective_controls(plan: ForkSpawnPlan, branch: ForkBranch) -> dict[str, object]:
    """The fields the backend owns, in the spawn tool's own spelling.

    Merged OVER whatever the expander returned. Every key here selects where the
    agent runs or what it is allowed to do; none of them is prose, and none of
    them is a thing a model reading untrusted terminal output should be able to
    move.

    ``context.filePaths`` rides along only when the plan supplied paths — an
    absent key means the primary model expressed no opinion, and an expander
    naming files it read is adding useful context rather than seizing authority.
    """
    controls: dict[str, object] = {
        "agentId": effective_agent_id(plan, branch),
        "worktreeId": branch.worktree_id,
        "mode": effective_mode(plan, branch),
        "watch": effective_watch(plan, branch),
    }
    if branch.context_file_paths:
        controls["context"] = {"filePaths": list(branch.context_file_paths)}
    return controls
