"""Runbook-aware tool projection — computed every turn, enforced under a flag.

Tool schemas are ~61% of a representative request, and most of them are
irrelevant to what the turn is doing: a rename-terminals turn does not need the
forge, git or recipe families. Filtering them to the active runbooks' declared
needs is the single largest prompt saving available. Measured against the pinned
CLI inventory (79.3 KiB): 22.2 KiB on a runbook-less turn (72% saved) and 46.8 KiB
with a workflow runbook and its domain foundation loaded (41% saved). Issue #64's
~56% median sits between the two, which is what a mix of both shapes looks like.

**Three modes, one switch.** ``TOOL_PROJECTION_MODE`` is ``disabled`` (compute
nothing, send everything), ``shadow`` (compute and log, send everything) or
``enforced`` (send the projection when every safety gate passes). ``shadow`` is
the default, so a deployment that sets nothing behaves exactly as it did while
this was measurement-only. Enforcement additionally rides a session-stable
percentage (``TOOL_PROJECTION_ROLLOUT_PERCENT``) so the first production traffic
is a cohort rather than everything at once. Flipping the mode back to
``disabled`` is a Cloud Run environment update — a new revision, no image
rebuild — which is what "roll back without a code deployment" means here.

``outside_proposed`` is the number that authorized this. It names the calls a
turn made that the projection would have withheld, and it is still logged under
enforcement — but read it from the SHADOW window when deciding to promote: an
enforced turn cannot call a tool it was never offered, so enforced traffic
grades itself clean by construction and cannot authorize its own expansion.

**What the proposed set always contains**, beyond the active runbooks' declared
tools:

* the always-on core — every tool the ALWAYS-PRESENT layers instruct (the base
  prompt, and the turn footer's conditional lines), so a turn can orient, read
  the workspace, delegate, page an artifact, stop what is already running and
  talk to the user whatever is loaded. This set is not a judgment call:
  withholding a tool the server's own permanent prompt commands is a
  contradiction the model cannot resolve, and
  ``tests/unit/test_prompt_hygiene_tools.py`` fails if either layer ever names
  one this set is missing;
* the runbook-run scaffold (``runbook.step.advance``, ``runbook.run.get``) whenever a
  runbook is active, because the loaded-runbooks block instructs them and no runbook
  declares them — they belong to the template, not to any runbook;
* every domain foundation's tools for the domains in play — a foundation is the
  base judgment under its specifics and its tools are what that judgment uses;
* any tool named by a forced ``tool_choice``, because withholding one the client
  pinned would be an immediate hard error;
* every tool named by an unresolved assistant tool call earlier in the
  conversation, so a continuation round can always answer for work already
  started;
* any offered tool NO runbook in the catalog declares. Withholding is an argument
  from metadata, and there is no metadata about a tool the catalog has never
  heard of — a client-side addition, or an integration's. Silence is not
  evidence that a turn does not need it.

Withholding is final for the turn. The CLI reports an unoffered tool to the
model as ``callable: false`` — "calling them would do nothing" — and dynamic
invocation of a withheld tool does not exist, so the escape hatch discovers
names rather than recovering capability.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ..config import ToolProjectionMode
from ..contracts.openai import ChatMessage, ToolDef
from .catalog import Catalog
from .models import Runbook

# Runbook frontmatter and the base prompt name tools in DOTTED form
# ("terminal.rename"); the CLI puts them on the wire with a double underscore
# ("terminal__rename"), because a dot is not legal in an OpenAI function name.
# Comparing the two forms directly matches nothing — a projection that withheld
# the entire inventory, and exactly the kind of silent inversion shadow mode
# exists to catch before it reaches a request.
_WIRE_SEP = "__"


def wire_name(dotted: str) -> str:
    """The wire spelling of a dotted tool name. Idempotent."""
    return dotted.replace(".", _WIRE_SEP)


# Tools that must stay callable whatever is loaded — the ALWAYS-PRESENT layers
# (``prompts/main/base/*.md`` and ``prompts/main/turn_footer.md.j2``) instruct
# every one of them, on every turn, with no runbook involved. That is what makes
# this list derived rather than chosen: a projection that withholds a tool the
# permanent prompt commands hands the model an instruction it cannot follow, and
# no runbook is loaded to explain the absence.
# `tests/unit/test_prompt_hygiene_tools.py` re-derives the set from both layers'
# text and fails when they drift.
#
# It is not free — every entry is a permanent cost on every request — but the
# alternative is not a smaller core, it is a smaller BASE PROMPT. Drop the
# instruction first; the tool follows it out.
ALWAYS_AVAILABLE: frozenset[str] = frozenset(
    {
        # Orient.
        "context.snapshot",
        "daintree.status",
        # Read the workspace. A turn the classifier answered with NO runbook is a
        # first-class outcome (the cardinality head makes it one), and it is
        # still expected to be able to look at the project.
        "fs.read",
        "fs.list",
        "fs.search",
        # Page a large result. `20-tool-discipline.md` says any result too big to
        # inline arrives as an artifact to be read one page at a time — so this is
        # reachable from ANY tool call, not from a particular family.
        "artifact.read",
        # Delegate the search. The base prompt tells the model to reach for this
        # BEFORE the first exploratory round, and CLAUDE.md calls the bias toward
        # delegating the point of three separate prompt edits.
        "subagent.run",
        # A HARD rule, not a preference: `10-hard-rules.md` says file changes are
        # made by spawning a visible agent and that EVERY spawn goes through this
        # one call. A turn told it may not edit files itself and then denied the
        # only sanctioned way to have them edited has no legal move left.
        "agentTask.spawnForEdits",
        # Find a capability that was not projected, and reach an MCP tool that has
        # no typed wrapper. `daintree.call` is what makes `daintree.listTools`
        # more than a catalogue — without it the model can see a tool and not
        # invoke it.
        "tool.search",
        "daintree.listTools",
        "daintree.call",
        # Talk to the user and close the loop. `user.askMultipleChoice` is how the
        # base prompt says to ask a finite question; without it the model asks in
        # prose instead, which the CLI cannot render as choices. `queue.resolve`
        # is how it retires an inbox item it has actually reported.
        "user.askMultipleChoice",
        "queue.resolve",
        # Speak to work already running. The base prompt advertises these as
        # always there; a turn that cannot answer a waiting agent or retire a
        # finished terminal strands the cohort it started.
        "terminal.sendCommand",
        "terminal.close",
        # Instructed in PROSE, so the hygiene scanner cannot see them and each has
        # to be argued for by hand (`_PROSE_INSTRUCTED` in that test carries the
        # quote). The bar is a SPECIFIC instruction naming the capability, not a
        # passing mention: "confirm a finish with a bounded tail read per
        # terminal" is one, and "read output only when you need the content" is
        # not — under the looser reading, terminal.summarize and every watcher and
        # async tool qualify too, and the derivation stops meaning anything.
        #
        # The CLI's `coreToolNames` is a tempting second source and was tried as
        # one; it is the CLI's list, written against its own view, and four of the
        # six tools it added here turned out to be justified by nothing in this
        # prompt. It is a cross-check, never the authority.
        "terminal.read",
        "queue.digest",
        # See what is already watching. Listing is read-only and the base prompt
        # names all three as the way to check standing supervision.
        "watcher.list",
        "timer.list",
        "worktree.list",
        # Stop it. These two are instructed by the TURN FOOTER rather than the
        # base prompt — "do NOT re-run, await, or poll them; async.cancel stops
        # one" and "use watcher.list to inspect or watcher.cancel to stop one" —
        # and the footer is a server-owned layer that renders on a runbook-less turn
        # like any other. `daintree.foundation` declares both, so the catalog HAS
        # an opinion about them and the unclassified pass-through does not rescue
        # them; withholding is final, so `tool.search` cannot either.
        #
        # Admitted unconditionally, unlike the runbook scaffold below: those footer
        # lines render only when the turn carries async work or a resumed watcher,
        # and `project_tools` is not given `turn_context` to mirror that. Two
        # permanent entries is the cheaper end of that trade.
        "async.cancel",
        "watcher.cancel",
        # Remember. `memory.save` is instructed in the base prompt and belongs to
        # no domain.
        "memory.save",
    }
)


# The loaded-runbooks scaffold (``prompts/main/loaded_runbooks.md.j2``) instructs
# these whenever a runbook is active, and NO runbook declares them — they belong to
# the template, so no `requiredTools` audit could ever see them. Admitted exactly
# when the block that instructs them renders: when the active set is non-empty.
RUNBOOK_SCAFFOLD_TOOLS: frozenset[str] = frozenset(
    {
        "runbook.step.advance",
        "runbook.run.get",
    }
)


@dataclass(frozen=True)
class ToolProjection:
    """What a turn WOULD send, and why each tool survived.

    ``reasons`` exists for the shadow log: when the model calls something outside
    the proposed set, the useful diagnostic is which rule would have admitted it
    had the metadata been right — not merely that it was missing.
    """

    proposed: frozenset[str] = frozenset()
    offered: frozenset[str] = frozenset()
    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def withheld(self) -> frozenset[str]:
        return self.offered - self.proposed

    def outside(self, called: list[str]) -> list[str]:
        """Called tools the projection would have withheld.

        THE shadow-mode metric. Durably zero means the metadata describes what
        turns actually do, and filtering can be enabled.
        """
        return sorted({name for name in called if name in self.withheld})


def project_tools(
    *,
    offered: list[ToolDef] | None,
    active_runbooks: list[Runbook],
    catalog: Catalog,
    messages: list[ChatMessage],
    forced_tool: str | None = None,
) -> ToolProjection:
    """Compute the tool set this turn would send under runbook-aware filtering.

    Pure and side-effect free: it reads the turn and returns a proposal. Nothing
    here mutates the request, and in shadow mode nothing consumes the result
    except the log.
    """
    offered_names = frozenset(tool.function.name for tool in (offered or []))
    if not offered_names:
        return ToolProjection()

    reasons: dict[str, str] = {}

    def admit(dotted: str, reason: str) -> None:
        # Compare in WIRE form, and record the name the request actually used, so
        # `proposed` is directly comparable to what the model calls.
        name = wire_name(dotted)
        # First reason wins: "core" is more informative than "runbook:foo" for a
        # tool that would have been admitted either way.
        if name in offered_names and name not in reasons:
            reasons[name] = reason

    for name in ALWAYS_AVAILABLE:
        admit(name, "core")

    # The loaded-runbooks block renders only when something is active, and it is
    # what instructs the run scaffold. Admitting these unconditionally would
    # charge every runbook-less turn for two tools its prompt never mentions.
    if active_runbooks:
        for name in RUNBOOK_SCAFFOLD_TOOLS:
            admit(name, "runbook_scaffold")

    # The active runbooks' declared needs, plus their domain foundations'. A
    # foundation carries the base judgment its specifics build on, so a turn that
    # loaded a specific needs the foundation's tools even when the foundation
    # itself did not make the active set.
    for runbook in active_runbooks:
        for name in runbook.meta.required_tools or ():
            admit(name, f"runbook:{runbook.id}")
        foundation_id = catalog.domain_foundation(runbook.id)
        if foundation_id and foundation_id != runbook.id:
            foundation = catalog.get(foundation_id)
            if foundation is not None:
                for name in foundation.meta.required_tools or ():
                    admit(name, f"foundation:{foundation_id}")

    # A pinned tool_choice the projection withheld would be an immediate hard
    # error from the provider, not a degraded turn.
    if forced_tool:
        admit(forced_tool, "forced_tool_choice")

    # Anything an earlier assistant turn called and has not yet been answered.
    # A continuation round must always be able to finish work already started.
    for name in _unresolved_tool_calls(messages):
        admit(name, "unresolved_call")  # already a wire name; wire_name is idempotent

    # Anything the CATALOG has never heard of. Withholding is an argument from
    # metadata — "no active runbook needs this" — and that argument cannot be made
    # about a tool no runbook declares at all. A CLI release that adds a tool ahead
    # of the runbook that uses it, or an integration contributing its own, would
    # otherwise be withheld by silence and stay withheld until someone noticed.
    # Filtering has to be safe by default when the metadata is incomplete, which
    # is exactly the state it is always in.
    # The scaffold counts as CLASSIFIED even when it did not fire: the catalog has
    # a definite opinion about those two (they belong to the loaded-runbooks block),
    # so a runbook-less turn must WITHHOLD them rather than re-admit them here as
    # things nobody has heard of — which would make the activation rule above
    # decorative.
    classified = catalog.declared_tools | {wire_name(n) for n in RUNBOOK_SCAFFOLD_TOOLS}
    for name in offered_names:
        if name not in classified and name not in reasons:
            reasons[name] = "unclassified"

    return ToolProjection(proposed=frozenset(reasons), offered=offered_names, reasons=reasons)


def _unresolved_tool_calls(messages: list[ChatMessage]) -> set[str]:
    """Tool names called by an assistant turn with no matching tool result.

    Matched by call id rather than by name: the same tool can be called twice in
    a turn with only one answered, and dropping it because a namesake was
    resolved would strand the other.
    """
    # Ids are compared STRIPPED, which is now belt-and-braces rather than the
    # load-bearing rule it was. Validation used to PAIR on the stripped value, so
    # a raw comparison here would have seen a call answered by " c1 " as still
    # open; it now REJECTS a padded id outright, because the ids travel upstream
    # raw and a stripped pairing agreed with nothing downstream of it. So this
    # function can no longer be reached by a padded id — the strip stays because
    # it costs nothing and this is not the only caller's problem to inherit.
    #
    # What this function DOES still depend on is the other half of that rule: a
    # call id is unique while it is OPEN. Keyed by id, two calls open at once
    # under one id would collapse to the later name and strand the earlier tool.
    # `services/validation._validate_message_sequence` is what guarantees they
    # cannot be.
    pending: dict[str, str] = {}
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            for call in message.tool_calls:
                pending[call.id.strip()] = call.function.name
        elif message.role == "tool" and message.tool_call_id:
            pending.pop(message.tool_call_id.strip(), None)
    return set(pending.values())


# ---- enforcement -----------------------------------------------------------

# Dispositions that are NOT failures. A turn that sent the full inventory because
# nobody asked it not to is not evidence of anything going wrong, and counting it
# as a fail-open would bury the reasons that matter under the ones that don't.
REASON_DISABLED = "disabled"
REASON_SHADOW = "shadow"
REASON_PROFILE_EXCLUDED = "profile_excluded"
REASON_COHORT_EXCLUDED = "cohort_excluded"
REASON_NO_OFFERED_TOOLS = "no_offered_tools"
REASON_ENFORCED = "enforced"

# Genuine fail-open reasons: enforcement was asked for, and something about this
# turn made withholding unsafe. Every one of these should be ZERO in a healthy
# rollout except `selector_degraded`, which is a fact about the selector rather
# than about projection and has its own (nonzero) expected rate.
REASON_SELECTOR_DEGRADED = "selector_degraded"
REASON_PROJECTION_ERROR = "projection_error"
REASON_EMPTY_PROJECTION = "empty_projection"
REASON_INVALID_PROJECTION = "invalid_projection"
REASON_FORCED_TOOL_MISSING = "forced_tool_missing"
REASON_UNRESOLVED_TOOL_MISSING = "unresolved_tool_missing"
REASON_INVENTORY_MISMATCH = "inventory_mismatch"
REASON_FILTER_ERROR = "filter_error"

_FAIL_OPEN_REASONS: frozenset[str] = frozenset(
    {
        REASON_SELECTOR_DEGRADED,
        REASON_PROJECTION_ERROR,
        REASON_EMPTY_PROJECTION,
        REASON_INVALID_PROJECTION,
        REASON_FORCED_TOOL_MISSING,
        REASON_UNRESOLVED_TOOL_MISSING,
        REASON_INVENTORY_MISMATCH,
        REASON_FILTER_ERROR,
    }
)


@dataclass(frozen=True)
class EnforcementDecision:
    """Whether to send the projection this turn, and the one reason why not.

    ``fail_open`` is deliberately narrower than ``not enforce``: shadow mode,
    the unselected cohort and a sub-agent turn all decline to enforce without
    anything having gone wrong. Only the reasons in ``_FAIL_OPEN_REASONS`` mean
    "enforcement was asked for and this turn could not safely have it", and that
    is the rate a rollback triggers on.
    """

    enforce: bool
    reason: str
    cohort_selected: bool = False
    cohort_bucket: int | None = None

    @property
    def fail_open(self) -> bool:
        return self.reason in _FAIL_OPEN_REASONS


#: Default hash namespace — this module's own rollout. Kept as the default so
#: every existing caller and `test_the_bucket_is_pinned_to_literal_values`'s
#: literals are unaffected by the `namespace` parameter below.
_DEFAULT_NAMESPACE = b"tool-projection\x00"


def session_bucket(session_id: str, *, namespace: bytes = _DEFAULT_NAMESPACE) -> int:
    """A stable 0-99 bucket for a session.

    Bucketed on the SESSION, never the request: a percentage that re-rolled per
    request would move a conversation between arms mid-turn, so a continuation
    round could be offered a different inventory than the round that started the
    work. Hashed rather than taken from the id's bytes so that ids sharing a
    prefix (a client that stamps a machine or date into them) do not land in one
    arm together.

    Monotonic in the percentage by construction: raising the threshold only ever
    adds sessions, so a promotion never drops a session that was already enrolled.

    ``namespace`` exists because this bucketing is reused by more than one
    rollout (tool projection, the fast-response stage): the same session hashed
    under the same namespace lands in the same bucket for both, which correlates
    two experiments that are supposed to be independent — a session enrolled in
    one is then always enrolled in the other whenever the two percentages
    overlap. Each caller should pass its own literal namespace; the default is
    this module's, kept only for backward compatibility.
    """
    digest = hashlib.sha256(namespace + session_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % 100


def decide_enforcement(
    *,
    mode: ToolProjectionMode,
    rollout_percent: int,
    session_id: str,
    profile: str,
    selector_degraded: bool,
    projection: ToolProjection | None,
    messages: list[ChatMessage],
    offered_total: int,
    forced_tool: str | None = None,
) -> EnforcementDecision:
    """Decide whether this turn may send the projected inventory.

    Pure: it reads the turn's facts and returns a verdict. It takes no settings
    object, no orchestrator and no request id, so every branch below is reachable
    from a unit test without building an app.

    ``projection=None`` means the projection could not be computed. That is a
    DIFFERENT fact from an empty projection, and conflating them is how a bug
    that returns nothing becomes a request that offers nothing — so the caller
    passes ``None`` on failure rather than a default-constructed instance.

    ``offered_total`` is how many tools the REQUEST carries, read from the request
    rather than from the projection. Every other question here could be answered
    out of the projection's own ``offered`` set, and that is exactly the problem:
    a projector that wrongly returned an empty result would then be describing a
    turn where nothing was offered, and "nothing to withhold" is the one shape
    that reads as benign. One number from outside the projection is what makes
    that self-report checkable.

    Order matters below: the cheap categorical answers (mode, profile, cohort)
    come first so a disabled deployment never pays for the checks, and the
    correctness gates come last so their reasons are never masked by one of them.
    """
    if mode == "disabled":
        return EnforcementDecision(enforce=False, reason=REASON_DISABLED)
    if mode == "shadow":
        return EnforcementDecision(enforce=False, reason=REASON_SHADOW)

    # Sub-agents are held out of the first rollout. Their menu is already narrowed
    # to what they can execute and only a couple of runbooks survive that filter, so
    # most of their turns are runbook-less — which is exactly the shape where a core
    # tuned for the ASSISTANT base prompt is the wrong core. Little to win, and the
    # failure would be silent: a research worker that cannot read.
    if profile != "assistant":
        return EnforcementDecision(enforce=False, reason=REASON_PROFILE_EXCLUDED)

    bucket = session_bucket(session_id)
    selected = bucket < rollout_percent
    if not selected:
        return EnforcementDecision(
            enforce=False,
            reason=REASON_COHORT_EXCLUDED,
            cohort_selected=False,
            cohort_bucket=bucket,
        )

    def verdict(enforce: bool, reason: str) -> EnforcementDecision:
        return EnforcementDecision(
            enforce=enforce, reason=reason, cohort_selected=True, cohort_bucket=bucket
        )

    if selector_degraded:
        return verdict(False, REASON_SELECTOR_DEGRADED)
    if projection is None:
        return verdict(False, REASON_PROJECTION_ERROR)
    if not projection.offered:
        # Nothing was offered, so there is nothing to withhold. Benign ONLY when
        # the request agrees; a projection that saw no tools in a request full of
        # them is describing some other turn, and letting that pass as "nothing to
        # do here" would hide the failure behind its most reassuring outcome.
        if offered_total:
            return verdict(False, REASON_INVALID_PROJECTION)
        return verdict(False, REASON_NO_OFFERED_TOOLS)
    if len(projection.offered) != offered_total:
        return verdict(False, REASON_INVALID_PROJECTION)
    # Tools WERE offered and the projection kept none of them. The core alone
    # guarantees a non-empty proposal whenever the inventory contains any of it,
    # so this means either an inventory sharing no name with the catalog or a
    # projection bug — and both are answered by sending everything.
    if not projection.proposed:
        return verdict(False, REASON_EMPTY_PROJECTION)
    if not projection.proposed <= projection.offered or set(projection.reasons) != set(
        projection.proposed
    ):
        return verdict(False, REASON_INVALID_PROJECTION)

    # The last two are true by construction — `project_tools` admits both. They
    # are re-checked because construction is exactly what a regression breaks, and
    # the failure mode is not a smaller prompt: a pinned `tool_choice` the request
    # does not carry is an immediate provider 400, and a stranded continuation is
    # a turn that can never finish the work it started.
    if forced_tool and wire_name(forced_tool) not in projection.proposed:
        return verdict(False, REASON_FORCED_TOOL_MISSING)
    if any(name not in projection.proposed for name in _unresolved_tool_calls(messages)):
        return verdict(False, REASON_UNRESOLVED_TOOL_MISSING)

    return verdict(True, REASON_ENFORCED)


def filter_provider_tools(
    tools: list[dict[str, Any]] | None, projection: ToolProjection
) -> list[dict[str, Any]]:
    """The assembled tool array narrowed to the projection, in the original order.

    Order is preserved because the tool region is cached on its literal bytes: a
    reordering that changed nothing about which tools are present would still
    read upstream as a different prefix.

    Raises rather than skipping a malformed entry. A tool the caller cannot name
    is one it cannot decide about, and dropping it silently is the single
    failure this whole design is arranged to avoid — the caller turns the raise
    into a fail-open with a reason attached.
    """
    kept: list[dict[str, Any]] = []
    for tool in tools or []:
        name = tool["function"]["name"]
        if not isinstance(name, str):
            raise TypeError(f"tool name is not a string: {name!r}")
        if name in projection.proposed:
            kept.append(tool)
    return kept


def tools_bytes(tools: list[dict[str, Any]] | None) -> int:
    """Compact serialized size of a tool array, as the wire would carry it.

    Measures the tool ARRAY, not the request: the rest of the body is unaffected
    by projection, and serializing it a second time to learn nothing would be the
    most expensive part of the measurement.
    """
    return len(json.dumps(tools or [], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
