"""Skill-aware tool projection — computed every turn, applied to nothing yet.

Tool schemas are ~61% of a representative request, and most of them are
irrelevant to what the turn is doing: a rename-terminals turn does not need the
forge, git or recipe families. Filtering them to the active skills' declared
needs is the single largest prompt saving available.

**It runs in shadow mode.** Every turn computes the set it *would* send and logs
how that compares to the set it *does* send, but the request still carries the
full inventory.

The reason is not that ``requiredTools`` is known to be wrong — an offline audit
(``tests/unit/test_tool_projection.py``) now finds no skill whose body instructs
a call it fails to declare. It is that the audit cannot see far enough to
authorize withholding. It reads backticked registry names out of skill bodies,
which catches a runbook naming a call site and misses everything else: a tool the
model reaches for on its own judgment, one the base prompt implies, one named in
prose the scanner does not recognise as a call. ``requiredTools`` was written as
documentation. It now *looks* like a correct allowlist, and that is exactly the
state in which turning it into one is most tempting and least justified.

So the order is: measure in production first. The number to watch is
``outside_proposed`` — the calls a turn made that the projection would have
withheld. Filtering is safe to enable when that is durably empty across real
usage, and not before. A round the grader could not fully read is logged
``measured=False`` rather than as a clean zero, because an unmeasured turn
counted as evidence of safety is worse than no evidence at all.

**What the proposed set always contains**, beyond the active skills' declared
tools:

* a small always-on core, so a turn can orient, search for a capability and talk
  to the user even with no skill loaded;
* every domain foundation's tools for the domains in play — a foundation is the
  base judgment under its specifics and its tools are what that judgment uses;
* any tool named by a forced ``tool_choice``, because withholding one the client
  pinned would be an immediate hard error;
* every tool named by an unresolved assistant tool call earlier in the
  conversation, so a continuation round can always answer for work already
  started.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts.openai import ChatMessage, ToolDef
from .catalog import Catalog
from .models import Skill

# Skill frontmatter and the base prompt name tools in DOTTED form
# ("terminal.rename"); the CLI puts them on the wire with a double underscore
# ("terminal__rename"), because a dot is not legal in an OpenAI function name.
# Comparing the two forms directly matches nothing — a projection that withheld
# the entire inventory, and exactly the kind of silent inversion shadow mode
# exists to catch before it reaches a request.
_WIRE_SEP = "__"


def wire_name(dotted: str) -> str:
    """The wire spelling of a dotted tool name. Idempotent."""
    return dotted.replace(".", _WIRE_SEP)


# Tools that must stay callable whatever is loaded. A turn with no skill still
# has to be able to see the workspace, find a capability it was not given, and
# ask the user a question — and the base prompt instructs it to do all three.
# Deliberately tiny: every entry here is a permanent cost on every request.
ALWAYS_AVAILABLE: frozenset[str] = frozenset(
    {
        # Orient.
        "context.snapshot",
        "daintree.status",
        # Find a capability that was not projected — the escape hatch that makes
        # filtering recoverable rather than a dead end.
        "tool.search",
        "daintree.listTools",
        # Talk to the user. `user.askMultipleChoice` is how the base prompt says
        # to ask a finite question; without it the model asks in prose instead,
        # which the CLI cannot render as choices.
        "user.askMultipleChoice",
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
    active_skills: list[Skill],
    catalog: Catalog,
    messages: list[ChatMessage],
    forced_tool: str | None = None,
) -> ToolProjection:
    """Compute the tool set this turn would send under skill-aware filtering.

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
        # First reason wins: "core" is more informative than "skill:foo" for a
        # tool that would have been admitted either way.
        if name in offered_names and name not in reasons:
            reasons[name] = reason

    for name in ALWAYS_AVAILABLE:
        admit(name, "core")

    # The active skills' declared needs, plus their domain foundations'. A
    # foundation carries the base judgment its specifics build on, so a turn that
    # loaded a specific needs the foundation's tools even when the foundation
    # itself did not make the active set.
    for skill in active_skills:
        for name in skill.meta.required_tools or ():
            admit(name, f"skill:{skill.id}")
        foundation_id = catalog.domain_foundation(skill.id)
        if foundation_id and foundation_id != skill.id:
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

    return ToolProjection(proposed=frozenset(reasons), offered=offered_names, reasons=reasons)


def _unresolved_tool_calls(messages: list[ChatMessage]) -> set[str]:
    """Tool names called by an assistant turn with no matching tool result.

    Matched by call id rather than by name: the same tool can be called twice in
    a turn with only one answered, and dropping it because a namesake was
    resolved would strand the other.
    """
    # Ids are compared STRIPPED, matching how request validation pairs a tool
    # result to its call (`services/validation.py`). Comparing raw here would
    # leave a call whose result arrived as " c1 " looking unanswered, and admit
    # its tool for no reason.
    pending: dict[str, str] = {}
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            for call in message.tool_calls:
                pending[call.id.strip()] = call.function.name
        elif message.role == "tool" and message.tool_call_id:
            pending.pop(message.tool_call_id.strip(), None)
    return set(pending.values())
