"""Synthetic catalogs big enough to make retrieval do its job.

## Why this exists

`k=32` against 38 selectable runbooks puts 84% of the catalog on every ballot. At
that ratio Stage 1 is barely a filter — any two queries must produce shortlists
overlapping in at least 26 candidates — so a green `candidate_miss = 0` is
evidence about the head, not about retrieval. The share reaching the head is
32/38 today, 16% at 200 runbooks and 6.4% at 500, and nothing in the repository
currently measures what happens in between.

Distractors are how that gets measured before there are 500 real runbooks to
measure it with.

## Why they cannot be noise

Padding the catalog with lorem ipsum would prove nothing: the real question is
whether the right runbook survives a shortlist crowded with runbooks that *look*
like it. Filling 460 slots with unrelated text makes retrieval's job easier than
it is today, not harder, and would produce a confidently green result that means
the opposite of what it appears to mean.

So every distractor is DERIVED from a real runbook, in one of five shapes, each
chosen because it is a way a large catalog actually goes wrong:

    paraphrase    the same job, reworded — the hardest kind, and the one a
                  growing catalog produces by accident
    scope_split   a plausible narrower slice of a real runbook's job
    sibling       the same domain, an adjacent verb, overlapping vocabulary
    collision     a runbook whose exclusion clause NAMES the real one, so the
                  routing document carries its vocabulary as a negative
    unrelated     a genuinely different domain — the easy majority, present so
                  the pool is not uniformly adversarial

The mix is deliberately weighted toward hard shapes but not made of them: a pool
of nothing but paraphrases would measure a catalog nobody would ship.

## What they are not

These are ROUTING SURFACES — id, title, summary, whenToUse — with a stub body.
The selector never reads a runbook body (`runbook_document` renders metadata only),
so a body would cost generation effort and change nothing. They are never
written to disk, never loaded by the server, and never mixed into the live
catalog: a shadow catalog is built in memory, handed to `sync()`, and dropped.

Generation is seeded and pure, so the same size always yields the same catalog
and a recall number is comparable across runs and machines.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from daintree_assistant_server.runbooks.catalog import Catalog
from daintree_assistant_server.runbooks.local.documents import runbook_document, split_when_to_use
from daintree_assistant_server.runbooks.models import Runbook, RunbookMeta

#: Fixed, because a recall number that moved with the clock would be unusable as
#: a baseline. Changing it regenerates every distractor and invalidates every
#: recorded measurement, so treat it like a schema version.
SEED = 20260821

#: Relative weights over the five shapes. Hard-heavy, not hard-only — see the
#: module docstring.
SHAPES: tuple[tuple[str, int], ...] = (
    ("paraphrase", 3),
    ("scope_split", 3),
    ("sibling", 4),
    ("collision", 3),
    ("unrelated", 7),
)

#: Vocabulary for the `unrelated` shape. Deliberately drawn from software work
#: the product does NOT do, so these are far from the real catalog in embedding
#: space without being nonsense a real encoder would find strange.
_FOREIGN_DOMAINS: tuple[tuple[str, str, str], ...] = (
    ("billing", "invoice", "reconcile a customer invoice against the ledger"),
    ("billing", "refund", "issue a partial refund and record the reason code"),
    ("hr", "onboarding", "prepare a new hire's first-week checklist"),
    ("hr", "leave", "approve a leave request and update the roster"),
    ("logistics", "shipment", "trace a delayed shipment through its carrier scans"),
    ("logistics", "inventory", "reconcile a warehouse stock count"),
    ("support", "escalation", "escalate a customer ticket to the on-call engineer"),
    ("support", "macro", "write a canned reply for a recurring question"),
    ("marketing", "campaign", "schedule a campaign send and pick its audience"),
    ("marketing", "copy", "draft landing-page copy for a launch"),
    ("legal", "contract", "review a vendor contract for renewal terms"),
    ("legal", "policy", "update a published privacy policy page"),
    ("finance", "forecast", "build a quarterly revenue forecast"),
    ("finance", "expense", "categorise an expense report for approval"),
    ("facilities", "booking", "book a meeting room for a recurring standup"),
    ("facilities", "access", "grant building access to a contractor"),
)

_QUALIFIERS = (
    "for a single target", "across an existing set", "in bulk", "for one item only",
    "when the target is already known", "as a follow-up step", "before the work starts",
    "after the work completes", "for a named subset", "without changing anything else",
)

_VERBS = (
    "inspect", "summarise", "reconcile", "prepare", "annotate", "archive",
    "restore", "reorder", "audit", "escalate", "duplicate", "retire",
)

#: Per-distractor variation, so that two distractors derived from the same
#: source are not the same DOCUMENT.
#:
#: This is not decoration. The selector embeds a rendered document, and the id is
#: not part of it (`runbook_document` renders title/summary/whenToUse only), so a
#: template that varied only the id produced text-identical rows: 961 distractors
#: collapsed to 492 distinct documents, one repeated seven times. Identical
#: vectors still occupy separate shortlist slots, so "1,000 runbooks" was really
#: ~530 routing surfaces crowding each other out by multiplicity — which is not
#: what a growing catalog does, and made everything past 200 unreadable.
_FACETS = (
    "when the request names a specific target",
    "when the request is phrased as a question",
    "when the work is part of a larger review",
    "when a previous attempt has already failed",
    "when the user is working from a written checklist",
    "when the result has to be reported back verbatim",
    "when the operation must be reversible",
    "when more than one person is waiting on the outcome",
    "when the state has already been captured elsewhere",
    "when the change is expected to be temporary",
    "when the request arrives mid-task",
    "when nothing has been set up yet",
)

_FORMS = ("written report", "checklist", "summary note", "audit trail")

_REPHRASINGS = (
    ("Use when", "Reach for this when"),
    ("Use when", "Applies when"),
    ("Use when", "The right runbook when"),
    ("Use when", "Pick this when"),
    ("Use when", "Choose this when"),
    ("Use when", "Follow this when"),
)


@dataclass(frozen=True)
class Distractor:
    """One synthetic runbook and the shape it was built to imitate."""

    runbook: Runbook
    shape: str
    #: The real runbook it was derived from, or ``None`` for ``unrelated``.
    derived_from: str | None


def _trigger(when: str) -> str:
    """The positive trigger of a whenToUse, with its "Use when..." lead stripped.

    Every catalog entry opens the same way, so re-prefixing without stripping
    produced "Use when use when the user wants..." on a third of the pool — text
    no real runbook would carry, which makes the distractor easy to tell apart for
    the wrong reason.
    """
    head, _ = split_when_to_use(when)
    stripped = head.strip()
    # Every lead-in the live catalog actually uses, counted rather than guessed:
    # 33 "Use when", 3 "Use for", 1 each of "Use whenever" and "Use ONLY". Missing
    # one produces "Use when Use for a one-off..." — text no real runbook carries,
    # which makes the distractor separable on a formatting tell instead of on
    # meaning.
    for lead in ("Use whenever ", "Use ONLY ", "Use only ", "Use when ", "Use for "):
        if stripped.lower().startswith(lead.lower()):
            stripped = stripped[len(lead):]
            break
    return stripped or "the same situation applies"


#: How often an exclusion clause names its source by DOTTED ID rather than by
#: title. Calibrated, not chosen: the real catalog averages 4.1 outgoing id
#: references per runbook (max 9), and three of the five distractor shapes carry an
#: exclusion clause — so naming the id every time would give each real runbook a
#: fan-IN of dozens of synthetic rows carrying its exact dotted id.
#:
#: That matters because the query carries the active ids literally, through
#: ``ACTIVE_PREFIX``, while a runbook's OWN document does not contain its id at all.
#: Distractors naming it would therefore hold an exact token sequence the target
#: itself lacks, and the dense-retrieval number would be measuring an artifact of
#: the generator rather than catalog growth. One in four keeps the real
#: catalog's negative-clause structure without manufacturing that advantage.
_ID_REFERENCE_EVERY = 4


def _refer_to(real: Runbook, n: int) -> str:
    """How an exclusion clause names the runbook it defers to. See above."""
    return real.id if n % _ID_REFERENCE_EVERY == 0 else f"the {real.title.lower()} runbook"


def _meta(runbook_id: str, title: str, summary: str, when: str) -> RunbookMeta:
    return RunbookMeta.model_validate(
        {"id": runbook_id, "title": title, "summary": summary, "whenToUse": when}
    )


def _paraphrase(real: Runbook, n: int, seq: int) -> tuple[str, str, str, str]:
    """The same job, said differently. The hardest shape to filter.

    It has to be a REWORDING, not a copy. Copying the source's summary and
    whenToUse verbatim does not produce a distractor at all — it produces an
    ALIAS, a second entry that genuinely answers the same request, and then
    scores the run wrong for retrieving it. The vocabulary must overlap heavily
    (that is the difficulty) while the text stays distinct.
    """
    lead, replacement = _REPHRASINGS[seq % len(_REPHRASINGS)]
    facet = _FACETS[(seq // len(_REPHRASINGS)) % len(_FACETS)]
    _, tail = split_when_to_use(real.meta.when_to_use)
    trigger = _trigger(real.meta.when_to_use)
    return (
        f"shadow.para{n:04d}.{real.id.rsplit('.', 1)[-1]}",
        f"{real.title}, second route",
        f"An alternate route to the same outcome: {real.summary[0].lower()}"
        f"{real.summary[1:]} Documented separately {facet}.",
        f"{replacement} {trigger} — particularly {facet}. {tail}".strip(),
    )


def _scope_split(real: Runbook, n: int, seq: int) -> tuple[str, str, str, str]:
    """A narrower slice of a real runbook's job — a plausible catalog mistake."""
    qualifier = _QUALIFIERS[seq % len(_QUALIFIERS)]
    facet = _FACETS[(seq // len(_QUALIFIERS)) % len(_FACETS)]
    return (
        f"shadow.scope{n:04d}.{real.id.rsplit('.', 1)[-1]}",
        f"{real.title}, {qualifier}",
        f"{real.summary} Narrowed to the case {qualifier}, {facet}.",
        f"Use when {_trigger(real.meta.when_to_use)} and the request is "
        f"{qualifier}, {facet}. Do not use for the general case "
        f"({_refer_to(real, n)}).",
    )


def _sibling(real: Runbook, n: int, seq: int) -> tuple[str, str, str, str]:
    """Same domain, adjacent verb: overlapping nouns, different intent."""
    domain = real.id.rsplit(".", 1)[0]
    verb = _VERBS[seq % len(_VERBS)]
    facet = _FACETS[(seq // len(_VERBS)) % len(_FACETS)]
    noun = real.title.split()[-1].lower()
    return (
        f"{domain}.{verb}-{noun}-{n:04d}",
        f"{verb.capitalize()} {noun}",
        f"{verb.capitalize()} the {noun} rather than changing it — {facet}.",
        f"Use when the user wants to {verb} a {noun} in the same area as "
        f"{real.title.lower()}, {facet}. Do not use when they want to "
        f"{real.title.lower()} ({_refer_to(real, n)}).",
    )


def _collision(real: Runbook, n: int, seq: int) -> tuple[str, str, str, str]:
    """A runbook whose EXCLUSION clause carries the real runbook's vocabulary.

    The shape the repository already measured going wrong: a lexical shortlist
    scored these highly for the very query they exclude, because the words match
    and the negation does not survive bag-of-words. A dense retriever should do
    better; this is what asks it to prove that.
    """
    form = _FORMS[seq % len(_FORMS)]
    facet = _FACETS[(seq // len(_FORMS)) % len(_FACETS)]
    return (
        f"shadow.near{n:04d}.{real.id.rsplit('.', 1)[-1]}",
        f"{form.capitalize()} on {real.title.lower()}",
        f"Produce a {form} about the same subject as {real.title.lower()}, without "
        f"performing it — {facet}.",
        f"Use when the user wants a {form.upper()} rather than the action itself, "
        f"{facet}. Do not use when {_trigger(real.meta.when_to_use)} — that is "
        f"{_refer_to(real, n)}.",
    )


def _unrelated(n: int, seq: int) -> tuple[str, str, str, str]:
    domain, topic, job = _FOREIGN_DOMAINS[seq % len(_FOREIGN_DOMAINS)]
    qualifier = _QUALIFIERS[(seq // len(_FOREIGN_DOMAINS)) % len(_QUALIFIERS)]
    facet = _FACETS[
        (seq // (len(_FOREIGN_DOMAINS) * len(_QUALIFIERS))) % len(_FACETS)
    ]
    return (
        f"shadow.{domain}.{topic}-{n:04d}",
        f"{topic.capitalize()} handling, {qualifier}",
        f"Handle {topic}: {job}, {qualifier}.",
        f"Use when the user wants to {job} {qualifier}, {facet}. Do not use for "
        f"anything outside {domain}.",
    )


def generate_distractors(count: int, real: Catalog, *, seed: int = SEED) -> list[Distractor]:
    """``count`` synthetic runbooks derived from ``real``. Deterministic."""
    rng = random.Random(seed)
    sources = [s for s in real.runbooks if s.id in real.selectable_ids]
    shapes = [name for name, weight in SHAPES for _ in range(weight)]
    # The seed's ONLY job, and it has to have one: it decides which source gets
    # which shape. Everything else is now indexed arithmetic (see the per-shape
    # counters below), so without this a "different seed" would regenerate the
    # identical pool and a second sample would be no evidence at all. Shuffled
    # rather than offset so the two cycles do not stay in lockstep.
    rng.shuffle(sources)
    rng.shuffle(shapes)

    out: list[Distractor] = []
    per_shape: dict[str, int] = dict.fromkeys((name for name, _ in SHAPES), 0)
    seen: set[str] = {s.id for s in real.runbooks}
    # Rendered DOCUMENTS, not ids. The selector embeds the document and the id is
    # not in it, so two rows with different ids and the same text are one vector
    # occupying two shortlist slots. Enforced rather than hoped for: the first
    # version of these templates produced 492 distinct documents for 961
    # distractors, one repeated seven times, which made "1,000 runbooks" mean
    # ~530 routing surfaces plus multiplicity.
    documents: set[str] = {runbook_document(s.meta) for s in real.runbooks}
    n = 0
    #: A template can legitimately collide a few times before its variation
    #: space rolls over; a bound keeps a genuinely exhausted one from spinning.
    attempts_left = max(count * 50, 1000)
    while len(out) < count:
        if attempts_left <= 0:
            raise RuntimeError(
                f"generated only {len(out)} of {count} distinct distractors — the "
                f"templates' variation space is exhausted. Widen _FACETS, "
                f"_QUALIFIERS or _FOREIGN_DOMAINS rather than allowing duplicates: "
                f"a repeated routing document is one vector in several slots."
            )
        attempts_left -= 1
        shape = shapes[n % len(shapes)]
        source = sources[n % len(sources)]
        # A per-SHAPE counter, so each template walks its own variation space in
        # order. Driving them all off the global `n` aliased the cycles together
        # — `unrelated` exhausted after ~160 rows while its templates could
        # express 1,920 — and the uniqueness guard below turned that into a hard
        # failure rather than a silent duplicate.
        seq = per_shape[shape]
        per_shape[shape] += 1
        if shape == "paraphrase":
            parts = _paraphrase(source, n, seq)
        elif shape == "scope_split":
            parts = _scope_split(source, n, seq)
        elif shape == "sibling":
            parts = _sibling(source, n, seq)
        elif shape == "collision":
            parts = _collision(source, n, seq)
        else:
            parts = _unrelated(n, seq)
        n += 1
        runbook_id = parts[0]
        if runbook_id in seen:
            continue
        meta = _meta(*parts)
        document = runbook_document(meta)
        if document in documents:
            continue
        seen.add(runbook_id)
        documents.add(document)
        out.append(
            Distractor(
                runbook=Runbook(
                    meta=meta,
                    # The selector never reads a body; see the module docstring.
                    body=f"Shadow runbook for {parts[1]}. Not a real procedure.",
                ),
                shape=shape,
                derived_from=None if shape == "unrelated" else source.id,
            )
        )
    return out


def shadow_catalog(size: int, real: Catalog, pool: list[Distractor]) -> Catalog:
    """The real catalog padded from ``pool`` to ``size`` total runbooks.

    Nested by construction — ``shadow_catalog(200, ...)`` is a prefix of
    ``shadow_catalog(500, ...)`` — which is what makes a sweep affordable. The
    classifier's digest-keyed vector memo survives table replacement, so
    embedding the LARGEST size first leaves every smaller one free.
    """
    needed = size - len(real.runbooks)
    if needed < 0:
        raise ValueError(
            f"shadow size {size} is smaller than the real catalog ({len(real.runbooks)})"
        )
    if needed > len(pool):
        raise ValueError(f"pool holds {len(pool)} distractors, need {needed}")
    return Catalog([*real.runbooks, *(d.runbook for d in pool[:needed])])
