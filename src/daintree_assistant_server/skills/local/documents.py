"""How a skill and a conversation are rendered into text the rankers score.

This is the single source of truth for both sides of the pipeline, and it lives
in the SHIPPED package rather than in ``training/`` on purpose. A bi-encoder is
only as good as the agreement between how a document was embedded at build time
and how a query is embedded at request time: if the trainer rendered a skill even
slightly differently from the server, the two are comparing vectors from
different spaces, and the resulting quality loss looks like nothing — no error,
no warning, just mediocre rankings that get blamed on the model.

``training/common.py`` imports from here. The dependency runs that way round
because ``training/`` is not part of the package, the build, or the image; the
server can never import it, so it cannot be the authority.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

# Where a whenToUse line stops describing what the skill IS and starts listing
# what it is NOT. Every catalog entry follows the same house style — positive
# trigger first, then the routing exclusions — and the two halves have very
# different value to the two models.
_EXCLUSION_MARKERS = ("Do not use", "do not use", "Never use", "never use")


def split_when_to_use(when: str) -> tuple[str, str]:
    """Split a whenToUse line into (positive trigger, routing exclusions)."""
    positions = [when.find(m) for m in _EXCLUSION_MARKERS]
    found = [p for p in positions if p != -1]
    if not found:
        return when.strip(), ""
    cut = min(found)
    return when[:cut].strip(), when[cut:].strip()


def skill_document(meta: Any, *, variant: str = "full") -> str:
    """Render one skill as the document a ranker scores against.

    The two models pay for document length in different currencies, so they get
    different documents:

    - The **bi-encoder** embeds each skill ONCE, into a stored index. Length is
      free at request time, so it gets everything — the exclusion clauses
      included, since their vocabulary genuinely helps pull the right
      neighbourhood.
    - The **cross-encoder** re-reads its documents on EVERY request and its cost
      is linear in sequence length. Measured on the real catalog: a pool of 10 at
      the ``full`` variant (mean 237 tokens) costs ~90ms, the same pool at
      ``short`` (mean 45 tokens) costs ~20ms. Handing it the exclusion clauses
      would more than quadruple the single number this whole path exists to cut.

    That trade is only safe because the reranker is FINE-TUNED: boundary
    negatives mined from the exclusion clauses are in its training set, so it
    learns "this query belongs to Y, not X" as weights instead of needing to
    re-read the rule at inference. The knowledge moves from the prompt into the
    parameters — which is the entire reason a small local model can replace a
    large prompted one here.
    """
    title = (getattr(meta, "title", "") or "").strip()
    summary = (getattr(meta, "summary", "") or "").strip()
    when = (getattr(meta, "when_to_use", "") or "").strip()
    head, _tail = split_when_to_use(when)

    if variant == "full":
        return f"{title}. {summary} {when}".strip()
    if variant == "short":
        return f"{title}. {summary}".strip()
    if variant == "trigger":
        return f"{title}. {summary} {head}".strip()
    if variant == "when":
        return when
    if variant == "summary_when":
        return f"{summary} {when}".strip()
    raise ValueError(f"unknown skill document variant: {variant!r}")


#: Which rendering the bi-encoder's stored index uses. Free at request time — the
#: index is built once — so it gets everything, exclusion clauses included.
INDEX_DOC_VARIANT = "full"

#: Which rendering the cross-encoder scores at request time.
#:
#: Set from training/sweep_reranker.py, and it is a genuine latency/accuracy
#: trade rather than a free choice: cost is linear in sequence length, measured
#: at ~20ms for a pool of 10 at `short` (~45 tokens) against ~90ms at `full`
#: (~237 tokens). `full` is affordable here only because the call being replaced
#: costs 2044ms — in a normal reranking setting it would not be.
SERVING_DOC_VARIANT = "full"

#: Prefix marking a candidate that is ALREADY LOADED for this conversation.
#:
#: The prompted selector is handed a "Currently active skills:" line and an
#: explicit instruction that omitting one EVICTS it mid-runbook. The local ranker
#: had no equivalent, and the cost of that was measurable and specific: with
#: enough conversation history in the query it scored continuation 9/9 and
#: topic-shift 1/9; with none it scored 4/9 and 6/9. Every history budget in
#: between traded one against the other and none won both, because a single
#: query vector cannot separate "this continues the loaded work" from "this
#: leaves it behind" — the two turns look almost identical, and what
#: distinguishes them is a fact about STATE, not about the words.
#:
#: Marking the candidate instead of the query puts that fact where the
#: cross-encoder can weigh it per skill: it sees "this specific runbook is the
#: one already open" and learns, from continuation and topic-shift examples
#: alike, when that earns the slot and when it does not.
ACTIVE_MARKER = "[currently loaded] "


def mark_active(document: str, *, active: bool) -> str:
    """Prefix a candidate document when that skill is already loaded."""
    return f"{ACTIVE_MARKER}{document}" if active else document


#: Prefix carrying conversation STATE into the classifier's QUERY text.
#:
#: The ``ACTIVE_MARKER`` above is the same idea for a per-candidate document; the
#: classifier has no per-candidate document to attach it to, so the fact goes in
#: the query instead. Continuation and topic-shift turns are near-identical in
#: wording and differ only in which skills are already open, so the model cannot
#: separate them from the words alone. Measured without it: continuation 9/9 and
#: topic-shift 1/9 at one history budget, 4/9 and 6/9 at another — a pure trade
#: with no setting winning both.
#:
#: Lives HERE, beside the rest of the render contract, because BOTH sides of the
#: pipeline must format it identically: the trainer builds every training input
#: with it and the server builds every query with it. It was previously defined
#: twice — once here-ish in ``classifier.py`` and once in
#: ``training/train_classifier.py`` — as equal literals with nothing checking
#: that they stayed equal, so editing one would have silently trained a model on
#: a prefix the server never sends.
ACTIVE_PREFIX = "[loaded: {}] "


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for chunk in content:
            # Validated wire messages carry TYPED content parts (pydantic
            # models with a .text attribute), not raw dicts — a text+image
            # message rendered as "" here until that was caught in review.
            # All three shapes are accepted so the renderer keeps working on
            # raw fixtures and validated requests alike.
            if isinstance(chunk, str):
                parts.append(chunk)
            elif isinstance(chunk, dict) and isinstance(chunk.get("text"), str):
                parts.append(chunk["text"])
            else:
                text = getattr(chunk, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


#: How much preceding conversation rides in the query, in characters.
#:
#: Not zero, and not the whole transcript. The newest instruction alone cannot
#: distinguish "how's it going?" continuing a fleet supervision from the same
#: words opening a new session, and those resolve to different answers. A bounded
#: tail of prior turns supplies exactly that signal. Kept small because the
#: failure in the other direction is worse and quieter: with a long history the
#: query vector drifts toward whatever the conversation has been ABOUT, and a
#: genuine topic shift keeps ranking the old workflow first.
QUERY_HISTORY_CHARS = 320


def query_document(
    messages: Sequence[Any],
    *,
    turn_context: str | None = None,
    history_chars: int = QUERY_HISTORY_CHARS,
) -> str:
    """Render a conversation as the query string the rankers embed.

    The newest eligible message is always present, mirroring the prompt
    selector's unconditional admission of it: a query built from history alone
    describes a task the ranker was never shown.
    """
    eligible = [
        m for m in messages if getattr(m, "role", None) in ("user", "assistant", "tool")
    ]
    if not eligible:
        return (turn_context or "").strip()

    newest = _content_text(getattr(eligible[-1], "content", "")).strip()

    prior: list[str] = []
    if history_chars > 0 and len(eligible) > 1:
        budget = history_chars
        for msg in reversed(eligible[:-1]):
            text = _content_text(getattr(msg, "content", "")).strip()
            if not text:
                continue
            text = text[:budget]
            prior.append(text)
            budget -= len(text)
            if budget <= 0:
                break
        prior.reverse()

    parts = [*prior, newest]
    if turn_context:
        parts.append(turn_context.strip())
    return " ".join(p for p in parts if p).strip()


def routing_revision(catalog: Any) -> str:
    """Hash of the catalog's ROUTING surface — what the classifier trains on.

    Deliberately narrower than ``catalog.revision``, which hashes whole skill
    BODIES: the classifier reads only id/title/summary/whenToUse, so a body-only
    edit (the common skill-learning case) must not invalidate the head. The
    trainer stamps this into the artifact at TRAIN time; the export copies it
    verbatim; serving recomputes it from the live catalog and refuses to serve
    on mismatch. Stamping at export was measured to be a false-provenance trap:
    retraining nothing and re-exporting would have blessed stale weights with a
    fresh revision.
    """
    import hashlib

    selectable = set(catalog.selectable_ids)
    parts = []
    for skill in catalog.skills:
        meta = skill.meta
        if meta.id not in selectable:
            continue
        parts.append(
            f"{meta.id}\x1f{meta.title}\x1f{meta.summary}\x1f{meta.when_to_use}"
        )
    digest = hashlib.sha256("\x1e".join(sorted(parts)).encode()).hexdigest()
    return f"routing:{digest[:32]}"
