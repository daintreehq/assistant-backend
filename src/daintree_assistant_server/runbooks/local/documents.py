"""How a runbook and a conversation are rendered into text the rankers score.

This is the single source of truth for both sides of the pipeline, and it lives
in the SHIPPED package rather than in ``training/`` on purpose. A bi-encoder is
only as good as the agreement between how a document was embedded at build time
and how a query is embedded at request time: if the trainer rendered a runbook even
slightly differently from the server, the two are comparing vectors from
different spaces, and the resulting quality loss looks like nothing — no error,
no warning, just mediocre rankings that get blamed on the model.

``training/common.py`` imports from here. The dependency runs that way round
because ``training/`` is not part of the package, the build, or the image; the
server can never import it, so it cannot be the authority.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

# Where a whenToUse line stops describing what the runbook IS and starts listing
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


def runbook_document(meta: Any, *, variant: str = "full") -> str:
    """Render one runbook as the document a ranker scores against.

    Both selector stages embed a runbook ONCE — at build time, or on catalog load
    for a runbook that appeared afterwards — and read the vector thereafter. Length
    is therefore free at request time, which is why the shipped variant is
    ``full``: the exclusion clauses ("do not use for X") are the routing signal,
    and they sit at the END of a whenToUse line. Measured on the real catalog,
    runbook documents run to a p50 of 262 tokens and a max of 368, so a document
    cap set for the conversation side would silently cut exactly the clauses that
    separate neighbouring runbooks.

    The narrower variants remain because the trainers can select one, and because
    ``trigger``/``short`` are the ablation: they answer "how much of the routing
    is the positive description doing, and how much is the fence".
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
    raise ValueError(f"unknown runbook document variant: {variant!r}")


#: Which rendering the runbook embedding table uses. Free at request time — a runbook
#: is embedded once and looked up thereafter — so it gets everything, exclusion
#: clauses included.
INDEX_DOC_VARIANT = "full"

#: Prefix carrying conversation STATE into the selector's QUERY text.
#:
#: Continuation and topic-shift turns are near-identical in wording and differ
#: only in which runbooks are already open, so the model cannot separate them from
#: the words alone. Measured without it: continuation 9/9 and topic-shift 1/9 at
#: one history budget, 4/9 and 6/9 at another — a pure trade with no setting
#: winning both.
#:
#: The set-wise head ALSO gets the same fact per candidate, as the ``active``
#: feature: the prefix tells the encoder that a workflow is running, the feature
#: tells the head which specific slot is the one already open. Both, because they
#: answer different halves of the question.
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


#: Marks where the middle of an over-long newest message was removed.
_ELLIPSIS = " … "


def _head_and_tail(text: str, budget: int) -> str:
    """``text`` shortened to ``budget`` characters, keeping both ends.

    Head AND tail, not a prefix, because the two halves of a long message carry
    different things and either can be the decisive one. A pasted stack trace
    followed by "so why is it failing?" has its ask at the END; a request
    followed by supporting detail has it at the start. Cutting from one side
    reliably loses one of those cases, and cutting from the middle loses the
    part that is usually context for both.
    """
    if budget <= 0 or len(text) <= budget:
        return text
    keep = max(budget - len(_ELLIPSIS), 1)
    head = keep - keep // 2
    return text[:head] + _ELLIPSIS + text[len(text) - (keep - head):]


def query_document(
    messages: Sequence[Any],
    *,
    turn_context: str | None = None,
    history_chars: int = QUERY_HISTORY_CHARS,
    newest_chars: int | None = None,
) -> str:
    """Render a conversation as the query string the rankers embed.

    The newest eligible message is always present, mirroring the prompt
    selector's unconditional admission of it: a query built from history alone
    describes a task the ranker was never shown.

    ``newest_chars`` bounds that newest message, head and tail. It is ``None``
    on every ordinary call and is only set by :func:`fit_query`, on a query that
    would otherwise be cut by the encoder's token cap — see there for why the
    default path stays byte-identical.
    """
    eligible = [
        m for m in messages if getattr(m, "role", None) in ("user", "assistant", "tool")
    ]
    if not eligible:
        return (turn_context or "").strip()

    newest = _content_text(getattr(eligible[-1], "content", "")).strip()
    if newest_chars is not None:
        newest = _head_and_tail(newest, newest_chars)

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


#: Successive (history_chars, newest_chars) budgets tried when a rendered query
#: will not fit the encoder's cap. The FIRST entry is today's default, so a query
#: that already fits is rendered exactly as before and never reaches the rest.
#:
#: The order encodes what is worth losing, cheapest first:
#:
#: 1. **Prior history.** Bounded evidence about what the conversation WAS. Useful,
#:    and the thing the turn can most afford to lose.
#: 2. **The newest message's middle.** Head and tail are kept (see
#:    :func:`_head_and_tail`); the middle of a long paste is the least decisive
#:    part of it.
#: 3. **Nothing else.** The turn context is never dropped, because it carries the
#:    current goal and the top workflow digest, and because it is what the
#:    tokenizer would have thrown away first — that being the whole reason this
#:    schedule exists.
_FIT_SCHEDULE: tuple[tuple[int, int | None], ...] = (
    (QUERY_HISTORY_CHARS, None),
    (160, None),
    (0, None),
    (0, 1200),
    (0, 600),
    (0, 280),
)


def fit_query(
    messages: Sequence[Any],
    *,
    turn_context: str | None = None,
    fits: Callable[[str], bool],
    prefix: str = "",
) -> str:
    """The query string, shrunk until the encoder will take all of it.

    ## Why this exists

    The renderer cannot see the token cap and the cap cannot see the renderer.
    ``SetwiseClassifier`` truncates at 256 tokens FROM THE RIGHT, and this string
    ends with the turn context — so a long enough user message, or a token-dense
    enough history, silently deletes the end of the evidence. Measured: 320
    characters of path-like tool output is 320 tokens (the same 320 characters of
    one unbroken word is 3), and three such messages push the newest INSTRUCTION
    out entirely.

    Losing the tail is not a smaller version of the same thing. It is losing the
    turn context first, then the instruction — precisely the two most important
    channels — while every accuracy number carries on looking normal, because a
    truncated query still scores.

    ## Why this does not need a retrain

    The first schedule entry is the current default, so **a query that fits is
    returned byte-identical**. Measured over everything the model has ever been
    judged on: the 9,774-row training corpus peaks at 155 tokens and the 177
    benchmark queries at 130, against a cap of 256. Neither ever reaches the
    shrink path, so the training distribution is untouched and this is not a
    change the weights have to be re-fit to.

    What it does change is the over-cap case, and there it moves TOWARD the
    training distribution rather than away: the trainer renders
    ``[…] [turn_context]``, and a hard right-truncation produces a string ending
    mid-sentence with no context at all, which is a shape the head has never
    seen.

    ``fits`` is supplied by the caller because the tokenizer belongs to the
    artifact, not to this module; ``prefix`` is the ``[loaded: …]`` block, which
    is prepended downstream and so has to be counted here.
    """
    text = ""
    for history_chars, newest_chars in _FIT_SCHEDULE:
        text = query_document(
            messages,
            turn_context=turn_context,
            history_chars=history_chars,
            newest_chars=newest_chars,
        )
        if fits(prefix + text):
            return text
    # Every budget exhausted — a turn context alone can exceed the cap. Return
    # the smallest render and let the encoder truncate: there is nothing further
    # to give up, and a hard cut is still better than refusing to rank.
    return text


def last_user_text(messages: Sequence[Any]) -> str | None:
    """The newest user message, whitespace-collapsed. The DOCS decision's input.

    Lives here beside :func:`query_document` for the same reason that one does:
    this is a render contract, and both sides of the pipeline must apply it
    identically. The serving selector, the canonical evaluation and the export
    parity gate all call this, so a benchmark number cannot be produced from a
    string the server would never build.

    Deliberately NOT truncated. It used to be, back when its only consumer was
    the docs MCP's search box and 120 characters was plenty for a keyword query.
    It now also feeds the classifier, and a cap there is a different thing
    entirely: it can slice away the actual question in a long compound message,
    or cut mid-word and hand the tokenizer a fragment. The ranker's own 256-token
    bound is the right limit for a model input; the search string is capped
    separately, at its own call site, where the cap means what it says.
    """
    for msg in reversed(list(messages)):
        if getattr(msg, "role", None) != "user":
            continue
        cleaned = " ".join(_content_text(getattr(msg, "content", "")).split()).strip()
        return cleaned or None
    return None


def routing_revision(catalog: Any) -> str:
    """Hash of the catalog's ROUTING surface — the text the selector trains on.

    Deliberately narrower than ``catalog.revision``, which hashes whole runbook
    BODIES: the selector reads only id/title/summary/whenToUse, so a body-only
    edit (the common runbook-learning case) leaves it untouched.

    **This no longer gates anything at request time.** Under the fixed-head
    classifier it did: a head with one weight row per runbook could not name a
    runbook it had not trained on, so a mismatch had to degrade every turn open.
    The two-stage selector has no per-runbook weights, so a moved catalog is
    answered rather than refused, and this hash survives as a DRIFT SIGNAL — the
    trainer stamps it, the baseline records it, and a human reading a release can
    see how far the corpus has fallen behind the catalog. Stamping it at export
    rather than at train time was measured to be a false-provenance trap:
    retraining nothing and re-exporting would bless stale weights with a fresh
    revision.
    """
    import hashlib

    selectable = set(catalog.selectable_ids)
    parts = []
    for runbook in catalog.runbooks:
        meta = runbook.meta
        if meta.id not in selectable:
            continue
        parts.append(
            f"{meta.id}\x1f{meta.title}\x1f{meta.summary}\x1f{meta.when_to_use}"
        )
    digest = hashlib.sha256("\x1e".join(sorted(parts)).encode()).hexdigest()
    return f"routing:{digest[:32]}"
