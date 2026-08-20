"""The local skill selector — a drop-in replacement for the prompt-based one.

Same ``select()`` signature and same ``SelectionDecision`` return type as
``skills.selector.Selector``, so the orchestrator and the benchmark harness both
drive it unchanged and the two can be scored against each other on identical
evidence.

Selection itself is two stages (``setwise.SetwiseClassifier``): a cosine
shortlist over the embedded catalog, then one set-wise pass that ranks the
shortlist, predicts how many skills the turn needs (zero included) and decides
whether to retrieve documentation. The rules layer here stays deliberately thin
— the one rule it keeps, the background wake, is a STRUCTURAL fact about the
turn (there is no new user instruction at all), not a semantic judgment the
model should be second-guessed on.

**There is no staleness gate here any more, and its absence is the point.** The
previous selector owned one weight row per skill, so a catalog whose routing
surface had moved left it ranking against documents that no longer existed; it
had to compare a stamped ``routing_revision`` and degrade every turn open until
someone retrained. This selector has no per-skill parameters — it reads skill
EMBEDDINGS — so a changed catalog is just different vectors. ``sync`` re-embeds
whatever moved, on the turn it moved, and selection carries on. That is what
makes a skill written by skill learning reachable on the next turn instead of the
next deploy — reachable, not yet competitive against its trained neighbours; see
``setwise.py`` for the measurement.
"""

from __future__ import annotations

import logging
from typing import Protocol

from pydantic import SecretStr

from ...config import Settings
from ...contracts.openai import ChatMessage
from ...providers.policy import RoutingPreference
from ..catalog import Catalog, CatalogManager
from ..selector import SelectionDecision
from .documents import _content_text, query_document
from .runtime import InferencePool
from .setwise import SetwiseClassifier

_log = logging.getLogger(__name__)


class SkillSelector(Protocol):
    """What the orchestrator needs from a selector — nothing more.

    Two implementations exist: ``LocalSelector`` (production — the in-process
    two-stage selector) and the prompt-based ``skills.selector.Selector``, which survives
    as the scriptable seam for orchestration tests and deterministic benchmarks
    (its FakeProvider/ReplayProvider scripting is the interface dozens of
    behavioural assertions are written against).
    """

    async def select(
        self,
        conversation: list[ChatMessage],
        active_ids: list[str],
        *,
        api_key: SecretStr | None = None,
        turn_context: str | None = None,
        available_tools: list[str] | None = None,
        routing: RoutingPreference | None = None,
    ) -> SelectionDecision: ...


class LocalSelector:
    """Skill selection by a local model — no upstream call, no credential."""

    def __init__(
        self,
        classifier: SetwiseClassifier,
        pool: InferencePool,
        catalog_manager: CatalogManager,
        settings: Settings,
    ) -> None:
        self._classifier = classifier
        self._pool = pool
        self._catalog_manager = catalog_manager
        self._settings = settings

    @property
    def catalog(self) -> Catalog:
        return self._catalog_manager.catalog

    async def select(  # noqa: ARG002 - see docstring: signature parity is the point
        self,
        conversation: list[ChatMessage],
        active_ids: list[str],
        *,
        api_key: SecretStr | None = None,
        turn_context: str | None = None,
        available_tools: list[str] | None = None,
        routing: RoutingPreference | None = None,
    ) -> SelectionDecision:
        """Classify this turn. Never raises; degrades to the prior active set.

        ``api_key``, ``available_tools`` and ``routing`` are accepted and ignored
        — they exist so this is signature-compatible with the upstream selector.
        Nothing leaves the process here, which is the entire point: no credential
        is spent, no endpoint is chosen, and the caller's conversation does not
        cross the network for a routing decision.
        """
        eligible = [m for m in conversation if m.role in ("user", "assistant", "tool")]
        if not eligible:
            return SelectionDecision.reuse(active_ids)

        # ONE catalog snapshot for the whole decision. Skill learning can swap
        # the live catalog at any moment, including while the model runs on a
        # worker thread — every read below uses this snapshot so the embedding
        # table, the visible-active projection and the output validation cannot
        # disagree about which catalog they mean.
        catalog = self.catalog

        # A background wake: the turn was started by a watcher firing, an agent
        # exiting or a queue item landing, not by the user saying anything. The
        # skill that armed that watch owns whatever comes back from it, so the
        # active set continues unchanged. Structural, not semantic — there is no
        # new instruction to classify, so ranking one would be ranking the
        # conversation's past.
        if active_ids and eligible[-1].role != "user":
            # A REUSE, deliberately not an authoritative answer: authority over
            # the docs query rides the same flag as authority over the skills,
            # and a wake has no opinion on either — marking it authoritative
            # was measured (in review) to drop a carried docs query on a
            # force/always wake round, losing the pages mid-turn.
            return SelectionDecision(
                skill_ids=list(active_ids),
                ran=False,
                degraded=False,
                task_type="background_wake",
                reason="no new user instruction; continuing the active workflow",
            )

        query = query_document(conversation, turn_context=turn_context)
        max_active = self._settings.max_active_skills
        # Only SELECTABLE actives reach the model. The prior set can carry a
        # server-pinned skill (the docs runbook) that is hidden from selection
        # and never appeared as an active id in training — measured on the
        # benchmark, injecting that one unseen id into the [loaded: …] prefix
        # changed 49 of 124 answers and emptied 36 of them. The upstream
        # selector projects with catalog.selectable() for the same reason.
        visible_active = [sid for sid in active_ids if sid in catalog.selectable_ids]
        # The catalog goes in with the query, and the embedding table is
        # reconciled against it on the worker thread. Cheap and constant in the
        # common case (a dict comparison over document digests); when skill
        # learning has just written a runbook it re-embeds that ONE skill and
        # the turn routes to it immediately.
        result = await self._pool.run(
            lambda: self._classifier.select(
                query, visible_active, catalog=catalog, max_active=max_active
            )
        )
        if result is None:
            # The pool refused or the model failed. Same posture as the upstream
            # selector failing: degrade to whatever was loaded before rather than
            # blocking the turn.
            return SelectionDecision.degraded_open(["local_skill_selector_failed_open"])

        # Validate against the SNAPSHOT even though `sync` built the table from
        # this very catalog — these ids are about to drive skill loading, and a
        # guard that costs a set lookup is cheaper than trusting that forever.
        skill_ids = [sid for sid in result.skill_ids if sid in catalog.selectable_ids]
        if result.skill_ids and not skill_ids:
            # The selector NAMED skills and none resolved. Nothing should be
            # able to produce that — the ids come from the table ``sync`` built
            # out of this very catalog snapshot — so it means table and catalog
            # disagree, and the upstream selector's parser treats the same shape
            # as malformed for the same reason.
            _log.warning("local_selector_unresolvable_answer",
                         extra={"named": result.skill_ids[:3]})
            return SelectionDecision.degraded_open(["local_skill_selector_unknown_ids"])

        top_scores = ", ".join(
            f"{k.rsplit('.', 1)[-1]}:{v:.2f}" for k, v in list(result.scores.items())[:3]
        )
        # Confidence describes the ANSWER: the first admitted skill's
        # probability. The top of the raw ranking may have been rejected by a
        # floor — or cardinality may be zero — and reporting its score would
        # attach high confidence to a skill the turn never loaded.
        top = result.scores.get(skill_ids[0], 0.0) if skill_ids else 0.0
        return SelectionDecision(
            skill_ids=skill_ids,
            ran=True,
            degraded=False,
            confidence=float(top),
            task_type="local_setwise",
            reason=f"cardinality={result.cardinality} top=[{top_scores}]",
            usage=None,
            endpoint="local",
            calls=0,
            warnings=[],
            authoritative=True,
            docs_query=_docs_query(result.docs_wanted, eligible),
        )


def _docs_query(wanted: bool, eligible: list[ChatMessage]) -> str | None:
    """The docs search string, when the docs head asked for a lookup.

    The classifier answers WHETHER to search; the query string is the cleaned
    last user message. The docs MCP is a search endpoint that ranks keywords, so
    the raw sentence works — the upstream selector's rewritten query was a
    nicety, not a requirement, and synthesising one here would need exactly the
    generative model this path exists to avoid.
    """
    if not wanted:
        return None
    for msg in reversed(eligible):
        if msg.role == "user":
            # _content_text, not a str-only read: a validated text+image message
            # carries typed content PARTS, and reading only strings silently
            # dropped the retrieval the docs head just asked for.
            cleaned = " ".join(_content_text(msg.content).split())[:120].strip()
            return cleaned or None
    return None
