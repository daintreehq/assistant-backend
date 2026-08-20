"""The shipped skill selector: retrieve a shortlist, then judge it as a set.

## The architecture

    conversation ──► retriever encoder ──► normalised query vector
                                                │
                                    cosine vs the skill table
                                                │
                                        top-k candidate ids
                                                │
    conversation ──► ranking encoder ──► q ─────┤
    skill table  ─────────────────────► s_i ────┤
                                                ▼
                                        set-wise head
                                                │
                    ┌───────────────────────────┼──────────────────────┐
                k skill logits            1 docs logit         4 cardinality logits

Two encoder passes over the conversation, a matrix multiply, and two transformer
layers over k+1 vectors of width 256. The skills themselves are never re-read at
request time — their vectors are computed once, when the catalog is loaded, and
looked up thereafter.

A turn whose docs question differs from its ranking query runs that whole path a
SECOND time, on the bare question, purely to re-read the docs logit — see the
long note at the docs decision in ``select`` for why the head cannot be trusted
to read it off a context-laden query. That doubles the request-time encoder work
on those turns (~+7 ms) and is skipped entirely when the two strings agree.

## Why not the single fixed head this replaced

The previous selector was one encoder with a 38-row output layer: one weight row
per skill, learned end to end. It measured well (158/169) and it does not scale,
for two reasons that are both about the head owning the catalog:

1. **A skill it never trained on cannot be named at all.** There is no output for
   it. That forced a ``routing_revision`` provenance gate whose only possible
   response to a changed catalog was to degrade every turn open until someone
   retrained. Skill learning writing a new runbook could not route to it.
2. **Every skill needs its own training mass.** At 38 that is a weekend of
   synthetic generation. At 1,000 it is the whole project.

Here the head has no per-skill parameters. It consumes a skill's *embedding*,
produced by the same encoder that reads the conversation, so a skill added after
the build is embedded on load and can be selected that turn — no retrain, no
staleness gate, no degraded window. And because it only ever sees ``k``
candidates, its job is the same size at 38 skills and at 1,000; only the cosine
search grows, and that is a matmul.

Be precise about how strong that first property is, because it is easy to
overstate. Measured by holding skills out of both stages (EXPERIMENTS.md), a
never-trained skill wins **4 of 14** benchmark cases against **13 of 14** for the
same skill trained: it takes requests only it plausibly answers and loses
contested ones to neighbours whose training data names them and not it. What
changed is the failure mode, and the change is large — under the fixed head a new
skill was unselectable AND the provenance gate degraded every OTHER turn open
until a human retrained. A retrain is no longer what makes a skill possible; it
is what makes it competitive.

## Why brute-force cosine search and not an ANN index

At 1,000 skills the table is 1,000 x 384 floats. One normalised matrix-vector
product is ~400k FLOPs — microseconds, and exact. An HNSW or IVF index would add
a dependency, a build step and approximate recall in exchange for nothing
measurable; the crossover where that trade starts paying is around 10^5 vectors,
two orders of magnitude beyond the ceiling this catalog is planned against.

## Why the shortlist's ORDER does not matter

The head has no positional encoding, so it is permutation-equivariant: a
candidate's logit depends on its embedding and its features, never on which slot
it occupies. Retrieval evidence is not discarded — the cosine score and the
normalised rank are per-candidate FEATURES the head weighs like any other
signal. This was measured against the alternative (candidates in cosine order
with a learned rank embedding); see ``training/EXPERIMENTS.md``. Equivariance is
what keeps a skill added after the build from being penalised for landing in a
slot the head never saw during training.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from .documents import INDEX_DOC_VARIANT, skill_document

#: Sequence cap for a conversation query. Measured p90 ~64 tokens on the
#: benchmark; 256 is generous and costs nothing, because attention tracks the
#: ACTUAL batch length rather than the cap.
MAX_TOKENS = 256

#: Per-candidate scalar features, in this order. Mirrors
#: ``training/setwise_model.py``'s ``FEATURE_NAMES`` — the two must agree or the
#: head is served a permutation of the meanings it trained on, silently.
FEATURE_NAMES = ("cosine", "active", "rank_norm")

#: Probability the docs head must clear for the turn to retrieve documentation.
#:
#: Below 0.5 on purpose: retrieval is the cheapest thing this classifier can ask
#: for and the most expensive thing for it to miss. A false positive costs one
#: best-effort HTTP call inside an existing budget and some volatile-tail bytes,
#: and the turn answers regardless. A false negative answers a product question
#: with no product documentation — which reads to a user as the assistant simply
#: not knowing how its own application works.
#:
#: 0.40 rather than 0.5 is bought for nothing, and rather than lower for a
#: reason. The head is sharply bimodal, so on the corpus the two thresholds are
#: indistinguishable — 199/207 recall and the SAME 6 false positives at each,
#: with chatter and no-task turns at 0 false positives all the way down to 0.15.
#: What lives in the gap between them is one real class: docs questions phrased
#: in the SECOND person. 66 of the corpus's 207 docs rows contain the token "I"
#: (76 counting my/me/mine) against 4 mentioning "you", and none is of the form
#: "how do you <do X>", so those land in
#: the sparse middle of the sigmoid instead of the top mode — "How do you create
#: a worktree in Daintree?" at 0.4471 and "how do you rename a terminal" at
#: 0.4783, while six other second-person phrasings clear 0.5 unaided. 0.40 is
#: the lowest floor that recovers that class without reaching for anything the
#: corpus actually labels not-docs; going further buys +1 recall and a 7th false
#: positive. Re-measure it after any retrain — like the floors on the class
#: below, it is calibrated to THIS artifact's sigmoid and does not transfer.
DOCS_THRESHOLD = 0.40


@dataclass(frozen=True)
class Selection:
    """One turn's answer."""

    skill_ids: list[str]
    docs_wanted: bool
    #: Predicted number of skills, before the cap is applied.
    cardinality: int
    #: Per-skill probability, for logging and diagnostics.
    scores: dict[str, float]


@dataclass(frozen=True)
class SkillTable:
    """Embedded skills: what the shortlist searches and the head reads.

    Immutable and swapped wholesale rather than mutated, because it is read from
    an inference worker thread while the catalog can be reloaded underneath it
    (skill learning). ``digests`` keys each row to the exact document text it was
    built from, so a reload re-embeds precisely the skills whose routing surface
    moved and reuses every other vector.
    """

    ids: tuple[str, ...]
    #: (n, retriever_dim), L2-normalised — cosine is a plain dot product.
    retrieval: np.ndarray
    #: (n, ranking_dim), mean-pooled; what the set-wise head consumes.
    ranking: np.ndarray
    #: skill id -> sha256 of the rendered document it was embedded from.
    digests: dict[str, str]
    #: Which encoder precision produced these vectors ("int8" / "fp32").
    precision: str


def document_digest(document: str) -> str:
    return hashlib.sha256(document.encode()).hexdigest()


class SetwiseClassifier:
    """Two-stage skill selection: cosine shortlist, then one set-wise judgement."""

    def __init__(self, directory: Path, *, quantized: bool = True) -> None:
        """int8 encoders, fp32 head.

        The encoders carry ~99% of the parameters and all of the memory
        pressure, and quantising them is gated on decision parity in
        ``training/export_two_stage.py``. The head is ~2.4M parameters — under
        10MB in fp32 — so quantising it would trade measurable numerical slack
        for memory nobody is short of.
        """
        meta = json.loads((directory / "meta.json").read_text())
        self._meta = meta
        self._k = int(meta["candidates"])
        self._card_classes = int(meta["cardinality_classes"])
        self._query_max = int(meta.get("max_length", MAX_TOKENS))
        self._doc_max = int(meta.get("doc_max_length", 192))
        self._doc_variant = str(meta.get("doc_variant", INDEX_DOC_VARIANT))

        #: Which precision this session runs. It is part of the table's identity,
        #: not just a file name: a vector embedded by the fp32 graph and a query
        #: embedded by the int8 graph are outputs of two different functions, and
        #: comparing them is a silent accuracy loss with no error anywhere. Seen
        #: for real — an int8-built ``vectors.npz`` reused by an fp32 session
        #: moved the benchmark by two cases between two otherwise identical
        #: exports, which read as run-to-run noise and was not.
        self._precision = "int8" if quantized else "fp32"
        graph = "model_int8.onnx" if quantized else "model.onnx"
        self._retriever = _session(directory / "retriever" / graph)
        self._ranker = _session(directory / "ranking" / graph)
        self._head = _session(directory / "head.onnx")

        self._retriever_tok = _tokenizer(directory / "retriever", self._query_max)
        self._ranker_tok = _tokenizer(directory / "ranking", self._query_max)
        # Documents get their own tokenizer instances: truncation length is
        # tokenizer state, and sharing one instance across queries and documents
        # would silently apply whichever cap was configured last.
        self._retriever_doc_tok = _tokenizer(directory / "retriever", self._doc_max)
        self._ranker_doc_tok = _tokenizer(directory / "ranking", self._doc_max)

        # An artifact is several files that must agree — a head.onnx from one
        # export beside a meta.json from another loads fine and then reads the
        # wrong slot for every decision. Checked at load, loudly, rather than
        # served.
        head_inputs = {i.name for i in self._head.get_inputs()}
        expected = {"query", "candidates", "features", "mask"}
        if head_inputs != expected:
            raise ValueError(
                f"set-wise head expects inputs {sorted(expected)}, graph has "
                f"{sorted(head_inputs)}"
            )
        cand_dim = self._head.get_inputs()[
            [i.name for i in self._head.get_inputs()].index("candidates")
        ].shape
        if isinstance(cand_dim[1], int) and cand_dim[1] != self._k:
            raise ValueError(
                f"set-wise head takes {cand_dim[1]} candidate slots, meta says {self._k}"
            )
        card_out = self._head.get_outputs()[2].shape[-1]
        if isinstance(card_out, int) and card_out != self._card_classes:
            raise ValueError(
                f"set-wise head emits {card_out} cardinality logits, "
                f"meta expects {self._card_classes}"
            )
        feature_dim = self._head.get_inputs()[
            [i.name for i in self._head.get_inputs()].index("features")
        ].shape[-1]
        if isinstance(feature_dim, int) and feature_dim != len(FEATURE_NAMES):
            raise ValueError(
                f"set-wise head takes {feature_dim} per-candidate features, "
                f"serving builds {len(FEATURE_NAMES)} ({', '.join(FEATURE_NAMES)})"
            )

        self._lock = threading.Lock()
        #: (catalog revision the table was built for, table) — held as ONE tuple
        #: so a reader gets a consistent pair. Two attributes would let a reader
        #: land between the two writes and pair a new revision with the old
        #: table, which is exactly the stale answer the digest check exists to
        #: prevent. The revision is a fast path only; the digest comparison in
        #: ``sync`` is the correctness check. It skips re-rendering and
        #: re-hashing every skill document on a turn where the catalog has not
        #: changed at all: microseconds at 38 skills, milliseconds at 1,000, and
        #: this sits on the first-byte path.
        self._cached: tuple[str | None, SkillTable] | None = None
        preload = directory / "vectors.npz"
        if preload.exists():
            table = _load_table(preload)
            # Only adopt the shipped table if it was built by THIS precision's
            # encoders (see ``_precision``). A mismatch simply re-embeds — a
            # second or so at startup — rather than serving mixed vectors.
            if table is not None and table.precision == self._precision:
                # No revision: the shipped table was built against the catalog at
                # BUILD time, and the live catalog may have moved since. Pairing
                # it with None forces the first sync through the digest check.
                self._cached = (None, table)

    @property
    def candidates(self) -> int:
        """How many skills reach the head. Shortlist width, not a catalog size."""
        return self._k

    @property
    def table(self) -> SkillTable | None:
        cached = self._cached
        return cached[1] if cached else None

    # ------------------------------------------------------------------ sync

    def sync(self, catalog: Any) -> SkillTable:
        """Bring the skill table in line with the live catalog.

        The cheap path — every selectable skill already embedded under the same
        document digest — does a dict comparison and returns the existing table.
        Anything else (a new skill from skill learning, an edited whenToUse, a
        deleted runbook) re-embeds only what moved.

        This is the mechanism that removes the old architecture's staleness
        gate: the catalog can change at runtime and the selector answers for the
        new catalog on the next turn, rather than degrading open until a human
        retrains a fixed head.
        """
        revision = getattr(catalog, "revision", None)
        cached = self._cached
        if cached is not None and revision is not None and cached[0] == revision:
            return cached[1]

        ids = tuple(sorted(catalog.selectable_ids))
        wanted = {
            sid: skill_document(catalog.get(sid).meta, variant=self._doc_variant)
            for sid in ids
        }
        digests = {sid: document_digest(doc) for sid, doc in wanted.items()}

        with self._lock:
            cached = self._cached
            current = cached[1] if cached else None
            if current is not None and current.digests == digests:
                # Same routing surface, different catalog revision — a body-only
                # edit, which is the common skill-learning case. Nothing to
                # re-embed; just remember the new revision so the next turn takes
                # the fast path.
                self._cached = (revision, current)
                return current

            keep = current.digests if current is not None else {}
            reuse = {
                sid: i
                for i, sid in enumerate(current.ids if current else ())
                if keep.get(sid) == digests.get(sid)
            }
            stale = [sid for sid in ids if sid not in reuse]

            retrieval = np.zeros(
                (len(ids), self._retrieval_dim(current)), dtype=np.float32
            )
            ranking = np.zeros((len(ids), self._ranking_dim(current)), dtype=np.float32)
            for i, sid in enumerate(ids):
                if sid in reuse and current is not None:
                    retrieval[i] = current.retrieval[reuse[sid]]
                    ranking[i] = current.ranking[reuse[sid]]
            if stale:
                fresh_docs = [wanted[sid] for sid in stale]
                r_vecs = _run_encoder(
                    self._retriever, self._retriever_doc_tok, fresh_docs, normalize=True
                )
                c_vecs = _run_encoder(
                    self._ranker, self._ranker_doc_tok, fresh_docs, normalize=False
                )
                positions = {sid: i for i, sid in enumerate(ids)}
                for sid, r_vec, c_vec in zip(stale, r_vecs, c_vecs, strict=True):
                    retrieval[positions[sid]] = r_vec
                    ranking[positions[sid]] = c_vec

            table = SkillTable(
                ids=ids,
                retrieval=retrieval,
                ranking=ranking,
                digests=digests,
                precision=self._precision,
            )
            self._cached = (revision, table)
            return table

    def _retrieval_dim(self, current: SkillTable | None) -> int:
        if current is not None and current.retrieval.size:
            return int(current.retrieval.shape[1])
        # From meta, not from the graph's declared output shape: an ONNX
        # dimension can come back as a symbolic NAME rather than an int, and
        # int() on it raises at load time for no reason.
        return int(self._meta["retriever_hidden_size"])

    def _ranking_dim(self, current: SkillTable | None) -> int:
        if current is not None and current.ranking.size:
            return int(current.ranking.shape[1])
        return int(self._meta["hidden_size"])

    # ---------------------------------------------------------------- select

    def _encode_pass(
        self, table: SkillTable, text: str, active: set[str]
    ) -> tuple[np.ndarray, int, dict[str, np.ndarray]]:
        """Build one complete, self-consistent set of head inputs for ``text``.

        Extracted so the docs pass CANNOT be built half from another query's
        evidence. Every tensor here is derived from this one string: the
        shortlist, each candidate's cosine to it, each candidate's rank within
        it. Mixing those across two different queries hands the head a
        combination it never saw in training — the trainer always rendered the
        shortlist and the ranker query from the same text — so the seam is closed
        by construction rather than by a comment asking the next reader to be
        careful.
        """
        # normalize=True is redundant — the retriever graph L2-normalises
        # internally — and kept because it is idempotent and makes the cosine
        # contract local: `sims` below is a plain dot product only if BOTH sides
        # are unit vectors, and that should not depend on reading the exporter.
        q_ret = _run_encoder(self._retriever, self._retriever_tok, [text], normalize=True)[0]
        sims = table.retrieval @ q_ret
        n = min(self._k, len(table.ids))
        # argpartition, not a full sort: O(n) rather than O(n log n), which is
        # the difference that matters at the catalog sizes this design is for.
        top = np.argpartition(-sims, n - 1)[:n] if n < len(sims) else np.arange(len(sims))
        top = top[np.argsort(-sims[top])]

        q_rank = _run_encoder(self._ranker, self._ranker_tok, [text], normalize=False)[0]
        # Exactly ``self._k`` slots, always. The head's graph is static (see
        # export_two_stage: MultiheadAttention bakes the sequence length into its
        # reshapes), so a catalog smaller than k is served by padding with zeros
        # and mask 0 rather than by a shorter tensor.
        k = self._k
        cand = np.zeros((1, k, table.ranking.shape[1]), dtype=np.float32)
        cand[0, :n] = table.ranking[top]
        feats = np.zeros((1, k, len(FEATURE_NAMES)), dtype=np.float32)
        feats[0, :n, 0] = sims[top]
        feats[0, :n, 1] = [1.0 if table.ids[i] in active else 0.0 for i in top]
        feats[0, :n, 2] = np.arange(n, dtype=np.float32) / max(k - 1, 1)
        mask = np.zeros((1, k), dtype=np.float32)
        mask[0, :n] = 1.0
        return top, n, {
            "query": q_rank[None, :].astype(np.float32),
            "candidates": cand,
            "features": feats,
            "mask": mask,
        }

    def select(
        self,
        query: str,
        active_ids: Sequence[str] = (),
        *,
        catalog: Any = None,
        max_active: int = 3,
        docs_query: str | None = None,
        docs_threshold: float = DOCS_THRESHOLD,
    ) -> Selection:
        table = self.sync(catalog) if catalog is not None else self.table
        if table is None or not table.ids:
            return Selection(skill_ids=[], docs_wanted=False, cardinality=0, scores={})

        # The conversation's STATE rides in the query text as well as in the
        # per-candidate `active` feature. Both, deliberately: the prefix is what
        # the ranking encoder was measured to need to separate continuation from
        # topic shift, and the feature is what lets the head weigh "this exact
        # candidate is the one already open" per slot.
        from .documents import ACTIVE_PREFIX

        text = ACTIVE_PREFIX.format(", ".join(active_ids)) + query if active_ids else query

        # normalize=True is redundant — the retriever graph L2-normalises
        # internally — and kept because it is idempotent and makes the cosine
        # contract local: `sims` below is a plain dot product only if BOTH sides
        # are unit vectors, and that should not depend on reading the exporter.
        active = set(active_ids)
        top, n, feed = self._encode_pass(table, text, active)
        skill_logits, docs_logit, card_logits = self._head.run(None, feed)
        # Padded slots are already driven to -1e4 inside the graph, but they are
        # dropped here as well rather than trusted: a ranking is about to decide
        # what the turn loads, and a padding slot has no skill id to name.
        probs = 1.0 / (1.0 + np.exp(-skill_logits[0][:n]))
        count = int(np.argmax(card_logits[0]))
        order = np.argsort(-probs)
        ranked = [(table.ids[int(top[int(j)])], float(probs[int(j)])) for j in order]
        picked = admit(ranked, active, count, max_active)

        # THE DOCS DECISION IS TAKEN ON A DIFFERENT STRING FROM THE SKILL ONE.
        #
        # The skill query is deliberately context-laden: an ACTIVE_PREFIX naming
        # the loaded set, plus a 320-char tail of prior turns. Both are what let
        # the head separate a continuation from a topic shift. Neither belongs in
        # the docs decision, and shipping them there was a silent, near-total
        # outage of documentation lookup:
        #
        #   "what is a watcher?"                     docs_p = 0.9998
        #   ...same question, one skill loaded       docs_p = 0.0020
        #   ...same question, one turn of history    docs_p = 0.00017
        #
        # The cause is in the corpus, not the architecture. Of 8,640 training
        # rows, all 207 docs positives carry an empty active set AND no history,
        # while all 1,956 rows that carry an active set are labelled not-docs.
        # The separation is perfect and never once contradicted, so the head got
        # a lossless shortcut — "anything in the context means this is not a docs
        # turn" — and took it. Measured over 120 docs positives with a skill
        # loaded, that is 5/120 recall; on the bare question it is 113/120.
        #
        # So the docs logit is re-read from a SECOND COMPLETE PASS over the bare
        # user question — its own retrieval, its own shortlist, its own cosine
        # and rank features, and no active skills. A cheaper version of this
        # reused the primary pass's candidates and features and swapped only the
        # query vector; that is rejected deliberately, because it hands the head
        # a combination that cannot occur in training (a bare docs question
        # scored against another query's shortlist, carrying that query's
        # cosines and an `active` bit no docs positive ever had). The measurement
        # that made reuse look free — equal recall either way — could not have
        # detected the problem: k is 32 against 38 skills, so any two shortlists
        # must share at least 26 candidates and retrieval is nearly a no-op. At
        # 64 skills two top-32 sets can be disjoint, and the seam would open
        # silently on exactly the topic-shift turns this feature is for.
        #
        # `active` is empty on purpose rather than passed through: every docs
        # positive in the corpus has an empty active set, so a set bit is as
        # out-of-distribution for this head as the prefix was.
        #
        # Cost is one retriever encode plus one ranker encode of one short
        # string plus one head run, and it is skipped entirely when the two
        # strings already agree — the cold opening turn, where the bug never bit.
        #
        # This is a workaround pinned to a data artifact, not a principle. The
        # durable fix is docs rows carrying history and active sets; until the
        # corpus has them, the head is only in-distribution on the bare question.
        # Truthiness, not `is not None`: an empty docs query is not a question to
        # classify, and running the pass on "" would ask the head to rate the
        # embedding of nothing. Same outcome as omitting it — keep the primary
        # logit — which is what a caller passing "" means.
        if docs_query and docs_query != text:
            _, _, docs_feed = self._encode_pass(table, docs_query, set())
            _, docs_logit, _ = self._head.run(None, docs_feed)
        docs_p = float(1.0 / (1.0 + np.exp(-float(docs_logit[0]))))
        return Selection(
            skill_ids=picked,
            docs_wanted=docs_p >= docs_threshold,
            cardinality=count,
            scores=dict(ranked[:5]),
        )

    #: Probability a skill must clear to displace or join an already-loaded set.
    #:
    #: The costs here are asymmetric, and the rule encodes that rather than
    #: treating every pick alike. A false positive is cheap-ish — a loaded
    #: runbook the turn didn't need costs its prompt bytes and a little model
    #: attention. But ADDING a skill mid-conversation is the more expensive
    #: shape of that mistake: it re-encodes the loaded-skills block and
    #: everything after it, and a marginal runbook stacked onto an in-progress
    #: workflow dilutes the runbooks actually driving it. So a NEWCOMER — a
    #: skill the model wants that is not currently loaded — must earn the slot
    #: when skills are already active: fresh turns admit at the model's word,
    #: busy turns demand real confidence.
    NEWCOMER_FLOOR_WHEN_ACTIVE = 0.65

    #: The recall bonus: the same asymmetry from the other side. A turn that
    #: MISSES its skill answers without its runbook, the worst outcome
    #: available, while carrying one marginal runbook costs prompt bytes. So
    #: beyond the cardinality head's answer, any further skill at or above this
    #: floor rides along up to the cap. Applied only when the model picked
    #: SOMETHING — an empty answer ("thanks!", off-domain chatter) stays empty,
    #: because polluting a no-task turn is how loading noise becomes the norm.
    #:
    #: 0.45, and the number is NOT transferable — it is calibrated to this
    #: artifact's sigmoid. The head that preceded this one shipped 0.55, and the
    #: band genuinely moved: this head is trained with a slot-level BCE over k
    #: candidates rather than a 38-way per-label one, which compresses its
    #: probabilities downward. Re-run ``training/calibrate_floors.py`` after
    #: every export rather than carrying the constant across.
    #:
    #: Chosen at the KNEE, not the peak. Measured on the shipped int8 artifact,
    #: recall-pass is flat at 158/169 for every floor from 0.20 to 0.45 and only
    #: the extras move (55 at 0.20, 41 at 0.45); 0.55 gives up a case for two
    #: fewer extras. So 0.45 is the lowest floor that is still buying something,
    #: and everything below it is precision spent for no recall. Picking the
    #: benchmark's peak instead would be fitting a constant to a suite that has
    #: been inspected many times (see the caveat at the top of this file's
    #: companion, training/EXPERIMENTS.md).
    BONUS_FLOOR = 0.45


def admit(
    ranked: list[tuple[str, float]],
    active: set[str],
    count: int,
    max_active: int,
) -> list[str]:
    """The admission policy: which of the ranked skills the turn actually gets.

    Pure so it is testable without the ONNX graphs — every rule below has a unit
    test driving it with hand-built rankings (``tests/unit``).

    ``ranked`` is (skill id, probability) in DESCENDING probability order;
    ``count`` is the cardinality head's answer; ``active`` is the prior set as
    the selector saw it (selectable ids only).
    """
    picked: list[str] = []
    want = min(max(count, 0), max_active)
    # Scan the WHOLE ranking until `want` candidates are accepted, not just the
    # first `count` rows. Bounding the scan to the count made a rejected
    # newcomer CONSUME its slot: cardinality says one, rank 1 is a filtered
    # newcomer, rank 2 (the active skill) was never examined — and the turn came
    # back authoritatively empty, evicting a live workflow.
    for sid, prob in ranked:
        if len(picked) >= want:
            break
        # The earn-your-place rule: with a workflow already loaded, a skill that
        # is NOT part of it needs NEWCOMER_FLOOR_WHEN_ACTIVE, while a skill the
        # conversation is already running rides on the model's ranking alone. On
        # a fresh turn (nothing active) everything admits on ranking alone —
        # early in a conversation the cheap mistake is to include, not withhold.
        newcomer = bool(active) and sid not in active
        if newcomer and prob < SetwiseClassifier.NEWCOMER_FLOOR_WHEN_ACTIVE:
            continue
        picked.append(sid)

    # The recall bonus (see BONUS_FLOOR): with a non-empty answer in hand, admit
    # any further skill scoring at or above the floor, up to the cap. Newcomers
    # still owe their higher floor when a workflow is active. The ranking is
    # descending, so the first below-floor score ends the scan — deliberately for
    # EVERY candidate, active ones included: the bonus exists to rescue near-miss
    # rankings, not to re-admit a skill the model scored below a coin flip just
    # because it used to be loaded.
    if picked:
        for sid, prob in ranked:
            if len(picked) >= max_active:
                break
            if sid in picked:
                continue
            if prob < SetwiseClassifier.BONUS_FLOOR:
                break
            if (
                active
                and sid not in active
                and prob < SetwiseClassifier.NEWCOMER_FLOOR_WHEN_ACTIVE
            ):
                continue
            picked.append(sid)
    return picked


# ------------------------------------------------------------------ plumbing


def _session(path: Path) -> ort.InferenceSession:
    opts = ort.SessionOptions()
    # One intra-op thread: the serving shape is many small concurrent requests
    # on a 1-vCPU container, so parallelism belongs at the request level, not
    # inside a few-millisecond forward pass.
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    return ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])


def _tokenizer(directory: Path, max_tokens: int) -> Tokenizer:
    tok = Tokenizer.from_file(str(directory / "tokenizer.json"))
    tok.enable_truncation(max_length=max_tokens)
    pad = tok.token_to_id("[PAD]")
    tok.enable_padding(pad_id=int(pad or 0), pad_token="[PAD]")
    return tok


def _run_encoder(
    session: ort.InferenceSession,
    tokenizer: Tokenizer,
    texts: list[str],
    *,
    normalize: bool,
) -> np.ndarray:
    encoded = tokenizer.encode_batch(texts)
    ids = np.array([e.ids for e in encoded], dtype=np.int64)
    mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
    out: np.ndarray = session.run(
        None, {"input_ids": ids, "attention_mask": mask}
    )[0]
    if normalize:
        out = out / np.linalg.norm(out, axis=-1, keepdims=True).clip(min=1e-9)
    return out.astype(np.float32)


def _load_table(path: Path) -> SkillTable | None:
    """Read a shipped table, or ``None`` if it predates the precision stamp."""
    data = np.load(path, allow_pickle=False)
    if "precision" not in data:
        return None
    ids = tuple(str(x) for x in data["ids"])
    digests = {sid: str(d) for sid, d in zip(ids, data["digests"], strict=True)}
    return SkillTable(
        ids=ids,
        retrieval=data["retrieval"].astype(np.float32),
        ranking=data["ranking"].astype(np.float32),
        digests=digests,
        precision=str(data["precision"]),
    )


def save_table(path: Path, table: SkillTable) -> None:
    np.savez(
        path,
        ids=np.array(table.ids),
        digests=np.array([table.digests[sid] for sid in table.ids]),
        retrieval=table.retrieval,
        ranking=table.ranking,
        precision=np.array(table.precision),
    )
