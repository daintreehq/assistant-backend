"""Offering the fork tool, and making the call it produces invisible.

Two halves of one loop live here. On the way OUT, `decide_fork_feature` says
whether this turn may see the internal function and `inject_fork_tool` appends
it to the prepared provider request. On the way BACK, `ForkedToolCallRewriter`
finds the model's internal call, expands it, and replaces it with four ordinary
`agentTask__spawnForEdits` calls.

**The internal call must never cross the wire.** Not "the client ignores it" —
the client is forbidden from offering or replaying anything in the
`daintree_internal__` namespace, so a leaked call would come straight back as a
400 on the continuation round, after the user had already watched four terminals
fail to appear. Both response paths therefore rewrite BEFORE anything grades,
lints, accounts for or emits the round.

**The call must arrive alone.** A batch mixing the fork call with other tools is
refused rather than partially executed: the reply is one assistant decision, and
some of those siblings may be prerequisites of the fork while others are
independent. There is no safe way to run half of it, and exposing the siblings
while silently dropping the fork would leave the model believing it had spawned
four agents.

Injection happens AFTER the client's inventory is validated and projected, and
only into `ProviderChatRequest.tools` — never into `RespondRequest.input.tools`,
which is the client's real callable surface and is replayed back to us next
round.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..config import ForkedTerminalCallsMode
from ..contracts.forked_terminals import ForkSpawnPlan
from ..errors import fork_call_not_alone, fork_plan_invalid, fork_target_unavailable
from ..observability import get_logger
from ..providers.base import ProviderChatRequest, ProviderUsage
from .deadlines import RequestBudget
from .fork_prompt_expansion import ForkExpansionResult, ForkPromptExpansionService
from .forked_terminal_calls import (
    FORK_TOOL_DEFINITION,
    FORK_TOOL_WIRE_NAME,
    SPAWN_TOOL_WIRE_NAME,
    find_tool,
)

logger = get_logger("daintree.fork_rewrite")

REASON_ENABLED = "enabled"
REASON_DISABLED = "disabled"
REASON_PROFILE_EXCLUDED = "profile_excluded"
REASON_TARGET_TOOL_ABSENT = "target_tool_absent"
REASON_PROVIDER_TOOL_LIMIT = "provider_tool_limit"


@dataclass(frozen=True)
class ForkFeatureDecision:
    """Whether this turn is offered the fork tool, and the one reason why not."""

    enabled: bool
    reason: str


def decide_fork_feature(
    *,
    mode: ForkedTerminalCallsMode,
    profile: str,
    tools: list[dict[str, Any]] | None,
    max_tools: int,
) -> ForkFeatureDecision:
    """Decide once per turn, on facts already settled by assembly.

    Deliberately NOT conditional on which skills the turn loaded. The rule the
    base prompt states must be true for the whole session or the model reads an
    instruction about a tool that appears halfway through a workflow — and a
    tool list that changes on a topic shift re-encodes the largest region of the
    request for nothing.
    """
    if mode != "enabled":
        return ForkFeatureDecision(enabled=False, reason=REASON_DISABLED)

    # A sub-agent is read-only and cannot spawn a visible terminal at all, so
    # the tool would be an instruction it has no legal move for.
    if profile != "assistant":
        return ForkFeatureDecision(enabled=False, reason=REASON_PROFILE_EXCLUDED)

    # The fork tool expands into exactly one other tool. Offering it to a caller
    # that does not hold that tool would produce four calls the client cannot
    # execute — worse than not offering it, because the model would believe it
    # had spawned them.
    if find_tool(tools, SPAWN_TOOL_WIRE_NAME) is None:
        return ForkFeatureDecision(enabled=False, reason=REASON_TARGET_TOOL_ABSENT)

    # One server tool on top of a maximum-sized client inventory would put the
    # request over the count the server itself calls the limit. Declining is the
    # conservative half of that trade: the alternative — advertising one fewer
    # client tool — would 400 a request that is legal today, for a ceiling this
    # repo has never verified against the live host.
    if len(tools or ()) >= max_tools:
        return ForkFeatureDecision(enabled=False, reason=REASON_PROVIDER_TOOL_LIMIT)

    return ForkFeatureDecision(enabled=True, reason=REASON_ENABLED)


def inject_fork_tool(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Append the server-owned tool to a prepared inventory.

    APPENDED, never inserted. The tools region is serialized ahead of the
    conversation and matched literally by the prefix cache, so adding the entry
    at the end leaves every existing schema at the byte offset it already had.
    """
    return [*(tools or ()), FORK_TOOL_DEFINITION]


def is_fork_call(call: dict[str, Any]) -> bool:
    function = call.get("function")
    if not isinstance(function, dict):
        return False
    return function.get("name") == FORK_TOOL_WIRE_NAME


def mentions_fork_tool(fragment: object) -> bool:
    """Whether a RAW stream fragment carries the internal function's name.

    Used only on the passthrough path, where fragments are forwarded verbatim
    and never consolidated. It is a coarse check on purpose: a fragment may hold
    any slice of the name, so anything that could be part of it is treated as if
    it were, and the turn fails closed rather than risk emitting one.
    """
    if not isinstance(fragment, dict):
        return False
    function = fragment.get("function")
    if not isinstance(function, dict):
        return False
    name = function.get("name")
    if not isinstance(name, str) or not name:
        return False
    return name in FORK_TOOL_WIRE_NAME or FORK_TOOL_WIRE_NAME.startswith(name)


@dataclass(frozen=True)
class RewrittenToolCalls:
    """What the client should see, and what producing it cost."""

    calls: list[dict[str, Any]]
    added_usage: ProviderUsage | None = None
    fork: ForkExpansionResult | None = None

    @property
    def rewritten(self) -> bool:
        return self.fork is not None


class ForkedToolCallRewriter:
    """Replaces one internal fork call with the four spawn calls it stands for."""

    def __init__(self, *, expansion: ForkPromptExpansionService) -> None:
        self.expansion = expansion

    async def rewrite(
        self,
        calls: list[dict[str, Any]],
        *,
        base_request: ProviderChatRequest,
        request_id: str,
        budget: RequestBudget | None = None,
    ) -> RewrittenToolCalls:
        """Return the client-visible calls for this round.

        A round with no fork call returns its own list unchanged and costs one
        list scan, which is what every turn on a deployment with the feature off
        pays.
        """
        fork_calls = [call for call in calls if is_fork_call(call)]
        if not fork_calls:
            return RewrittenToolCalls(calls=calls)

        if len(fork_calls) > 1 or len(calls) > 1:
            # Refused as a whole rather than partially executed. Running the
            # siblings while dropping the fork would leave the model believing
            # it had spawned four agents; running the fork while dropping the
            # siblings could skip a prerequisite it was told to do first.
            logger.warning(
                "fork_rewrite_rejected",
                request_id=request_id,
                reason="not_alone",
                raw_call_count=len(calls),
                fork_call_count=len(fork_calls),
            )
            raise fork_call_not_alone(len(calls))

        fork_call = fork_calls[0]
        plan = self._parse_plan(fork_call, request_id=request_id)

        spawn_tool = find_tool(base_request.tools, SPAWN_TOOL_WIRE_NAME)
        if spawn_tool is None:
            # Unreachable through `decide_fork_feature`, which refuses to offer
            # the fork tool without its target. Reaching it means assembly and
            # rewrite disagree about what was offered, and fabricating calls the
            # client cannot execute is the one wrong answer.
            logger.warning(
                "fork_rewrite_rejected", request_id=request_id, reason="target_absent"
            )
            raise fork_target_unavailable()
        schema = spawn_tool.get("function", {}).get("parameters")
        if not isinstance(schema, dict):
            raise fork_target_unavailable()

        result = await self.expansion.expand(
            base_request=base_request,
            plan=plan,
            fork_call_id=str(fork_call.get("id") or "fork"),
            spawn_schema=schema,
            request_id=request_id,
            budget=budget,
        )
        logger.info(
            "fork_rewrite",
            request_id=request_id,
            raw_call_count=len(calls),
            visible_call_count=len(result.calls),
            # Named separately because they are different questions: what the
            # model DECIDED, and what capability the client actually had to
            # execute. Conflating them would make a rewritten round look like a
            # round that called the spawn tool four times by itself.
            raw_internal_calls=1,
            visible_spawn_calls=len(result.calls),
        )
        return RewrittenToolCalls(
            calls=result.calls, added_usage=result.usage, fork=result
        )

    @staticmethod
    def _parse_plan(call: dict[str, Any], *, request_id: str) -> ForkSpawnPlan:
        """Validate the model's plan, failing CLOSED.

        There is no repair path and no default. Every field in a fork plan
        either names prose or selects a mutation target, and the server is not
        entitled to guess a worktree, an agent or a supervision decision on the
        user's behalf. The strict schema plus a benchmark corpus is what makes
        this rare; guessing is what would make it dangerous.
        """
        raw = call.get("function", {}).get("arguments")
        if raw is None:
            raw = ""
        if not isinstance(raw, str):
            # A provider that sends an already-decoded object rather than the
            # JSON-encoded string the wire specifies. Typed rather than left to
            # `.strip()` raising an AttributeError, which would surface as a 500
            # about a Daintree bug instead of the documented upstream failure.
            logger.warning(
                "fork_plan_rejected", request_id=request_id, reason="arguments_not_a_string"
            )
            raise fork_plan_invalid("the fork plan arguments were not a JSON string")
        try:
            parsed = json.loads(raw) if raw.strip() else None
        except json.JSONDecodeError as exc:
            logger.warning(
                "fork_plan_rejected", request_id=request_id, reason="invalid_json"
            )
            raise fork_plan_invalid("the fork plan was not valid JSON") from exc
        if not isinstance(parsed, dict):
            logger.warning(
                "fork_plan_rejected", request_id=request_id, reason="not_an_object"
            )
            raise fork_plan_invalid("the fork plan was not a JSON object")
        try:
            plan = ForkSpawnPlan.model_validate(parsed)
        except Exception as exc:  # noqa: BLE001 — pydantic raises its own type
            # The VALIDATION PATH is logged, never the content: a plan quotes
            # the user's goal and their project's file scopes.
            logger.warning(
                "fork_plan_rejected",
                request_id=request_id,
                reason="schema",
                error_count=len(getattr(exc, "errors", lambda: [])()),
            )
            raise fork_plan_invalid("the fork plan did not satisfy its schema") from exc
        logger.info(
            "fork_plan_received",
            request_id=request_id,
            branch_count=len(plan.branches),
        )
        return plan
