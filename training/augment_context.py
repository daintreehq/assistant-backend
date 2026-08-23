"""Deterministic corpus augmentation for CONTEXT-BEARING turns.

Two measured gaps in `selection.jsonl`, both of which made the shipped selector
fail on ordinary conversations, and neither of which any benchmark could see
because the benchmark was generated from the same assumptions as the corpus.

**1. A social lead-in erases the task.** Of 2,116 rows carrying history, 43 open
on a greeting — and every one of those is terse, answered in a single clause:
`user: hey` / `assistant: Hi — workspace is idle, nothing running.` 2.0%, none of
it at the length or register a real session opens with. So "How are you?"
answered with an actual status paragraph is a shape the head has effectively
never seen, and it answers with the zero-cardinality prior it learned for
chatter. Measured on the shipped artifact, "How do I create worktrees in this
app?" ranks `create-plain-worktree` at 1.000 cold, 0.248 behind a two-line
greeting, and 0.005 behind a short one — and cardinality collapses to 0, so the
turn loads NOTHING. It is not dilution: 105 characters of meaningless padding in
the same slot leaves the score at 0.998. It is the greeting itself.

**2. Sixty-four documentation questions are labelled `is_docs=False`.** They sit
in `no_task_stateful`, whose assembler branch hardcodes the flag, so every
usage question that arrived with history or an active set was trained as NOT a
docs turn — "what actually is a watcher", "how do i change which coding agent
daintree uses by default", "where does daintree put worktrees on disk". These are
not merely missing positives; they are the same question labelled both ways, and
they are why `EXPERIMENTS.md` measures 113/120 docs recall cold against 5/120
with a runbook loaded. The shipped mitigation (re-reading the docs logit from a
second pass over the bare question) works around exactly this, and stays.

The additions here also cover the phrasing gaps `EXPERIMENTS.md` names as the
next retrain's job: deixis ("in this app" — 0 of 207 docs rows), second person
("how do you …" — 0 of 207), conversational lead-ins (1 of 207) and compound
questions.

Everything is derived from rows already in the corpus by explicit transformation
rather than invented, so a label can only be as wrong as the row it came from.

**This applies ONCE to a given base, and refuses to apply twice.** The generation
is deterministic for a fixed input corpus — same seed, same pools, same journal —
but `assemble_data` is cumulative by design (the committed corpus IS the base,
journals only extend it), so running the pair again would ADD a second sampling
of the same families rather than reproduce the first: measured, 9,903 rows became
10,405 and docs positives 625 became 957. The guard below stops that. To
regenerate rather than extend, restore the pre-augmentation corpus first:

    git show <commit-before-this-one>:training/data/selection.jsonl \
        > training/data/selection.jsonl

    python -m training.augment_context          # relabels + writes the journal
    python -m training.assemble_data --journal training/data/journal_context.jsonl

The second command is what actually re-gates and rewrites `selection.jsonl`
(contamination against the benchmark, dedupe, unknown ids).
"""

from __future__ import annotations

import json
import random
import re
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
CORPUS = DATA / "selection.jsonl"
JOURNAL = DATA / "journal_context.jsonl"

SEED = 20260820


def _norm(text: str) -> str:
    """Match ``assemble_data``'s normalisation, so "authority" means the same
    thing on both sides of the pipeline."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())

# --------------------------------------------------------------------------
# Social lead-ins. The shape the corpus has none of: a turn or two of pure
# pleasantry, carrying no task, before the first real instruction. Assistant
# replies are written the way this server actually answers a greeting — a short
# status line — because that is the text that lands in the history budget.
# --------------------------------------------------------------------------
SOCIAL: list[tuple[str, str]] = [
    ("How are you?",
     "Good — thanks. Everything's quiet here: worktree develop is stable, "
     "and one terminal is open. How can I help?"),
    ("hey", "Hey! What are we working on?"),
    ("morning", "Morning. Nothing running right now — what's first?"),
    ("good morning", "Good morning. Everything's idle. Where do we start?"),
    ("you there?", "Here. What do you need?"),
    ("hows it going", "All quiet — two worktrees, no agents running. What's up?"),
    ("hi", "Hi — ready when you are."),
    ("yo", "Yo. What are we doing?"),
    ("hey, you around?", "Around. What's the task?"),
    ("thanks for yesterday", "Any time. What's on today?"),
    ("hows your day going", "Going fine. Nothing queued here — what's first?"),
    ("still awake?", "Still here. What do you need?"),
    ("sup", "Not much — nothing running. What do you need?"),
    ("hello", "Hello. Everything's idle — what would you like to do?"),
]

# --------------------------------------------------------------------------
# The contradictions. Curated BY HAND from the 64 Daintree-domain questions that
# `no_task_stateful` labels `is_docs=False`; matched on the exact query string so
# a corpus edit that changes the wording drops the relabel rather than silently
# moving a different row. Four of the 64 are deliberately NOT here: a pasta
# recipe (off-domain, matched only on the word "recipe"), and three general git
# concept questions ("what is a git worktree actually", the clone-twice
# comparison, the etymology one) which the product documentation does not own.
# --------------------------------------------------------------------------
RELABEL_TO_DOCS: frozenset[str] = frozenset({
    "what actually is a watcher, like whats the difference between that and an async wait",
    "how do i change which coding agent daintree uses by default?",
    "how do i create a worktree from the ui rather than asking you?",
    "where do i change the keybinding for switching tabs?",
    "where does daintree store its config, and can i keep it out of git?",
    "what does daintree do with the worktree once the pr is merged?",
    "what does auto-approve actually cover?",
    "whats the difference between a watcher and an async wait?",
    "where does daintree put worktrees on disk?",
    "do agent terminals burn my own api credit?",
    "how do i see the raw check output in daintree?",
    "does deleting a worktree delete the branch too?",
    "how many agents can i have pinned at once?",
    "where are recipes stored?",
    "how does daintree know an agent is asking a question rather than just idle?",
    "do spawned agents get daintree tools themselves?",
    "whats the difference between completed and exited on a terminal?",
    "can agents talk to each other directly or does everything go through you?",
    "is project memory per project or global?",
    "does daintree run the install step for a new worktree or do i do that myself?",
    "why do terminal ids go stale?",
    "does the mcp server run locally?",
    "how do i change which agents are pinned?",
    "what does visible mean versus a background agent?",
    "is there a size cap on a copytree bundle?",
    "how does daintree stop agents voting for themselves?",
    "how long can an agent sit waiting before it times out?",
    "how does daintree know an issue is already being worked on?",
    "does an agent see a relayed message as if i typed it?",
    "does daintree link a pr back to its issue automatically?",
    "is there a cap on how many terminals can run at once?",
    "what is the number on the attention queue badge counting?",
    "does closing a terminal kill the agent or detach it?",
    "what does a supervisor watcher check for?",
    "is there a scrollback limit on terminal reads?",
    "can daintree post review findings onto a pr?",
    "how do i pin which model daintree uses?",
    "does daintree index the repo up front or does the agent just grep?",
    "is anything in project memory sent upstream?",
    "how do i change the keybinding for switching terminals?",
    "does daintree work over ssh?",
    "what is a watcher?",
    "does daintree run on linux or is it mac only?",
    "how do i update daintree?",
    "does daintree collect telemetry?",
    "can i use daintree with a local model?",
    "what is a fleet in daintree?",
    "whats a recipe and who writes them?",
    "can two people share one daintree session?",
    "does daintree store my api key or is it just in memory?",
    "whats the shortcut to jump to the terminal thats asking me something?",
    "whats a watcher in daintree",
    "whats a watcher again exactly, do they cost anything to leave running",
    "what actually is a watcher",
    "how do i change the keybinding for switching tabs",
    "where do i set the default coding agent",
    "whats the difference between a recipe and a worktree in daintree",
    "how does the attention queue decide what surfaces first",
    "does daintree run on linux or is it mac only",
    "how do watchers actually work — what wakes them up, is it polling or a hook?",
})

#: An explicit product reference — the thing that makes a question unambiguously
#: about Daintree rather than about whatever the conversation was just doing.
_NAMES_PRODUCT = re.compile(r"\b(daintree|this app|the app)\b", re.I)

LEAD_INS = [
    "quick q — ",
    "sorry, unrelated — ",
    "hang on, before you push — ",
    "one thing first, ",
    "actually wait — ",
    "side question, ",
]

#: Compound tails, chosen per question shape so the result is still a question
#: about the same thing. A tail is only appended where its pattern matches.
#: The "and when would i want one?" tail only makes sense after a question about
#: a THING ("what is a watcher"), which is why the first pattern requires the
#: indefinite article. A broader `^(what is|whats)` matched "whats pinned and
#: configured agents" and produced "…, and when would I want one?" — grammatical
#: noise. The old catch-all `^(where|does|do|can|is|are)` is gone for the same
#: reason: "and is that configurable?" is not answerable for most of what it hit.
COMPOUND = [
    (re.compile(r"^(what is|whats|what's)\s+(a|an)\s+\w+", re.I),
     " and when would i want one?"),
    (re.compile(r"^(how do i|how do you)\b", re.I), " and is there a shortcut for it?"),
    (re.compile(r"^where (do|does|is|are)\b", re.I), " and can i change it?"),
]


#: A docs row is only rephrased if it READS as a question. Several of the 207 are
#: reported symptoms ("the mcp says disconnected, what now"), and suffixing those
#: produced "…what now in this app" — noise that teaches the phrase in a register
#: no user writes it in.
_QUESTION = re.compile(
    r"^(what|how|why|where|when|which|who|does|do|can|is|are|will|should|could|"
    r"whats|what's|hows|how's|any|if)\b",
    re.I,
)


def _deixis(q: str) -> str | None:
    """Rewrite a product-name reference as a deictic one, or add one."""
    if re.search(r"\bdaintree\b", q, re.I):
        return re.sub(r"\bdaintree\b", "this app", q, count=1, flags=re.I)
    if not _QUESTION.match(q.strip()):
        return None
    if q.rstrip().endswith("?"):
        return q.rstrip()[:-1].rstrip() + " in this app?"
    return q.rstrip() + " in this app"


def _second_person(q: str) -> str | None:
    """A user asking how the PRODUCT does something, not how they do it.

    `can i …` is deliberately NOT converted. "can you pin an agent" is a request
    to pin one at least as often as it is a question about whether pinning
    exists, and a docs positive that reads as an imperative teaches the head to
    raise the docs flag on ordinary work. The two conversions kept are
    unambiguous: "how/where do you …" asks about a mechanism, never for an
    action.
    """
    out = re.sub(r"^how do i\b", "how do you", q, count=1, flags=re.I)
    out = re.sub(r"^where do i\b", "where do you", out, count=1, flags=re.I)
    out = re.sub(r"^how does daintree\b", "how do you", out, count=1, flags=re.I)
    return out if out != q else None


def _compound(q: str) -> str | None:
    body = q.rstrip().rstrip("?").rstrip()
    if not _QUESTION.match(body):
        return None
    for pattern, tail in COMPOUND:
        if pattern.search(body):
            return body + "," + tail
    return None


def _write_atomic(path: Path, text: str) -> None:
    """Write via a sibling temp file and one rename.

    Both files here are OVERWRITTEN in place, and one of them is the corpus. A
    plain ``write_text`` interrupted midway leaves a truncated JSONL that still
    parses up to the cut — a corpus quietly missing its tail is worse than no
    corpus at all, because everything downstream keeps working.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def main() -> int:
    rng = random.Random(SEED)
    rows = [json.loads(line) for line in CORPUS.read_text().splitlines() if line.strip()]

    # Refuse to apply twice. A social lead-in in a row's history is this script's
    # own signature — nothing else in the corpus has ever put a greeting there,
    # which is the whole reason the augmentation exists.
    # The signature is the exact user+assistant PAIR, not the greeting alone:
    # the base corpus already contains 43 rows opening on "hey" / "hi" /
    # "morning", so matching the user line alone reports a pristine corpus as
    # already-augmented (measured: 43 false positives, 0 for the pair).
    signature = {(f"user: {u}", f"assistant: {a}") for u, a in SOCIAL}
    already = sum(
        1 for r in rows
        if len(r["history"]) >= 2 and (r["history"][0], r["history"][1]) in signature
    )
    if already:
        raise SystemExit(
            f"this corpus already carries the augmentation ({already} social "
            f"lead-in rows). Applying it again EXTENDS rather than regenerates — "
            f"assemble_data is cumulative. Restore the pre-augmentation corpus "
            f"first:\n    git show <commit>:training/data/selection.jsonl "
            f"> {CORPUS}"
        )

    # ---- 1. Relabel the contradictions, in place, in the committed corpus.
    relabelled = 0
    seen_targets: set[str] = set()
    for r in rows:
        if r["query"] in RELABEL_TO_DOCS and r["source"] == "no_task_stateful":
            seen_targets.add(r["query"])
            if not r["is_docs"]:
                r["is_docs"] = True
                relabelled += 1
    # A target can go missing for two very different reasons, and only one is a
    # problem. The benign one: `assemble_data` drops a row whose query collides
    # with a held-out benchmark string, so re-running this pipeline sees a corpus
    # one row shorter than the curation was written against ("what is a watcher?"
    # is the live example — the benchmark uses it, so the corpus may not). The
    # real one: the wording moved and the whole curation is now addressing rows
    # that no longer exist. Report the first, fail on the second, and do not use
    # an arbitrary fraction to tell them apart — nothing matching at all is the
    # only unambiguous signal.
    missing = RELABEL_TO_DOCS - seen_targets
    if not seen_targets:
        raise SystemExit(
            "no relabel target matched the corpus — the wording moved and the "
            "curation must be redone against the current selection.jsonl"
        )
    if missing:
        print(f"  note: {len(missing)} relabel target(s) absent (held out by the "
              f"benchmark gate, or already re-gated): {sorted(missing)[:2]}")
    # The curated list is explicit and reviewable, but it is a LIST, and three
    # contradictions survived it — "how do i change the theme" and "is there a
    # config file and where does it live" (missed by the curation) and "does
    # daintree index the repo up front" (a `topic_shift` row, whose branch
    # hardcodes the flag just as `no_task_stateful` does, so a source-specific
    # gate could never have reached it). Curation cannot be the only mechanism
    # when three assembler branches can mint a False.
    #
    # So the docs BUNDLE is treated as the authority on docs-ness: any row whose
    # normalised query matches a `source == "docs"` positive is a docs question
    # wherever else it appears. Then the invariant is asserted rather than
    # trusted, because the whole bug was one label contradicting another.
    # Authority is every row now labelled True — the docs bundle's own positives
    # AND the curated relabels above. Sourcing it from the docs bundle alone left
    # one contradiction standing that no amount of curation could close: "does
    # daintree index the repo up front or does the agent just grep" appears as a
    # `no_task_stateful` row (curated to True here) and as a `topic_shift` row
    # (hardcoded False by its branch), and is in the docs bundle under neither.
    authoritative = {_norm(r["query"]) for r in rows if r["is_docs"]}
    propagated = 0
    for r in rows:
        if r["is_docs"]:
            continue
        if _norm(r["query"]) in authoritative:
            r["is_docs"] = True
            propagated += 1

    conflicts = defaultdict(set)
    for r in rows:
        conflicts[_norm(r["query"])].add(bool(r["is_docs"]))
    still = sorted(q for q, labels in conflicts.items() if len(labels) > 1)
    if still:
        raise SystemExit(
            f"{len(still)} query/queries still carry BOTH docs labels — the "
            f"contradiction this script exists to remove: {still[:3]}"
        )

    _write_atomic(CORPUS, "".join(json.dumps(r) + "\n" for r in rows))
    print(f"relabelled {relabelled} curated + {propagated} propagated to is_docs=True "
          f"({len(seen_targets)}/{len(RELABEL_TO_DOCS)} curated targets matched)")

    # ---- source material, all of it already in the corpus.
    #
    # These pools must name the ORIGINAL rows even when this runs against a
    # corpus this script has already extended — which is the normal case, because
    # the committed `selection.jsonl` IS the augmented one. Sampling naively made
    # the "byte for byte" promise above false: a second run saw 625 docs
    # positives instead of 207 and produced a different journal. So the
    # augmentation's own output is subtracted first. Context-bearing generated
    # rows are excluded by having history or an active set (no original docs row
    # had either); the bare phrasing variants are excluded by rebuilding every
    # string the transforms can emit and removing exact matches.
    docs_all = [r for r in rows if r["source"] == "docs" and r["is_docs"]]
    _emitted = {
        out
        for r in docs_all
        for out in (_deixis(r["query"]), _second_person(r["query"]), _compound(r["query"]))
        if out
    } | {lead + r["query"] for r in docs_all for lead in LEAD_INS}
    docs_pos = [
        r for r in docs_all
        if not r["history"] and not r["active_ids"] and r["query"] not in _emitted
    ]
    docs_neg = [
        r for r in rows
        if r["source"] == "docs" and not r["is_docs"] and not r["history"]
        and not any(r["query"].startswith(lead) for lead in LEAD_INS)
    ]
    # The routing families below are emitted through the bundle shape whose
    # assembler branch HARDCODES ``is_docs=False``. So a docs positive must never
    # enter these pools: sampling one produced a row contradicting its own
    # source ("the watcher i set never wakes…" as True in the docs bundle and
    # False as a topic shift), which is the exact defect this script exists to
    # remove — reintroduced by the script itself.
    task_rows = [
        r for r in rows
        if r["source"] == "positive" and not r["history"] and r["expect_ids"]
        and not r["is_docs"]
    ]
    multi_rows = [
        r for r in rows
        if len(r["expect_ids"]) >= 2 and not r["history"] and not r["is_docs"]
    ]
    chatter = [
        r for r in rows
        if r["source"] in ("no_task", "off_domain") and not r["expect_ids"]
        and not r["is_docs"]
    ]
    task_hist = sorted({
        tuple(r["history"]) for r in rows
        if len(r["history"]) == 2 and r["source"] in ("topic_shift", "followup")
    })
    print(f"source: {len(docs_pos)} docs+, {len(docs_neg)} docs-, {len(task_rows)} tasks, "
          f"{len(multi_rows)} multi-runbook, {len(task_hist)} histories")

    def social() -> list[str]:
        u, a = rng.choice(SOCIAL)
        return [f"user: {u}", f"assistant: {a}"]

    docs_items: list[dict] = []
    shift_items: list[dict] = []

    # ---- 2. The observed bug, exactly: a usage question after a greeting.
    #
    # `runbook_ids` is the SOURCE row's answer, never a hardcoded []. 21 of the 207
    # docs positives legitimately require runbooks — a docs turn and an operational
    # turn are not exclusive, which is the whole premise of the pinned docs
    # runbook arriving ALONGSIDE another one. An earlier draft wrote [] for all
    # of them and generated 25 rows training "answer with nothing" for requests
    # whose own source row demands diagnostics or watch-outcome.
    for r in rng.sample(docs_pos, min(70, len(docs_pos))):
        docs_items.append({"query": r["query"], "is_docs": True,
                           "runbook_ids": list(r["expect_ids"]),
                           "history": social(), "active_ids": []})

    # ---- 3. Docs positives carrying a REAL prior exchange.
    #
    # Only questions that NAME the product get a task history grafted on. A
    # borrowed exchange is not label-preserving for a question whose referent is
    # ambiguous: "where are the logs kept" after a walk through five monorepo
    # services is a question about those services, not about Daintree, and
    # labelling it a docs turn teaches the head to answer the wrong one. Social
    # lead-ins carry no referent at all, which is why family 2 above can use the
    # whole pool and this one cannot.
    #
    # NO ACTIVE-SET FAMILY. It was built (60 rows, an active set crossed with an
    # unrelated history) and removed on two independent grounds. Serving takes
    # the docs decision from a second pass with the active set EMPTIED, so those
    # rows could never reach the path they were meant to teach; and carrying an
    # active set with an empty answer trains eviction, because an authoritative
    # empty result CLEARS the active set (runbooks/active_set.py). The corpus
    # already holds 318 such rows under `no_task_stateful`; this round declines
    # to add 60 more to a convention it has not measured.
    grounded = [r for r in docs_pos if _NAMES_PRODUCT.search(r["query"])]
    for r in rng.sample(grounded, min(80, len(grounded))):
        docs_items.append({"query": r["query"], "is_docs": True,
                           "runbook_ids": list(r["expect_ids"]),
                           "history": list(rng.choice(task_hist)), "active_ids": []})

    # ---- 4. Phrasing. Each transform is applied cold AND with context, because
    #        the two failures compose — "in this app" after a greeting is the
    #        turn that was actually reported.
    for transform in (_deixis, _second_person, _compound):
        # Second person converts only ~36 of the 207 (the "how/where do i …"
        # openers), so it runs over the WHOLE set rather than a sample of it —
        # sampling 45 rows for a transform that fires on 17% of them yielded 8
        # rows, which is not a phrasing the head can learn.
        pool = docs_pos if transform is _second_person else rng.sample(
            docs_pos, min(45, len(docs_pos)))
        for r in pool:
            q = transform(r["query"])
            if not q:
                continue
            grounded_q = bool(_NAMES_PRODUCT.search(q))
            hist = social() if rng.random() < 0.4 else (
                list(rng.choice(task_hist))
                if grounded_q and rng.random() < 0.5 else [])
            docs_items.append({"query": q, "is_docs": True,
                               "runbook_ids": list(r["expect_ids"]),
                               "history": hist, "active_ids": []})
    for r in rng.sample(docs_pos, min(30, len(docs_pos))):
        q = _deixis(r["query"])
        second = _second_person(q or r["query"])
        if second:
            docs_items.append({"query": second, "is_docs": True,
                               "runbook_ids": list(r["expect_ids"]),
                               "history": [], "active_ids": []})
    for r in rng.sample(docs_pos, min(35, len(docs_pos))):
        docs_items.append({"query": rng.choice(LEAD_INS) + r["query"], "is_docs": True,
                           "runbook_ids": list(r["expect_ids"]),
                           "history": [], "active_ids": []})

    # ---- 5. Balance. Context must not BECOME the docs cue: operational requests
    #        get the same lead-ins and the same histories, and stay not-docs.
    for r in rng.sample(docs_neg, min(80, len(docs_neg))):
        docs_items.append({"query": r["query"], "is_docs": False,
                           "runbook_ids": list(r["expect_ids"]),
                           "history": social(), "active_ids": []})
    for r in rng.sample(docs_neg, min(60, len(docs_neg))):
        docs_items.append({"query": rng.choice(LEAD_INS) + r["query"], "is_docs": False,
                           "runbook_ids": list(r["expect_ids"]),
                           "history": list(rng.choice(task_hist)), "active_ids": []})

    # ---- 6. The routing half: a real instruction after a greeting still routes.
    #
    # The COUNT here is load-bearing, and getting it wrong is measurable. A first
    # pass added 320 of these against ~570 zero-cardinality rows (the docs
    # families above, which genuinely load no runbook), which moved the corpus
    # from 13.8% zero-answer to 17.5% and thinned the two-runbook class from 3.3%
    # to 3.0%. The cardinality head followed the prior it was given: docs recall
    # rose 14/19 -> 20/21 and multi-runbook fell 4/5 -> 2/5 in the same export.
    # An augmentation is not free of the balance it lands in, so this family and
    # the one below are sized to hold the shape of the corpus they extend.
    for r in rng.sample(task_rows, min(600, len(task_rows))):
        shift_items.append({"query": r["query"], "expect_ids": list(r["expect_ids"]),
                            "history": social(), "active_ids": []})
    # Two-runbook answers are only 3.3% of the corpus and NONE of them carried a
    # social lead-in, so without this the pleasantry lesson is taught in the
    # one-runbook class alone and the head learns "greeting implies at most one".
    for r in rng.sample(multi_rows, min(95, len(multi_rows))):
        shift_items.append({"query": r["query"], "expect_ids": list(r["expect_ids"]),
                            "history": social() if rng.random() < 0.6
                            else list(rng.choice(task_hist)),
                            "active_ids": []})
    # ...and a greeting followed by chatter still answers with nothing, so the
    # lesson is "ignore the pleasantry", not "always route after one".
    for r in rng.sample(chatter, min(70, len(chatter))):
        shift_items.append({"query": r["query"], "expect_ids": [],
                            "history": social(), "active_ids": []})

    # The journal wire shape assemble_data reads: one {"type": "result", ...}
    # envelope per agent, whose `result` is the bundle. Written by hand here
    # rather than by a Workflow agent, because this corpus is a transformation
    # of rows that already exist rather than anything a model needs to invent.
    _write_atomic(
        JOURNAL,
        "".join(
            json.dumps({"type": "result", "result": {"items": items}}) + "\n"
            for items in (docs_items, shift_items)
        ),
    )
    print(f"journal: {len(docs_items)} docs items, {len(shift_items)} routing items "
          f"-> {JOURNAL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
