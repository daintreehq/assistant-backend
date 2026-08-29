"""The local runbook selector — a drop-in replacement for the prompt-based one.

Same ``select()`` signature and same ``SelectionDecision`` return type as
``runbooks.selector.Selector``, so the orchestrator and the benchmark harness both
drive it unchanged and the two can be scored against each other on identical
evidence.

Selection itself is two stages (``setwise.SetwiseClassifier``): a cosine
shortlist over the embedded catalog, then a set-wise pass that ranks the
shortlist and predicts how many runbooks the turn needs (zero included). Whether
to retrieve documentation is decided by a SECOND pass over the bare user
question, because the head reads that decision off a context-laden query as a
flat no (``setwise.py``). The rules layer here stays deliberately thin
— the one rule it keeps, the background wake, is a STRUCTURAL fact about the
turn (there is no new user instruction at all), not a semantic judgment the
model should be second-guessed on.

**There is no staleness gate here any more, and its absence is the point.** The
previous selector owned one weight row per runbook, so a catalog whose routing
surface had moved left it ranking against documents that no longer existed; it
had to compare a stamped ``routing_revision`` and degrade every turn open until
someone retrained. This selector has no per-runbook parameters — it reads runbook
EMBEDDINGS — so a changed catalog is just different vectors. ``sync`` re-embeds
whatever moved, on the turn it moved, and selection carries on. That is what
makes a runbook written by runbook learning reachable on the next turn instead of the
next deploy — reachable, not yet competitive against its trained neighbours; see
``setwise.py`` for the measurement.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import SecretStr

from ...config import Settings
from ...contracts.openai import ChatMessage
from ...observability import get_logger
from ..catalog import Catalog, CatalogManager
from ..decision import SelectionDecision
from .documents import ACTIVE_PREFIX, fit_query, last_user_text
from .runtime import InferencePool
from .setwise import SetwiseClassifier

_log = get_logger("daintree.runbooks.selector")

#: How much of the head's ranking the fast-response stage is shown. Five rather
#: than the active cap of three on purpose: the extra two are the near-misses, and
#: they are the point. A preview written from the admitted set alone can only
#: restate what the executor already holds; the runbooks that ALMOST loaded are
#: what let it describe the shape of the task rather than the shape of the answer.
#: Costs one list slice — these ids are already ranked and already in hand.
FAST_CONTEXT_RUNBOOKS = 5


class RunbookSelector(Protocol):
    """What the orchestrator needs from a selector — nothing more.

    ONE production implementation: ``LocalSelector``, the in-process two-stage
    selector. The prompt-based one that used to sit beside it is deleted — it
    made a billed model call on the turn's first-byte path and had not served a
    request in months.

    The protocol survives that deletion because it still has two implementors,
    and the second one is the reason it must stay honest: ``ScriptedSelector``
    in ``tests/fakes.py`` drives orchestration tests and the deterministic trace
    benchmark by returning a scripted answer instead of deciding. A seam living
    in test code is the right home for it — it is not product — but it also
    means nothing stops the two signatures drifting apart, so
    ``tests/unit/test_local_selection.py`` pins them against each other.
    """

    async def select(
        self,
        conversation: list[ChatMessage],
        active_ids: list[str],
        *,
        api_key: SecretStr | None = None,
        turn_context: str | None = None,
        available_tools: list[str] | None = None,
        catalog: Catalog | None = None,
    ) -> SelectionDecision: ...


class LocalSelector:
    """Runbook selection by a local model — no upstream call, no credential."""

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
        catalog: Catalog | None = None,
    ) -> SelectionDecision:
        """Classify this turn. Never raises; degrades to the prior active set.

        ``api_key`` and ``available_tools`` are accepted and ignored
        — they exist so this is signature-compatible with the upstream selector.
        Nothing leaves the process here, which is the entire point: no credential
        is spent, no endpoint is chosen, and the caller's conversation does not
        cross the network for a routing decision.

        ``catalog`` is NOT ignored. It narrows the menu for a caller that cannot
        run the whole catalog (the read-only sub-agent profile), and honouring it
        is a correctness requirement rather than a nicety: ignoring it here would
        let a locally-selected run load an orchestration runbook the sub-agent
        has no tools to follow, and the two selector implementations would
        disagree about what a profile is allowed to see.
        """
        eligible = [m for m in conversation if m.role in ("user", "assistant", "tool")]
        if not eligible:
            return SelectionDecision.reuse(active_ids)

        # ONE catalog snapshot for the whole decision. Runbook learning can swap
        # the live catalog at any moment, including while the model runs on a
        # worker thread — every read below uses this snapshot so the embedding
        # table, the visible-active projection and the output validation cannot
        # disagree about which catalog they mean. A caller-supplied catalog is
        # already such a snapshot (and already narrowed), so it is used as-is.
        catalog = catalog if catalog is not None else self.catalog

        # A background wake: the turn was started by a watcher firing, an agent
        # exiting or a queue item landing, not by the user saying anything. The
        # runbook that armed that watch owns whatever comes back from it, so the
        # active set continues unchanged. Structural, not semantic — there is no
        # new instruction to classify, so ranking one would be ranking the
        # conversation's past.
        if active_ids and eligible[-1].role != "user":
            # A REUSE, deliberately not an authoritative answer: authority over
            # the docs query rides the same flag as authority over the runbooks,
            # and a wake has no opinion on either — marking it authoritative
            # was measured (in review) to drop a carried docs query on a
            # force/always wake round, losing the pages mid-turn.
            return SelectionDecision(
                runbook_ids=list(active_ids),
                ran=False,
                degraded=False,
                task_type="background_wake",
                reason="no new user instruction; continuing the active workflow",
            )

        max_active = self._settings.max_active_runbooks
        # Only SELECTABLE actives reach the model. The prior set can carry a
        # server-pinned runbook (the docs runbook) that is hidden from selection
        # and never appeared as an active id in training — measured on the
        # benchmark, injecting that one unseen id into the [loaded: …] prefix
        # changed 49 of 124 answers and emptied 36 of them. The upstream
        # selector projects with catalog.selectable() for the same reason.
        visible_active = [sid for sid in active_ids if sid in catalog.selectable_ids]
        # The catalog goes in with the query, and the embedding table is
        # reconciled against it on the worker thread. Cheap and constant in the
        # common case (a dict comparison over document digests); when runbook
        # learning has just written a runbook it re-embeds that ONE runbook and
        # the turn routes to it immediately.
        # ONE source question, two bounds. The docs head is only in-distribution
        # on the bare user question (see the long note at the docs decision in
        # ``setwise.py``), so it reads this untruncated; the docs MCP gets the
        # same question trimmed to a search box's worth. Same string underneath,
        # so the decision and the search cannot be about different questions,
        # but the model's input is not shortened to suit a search endpoint.
        docs_text = last_user_text(eligible)
        # Rendered against the encoder's token cap rather than blind to it. The
        # classifier truncates from the RIGHT and this string ends with the turn
        # context, so a long user message or a token-dense history used to delete
        # the evidence that matters most — silently, since a truncated query still
        # scores. A query that already fits comes back byte-identical; see
        # ``documents.fit_query``.
        query = fit_query(
            conversation,
            turn_context=turn_context,
            fits=self._classifier.query_fits,
            prefix=ACTIVE_PREFIX.format(", ".join(visible_active)) if visible_active else "",
        )
        result = await self._pool.run(
            lambda: self._classifier.select(
                query,
                visible_active,
                catalog=catalog,
                max_active=max_active,
                docs_query=docs_text,
            )
        )
        if result is None:
            # The pool refused or the model failed. Same posture as the upstream
            # selector failing: degrade to whatever was loaded before rather than
            # blocking the turn.
            return SelectionDecision.degraded_open(["local_runbook_selector_failed_open"])

        # Validate against the SNAPSHOT even though `sync` built the table from
        # this very catalog — these ids are about to drive runbook loading, and a
        # guard that costs a set lookup is cheaper than trusting that forever.
        runbook_ids = [sid for sid in result.runbook_ids if sid in catalog.selectable_ids]
        if result.runbook_ids and not runbook_ids:
            # The selector NAMED runbooks and none resolved. Nothing should be
            # able to produce that — the ids come from the table ``sync`` built
            # out of this very catalog snapshot — so it means table and catalog
            # disagree, and the upstream selector's parser treats the same shape
            # as malformed for the same reason.
            _log.warning("local_selector_unresolvable_answer", named=result.runbook_ids[:3])
            return SelectionDecision.degraded_open(["local_runbook_selector_unknown_ids"])

        if result.reserved_ids:
            # Dense retrieval did not return a runbook this conversation is
            # already running, and ``candidates.py`` seated it anyway. Harmless
            # here and the leading indicator for the whole catalog: it is how
            # often the pre-reservation ballot would have evicted a live
            # workflow on a vague follow-up. Watch the rate, not the event.
            _log.info(
                "local_selector_reserved_active",
                reserved=list(result.reserved_ids),
                retained=[sid for sid in result.reserved_ids if sid in result.runbook_ids],
            )

        top_scores = ", ".join(
            f"{k.rsplit('.', 1)[-1]}:{v:.2f}" for k, v in list(result.scores.items())[:3]
        )
        # Confidence describes the ANSWER: the first admitted runbook's
        # probability. The top of the raw ranking may have been rejected by a
        # floor — or cardinality may be zero — and reporting its score would
        # attach high confidence to a runbook the turn never loaded.
        top = result.scores.get(runbook_ids[0], 0.0) if runbook_ids else 0.0
        return SelectionDecision(
            runbook_ids=runbook_ids,
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
            # The head's own ranking, filtered against the same snapshot as the
            # admitted ids and truncated to the fast stage's width. Taken from
            # `ranked` rather than `scores` because `scores` is already a top-five
            # LOG line — reading it here would tie a prompt input to a diagnostic
            # someone could shorten without ever thinking about this.
            ranked_runbook_ids=[
                sid
                for sid, _ in result.ranked[:FAST_CONTEXT_RUNBOOKS]
                if sid in catalog.selectable_ids
            ],
            # Read from the FULL ballot, not from `result.scores` — that one is
            # the top five, kept for a log line, and the confirmation gate needs a
            # score for every admitted id. At the shipped cap of three they
            # almost always coincide; "almost always" is not a property to
            # build a user-visible behaviour on, and a runbook admitted at rank
            # six with no score would silently never announce.
            #
            # Restricted to `runbook_ids` (already validated against the
            # snapshot above) so nothing downstream can read a score for a
            # runbook the turn did not load.
            scores={
                sid: float(p)
                for sid, p in result.ranked
                if sid in set(runbook_ids)
            },
            docs_query=_docs_search_query(docs_text) if result.docs_wanted else None,
        )


#: Cap on the string handed to the docs MCP's search box.
#:
#: A SEARCH bound, and nothing else. The classifier reads the untruncated
#: question (``last_user_text``) because a cap there can slice off the actual
#: question in a long compound message; the search endpoint ranks keywords and
#: has no use for a paragraph, so it gets a short one. Cut on a word boundary —
#: a keyword index gains nothing from a severed final token.
DOCS_SEARCH_QUERY_MAX_CHARS = 120


def _docs_search_query(text: str | None) -> str | None:
    """``text`` trimmed to something worth putting in a search box."""
    if not text:
        return None
    if len(text) <= DOCS_SEARCH_QUERY_MAX_CHARS:
        return text
    clipped = text[:DOCS_SEARCH_QUERY_MAX_CHARS]
    head, _, _tail = clipped.rpartition(" ")
    # `head` is empty only for a single word longer than the cap, where the hard
    # cut is the only option left.
    return (head or clipped).strip() or None
