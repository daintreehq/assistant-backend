"""The shipped runbook selector: retrieve a shortlist, then judge it as a set.

## The architecture

    conversation ──► retriever encoder ──► normalised query vector
                                                │
                                    cosine vs the runbook table
                                                │
                                        top-k candidate ids
                                                │
    conversation ──► ranking encoder ──► q ─────┤
    runbook table  ─────────────────────► s_i ────┤
                                                ▼
                                        set-wise head
                                                │
                    ┌───────────────────────────┼──────────────────────┐
                k runbook logits            1 docs logit         4 cardinality logits

Two encoder passes over the conversation, a matrix multiply, and two transformer
layers over k+1 vectors of width 256. The runbooks themselves are never re-read at
request time — their vectors are computed once, when the catalog is loaded, and
looked up thereafter.

A turn whose docs question differs from its ranking query runs that whole path a
SECOND time, on the bare question, purely to re-read the docs logit — see the
long note at the docs decision in ``select`` for why the head cannot be trusted
to read it off a context-laden query. That doubles the request-time encoder work
on those turns (~+7 ms) and is skipped entirely when the two strings agree.

## Why not the single fixed head this replaced

The previous selector was one encoder with a 38-row output layer: one weight row
per runbook, learned end to end. It measured well (158/169) and it does not scale,
for two reasons that are both about the head owning the catalog:

1. **A runbook it never trained on cannot be named at all.** There is no output for
   it. That forced a ``routing_revision`` provenance gate whose only possible
   response to a changed catalog was to degrade every turn open until someone
   retrained. Runbook learning writing a new runbook could not route to it.
2. **Every runbook needs its own training mass.** At 38 that is a weekend of
   synthetic generation. At 1,000 it is the whole project.

Here the head has no per-runbook parameters. It consumes a runbook's *embedding*,
produced by the same encoder that reads the conversation, so a runbook added after
the build is embedded on load and can be selected that turn — no retrain, no
staleness gate, no degraded window. And because it only ever sees ``k``
candidates, its job is the same size at 38 runbooks and at 1,000; only the cosine
search grows, and that is a matmul.

Be precise about how strong that first property is, because it is easy to
overstate. Measured by holding runbooks out of both stages (EXPERIMENTS.md), a
never-trained runbook wins **4 of 14** benchmark cases against **13 of 14** for the
same runbook trained: it takes requests only it plausibly answers and loses
contested ones to neighbours whose training data names them and not it. What
changed is the failure mode, and the change is large — under the fixed head a new
runbook was unselectable AND the provenance gate degraded every OTHER turn open
until a human retrained. A retrain is no longer what makes a runbook possible; it
is what makes it competitive.

## Why brute-force cosine search and not an ANN index

At 1,000 runbooks the table is 1,000 x 384 floats. One normalised matrix-vector
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
what keeps a runbook added after the build from being penalised for landing in a
slot the head never saw during training.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from .candidates import CandidateSet, assemble_candidates
from .documents import INDEX_DOC_VARIANT, runbook_document

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
#: 0.40 rather than 0.5 is bought for nothing: the head is sharply bimodal, so on
#: the corpus the two are indistinguishable — 199/207 recall and the SAME 6 false
#: positives at each, with chatter and no-task turns at 0 false positives all the
#: way down to 0.15. NOTE those figures were measured against the 207-positive
#: corpus and have NOT been re-measured since the context augmentation took it to
#: 499; the sweep is owed (training/EXPERIMENTS.md, "Still open"). It is a cheap
#: standing bias toward retrieving, which is the posture this decision wants, and
#: it costs nothing measurable to hold.
#:
#: DO NOT read it as fixing the class it was first chosen for. Second-person
#: phrasings ("how do you create a worktree", "how do you rename a terminal")
#: landed in the sparse middle of the sigmoid rather than the top mode, because
#: 66 of the corpus's then-207 docs rows contained the token "I" (76 counting
#: my/me/mine) against 4 mentioning "you", and NONE was of the form "how do you
#: <do X>". The corpus now carries 39 of that shape, so this paragraph describes
#: why the floor was chosen, not the corpus it now runs on. They
#: were measured at 0.4471 and 0.4783 — above this floor — and that looked like a
#: fix until the same two queries turned out to DISAGREE BETWEEN fp32 AND int8 on
#: one machine, and between arm64 and x86-64 on the same artifact. A case decided
#: by quantisation noise is not recovered by a threshold, wherever the threshold
#: is put; it is recovered by giving the head training data in that shape. The
#: corpus work is in EXPERIMENTS.md.
#:
#: The general lesson, since this is the second constant in this file to meet it:
#: anything tuned to within a few hundredths of a borderline case is tuned to the
#: kernel, not the model. Re-measure after any retrain — like the floors on the
#: class below, this is calibrated to THIS artifact's sigmoid and does not
#: transfer — and prefer justifying a value by what it costs, which is stable,
#: over what single case it rescues, which may not be.
DOCS_THRESHOLD = 0.40


@dataclass(frozen=True)
class Selection:
    """One turn's answer."""

    runbook_ids: list[str]
    docs_wanted: bool
    #: Predicted number of runbooks, before the cap is applied.
    cardinality: int
    #: Per-runbook probability, for logging and diagnostics.
    scores: dict[str, float]
    #: Active runbooks the cosine shortlist missed and ``candidates.py`` seated
    #: anyway. Diagnostics only — nothing branches on it — but it is the one
    #: number that says how close the catalog is to the scale where the old
    #: top-k-only ballot would have started evicting live workflows.
    reserved_ids: tuple[str, ...] = ()
    #: The WHOLE ballot, (runbook id, probability), descending. ``scores`` is the
    #: top five of this, kept because several callers want a short log line.
    #:
    #: Carried in full because one end-to-end miss rate cannot say which stage
    #: lost the runbook, and the four causes have nothing to do with each other: it
    #: was never a candidate (stage 1), the head ranked it below the cut
    #: (stage 2), an admission floor rejected it, or the cardinality head asked
    #: for fewer runbooks than the turn needed. Only the ballot distinguishes
    #: "absent" from "scored 0.02", and those want opposite fixes.
    #: ~32 tuples, so the cost is noise beside one forward pass.
    ranked: tuple[tuple[str, float], ...] = ()
    #: Wall-clock milliseconds per internal span. An aggregate ``selection_ms``
    #: cannot tell a tokenizer regression from a catalog sync from queue
    #: contention, and those are the three things that move.
    timings: dict[str, float] = field(default_factory=dict)
    #: runbook id -> which admission rule rejected it (``REJECTED_*``), for every
    #: ranked candidate that did not make the answer. Produced BY ``admit`` so a
    #: diagnosis cannot drift from the rule it describes — see
    #: ``admit_with_reasons``.
    rejected: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RunbookTable:
    """Embedded runbooks: what the shortlist searches and the head reads.

    Immutable and swapped wholesale rather than mutated, because it is read from
    an inference worker thread while the catalog can be reloaded underneath it
    (runbook learning). ``digests`` keys each row to the exact document text it was
    built from, so a reload re-embeds precisely the runbooks whose routing surface
    moved and reuses every other vector.
    """

    ids: tuple[str, ...]
    #: (n, retriever_dim), L2-normalised — cosine is a plain dot product.
    retrieval: np.ndarray
    #: (n, ranking_dim), mean-pooled; what the set-wise head consumes.
    ranking: np.ndarray
    #: runbook id -> sha256 of the rendered document it was embedded from.
    digests: dict[str, str]
    #: Which encoder precision produced these vectors ("int8" / "fp32").
    precision: str


def _since(started: float) -> float:
    """Milliseconds since ``started``, rounded to something a log can carry."""
    return round((time.perf_counter() - started) * 1000.0, 3)


def document_digest(document: str) -> str:
    return hashlib.sha256(document.encode()).hexdigest()


#: Distinct runbook documents whose vectors are held across table replacements.
#: Sized for a catalog an order of magnitude larger than today's 39 plus room for
#: runbook-learning rewrites; at ~3 KB a pair this is single-digit megabytes.
_VEC_MEMO_MAX = 2048


class SetwiseClassifier:
    """Two-stage runbook selection: cosine shortlist, then one set-wise judgement."""

    def __init__(self, directory: Path, *, quantized: bool = True) -> None:
        """int8 ranking encoder, fp32 retriever and head.

        The ranking encoder carries ~80% of the parameters and the memory
        pressure, and quantising it is gated on decision parity in
        ``training/export_two_stage.py``. The retriever is 22.7M parameters
        (+67MB in fp32) and is the shortlist nothing downstream can repair:
        quantising it as well never gained a case on eight artifacts and cost
        one to six on five of them (training/EXPERIMENTS.md, 2026-09-28). The head
        is ~2.4M parameters — under 10MB in fp32 — so quantising it would trade
        measurable numerical slack for memory nobody is short of.
        ``quantized=False`` runs every stage in fp32.
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
        #: The stamp names the ranker's precision alone because the retriever is
        #: fp32 in both modes. It is not plain "int8": a table stamped that was
        #: built with an int8 retriever, and must re-embed rather than be reused.
        self._precision = "int8-ranker" if quantized else "fp32"
        self._retriever = _session(directory / "retriever" / "model.onnx")
        self._ranker = _session(
            directory / "ranking" / ("model_int8.onnx" if quantized else "model.onnx")
        )
        self._head = _session(directory / "head.onnx")

        self._retriever_tok = _tokenizer(directory / "retriever", self._query_max)
        self._ranker_tok = _tokenizer(directory / "ranking", self._query_max)
        # Documents get their own tokenizer instances: truncation length is
        # tokenizer state, and sharing one instance across queries and documents
        # would silently apply whichever cap was configured last.
        self._retriever_doc_tok = _tokenizer(directory / "retriever", self._doc_max)
        self._ranker_doc_tok = _tokenizer(directory / "ranking", self._doc_max)
        # Counting only, so truncation and padding are explicitly OFF.
        #
        # Explicitly, because ``Tokenizer.from_file`` carries whatever the JSON
        # declares — the shipped file already sets a 256-token truncation, so a
        # freshly loaded instance silently caps at exactly the number this is
        # trying to measure against. It reported "fits" for every string in the
        # world, which is a counter that always agrees.
        self._counter_tok = Tokenizer.from_file(
            str(directory / "retriever" / "tokenizer.json")
        )
        self._counter_tok.no_truncation()
        self._counter_tok.no_padding()

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
        #: re-hashing every runbook document on a turn where the catalog has not
        #: changed at all: microseconds at 38 runbooks, milliseconds at 1,000, and
        #: this sits on the first-byte path.
        self._cached: tuple[str | None, RunbookTable] | None = None
        #: Vector pairs by document digest, surviving every table replacement.
        #: See the note in ``sync``: ``_cached`` is one slot, and a profile that
        #: syncs a NARROWED catalog evicts the rest of the catalog's vectors from
        #: it. LRU-bounded because it is the one structure here that is not a pure
        #: function of the live catalog — runbook learning rewrites bodies, and each
        #: rewrite mints a digest the old one no longer matches.
        self._vec_memo: OrderedDict[str, tuple[np.ndarray, np.ndarray]] = OrderedDict()
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
        """How many runbooks reach the head. Shortlist width, not a catalog size."""
        return self._k

    def query_fits(self, text: str) -> bool:
        """Would the encoder receive ALL of ``text``, or cut it at the cap?

        Exposed because the render contract lives in ``documents.py``, which has
        no tokenizer — the tokenizer is part of the artifact. ``fit_query`` calls
        this to shrink a query deliberately rather than letting truncation take
        the tail, which is where the turn context and, with dense enough history,
        the newest instruction live.

        Measured on the UNTRUNCATED tokenizer: the serving instances have a cap
        configured, so their ``len(ids)`` can never exceed it and could not
        distinguish "exactly fits" from "was cut".
        """
        return len(self._counter_tok.encode(text).ids) <= self._query_max

    @property
    def table(self) -> RunbookTable | None:
        cached = self._cached
        return cached[1] if cached else None

    # ------------------------------------------------------------------ sync

    def sync(self, catalog: Any) -> RunbookTable:
        """Bring the runbook table in line with the live catalog.

        The cheap path — every selectable runbook already embedded under the same
        document digest — does a dict comparison and returns the existing table.
        Anything else (a new runbook from runbook learning, an edited whenToUse, a
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
            sid: runbook_document(catalog.get(sid).meta, variant=self._doc_variant)
            for sid in ids
        }
        digests = {sid: document_digest(doc) for sid, doc in wanted.items()}

        with self._lock:
            cached = self._cached
            current = cached[1] if cached else None
            if current is not None and current.digests == digests:
                # Same routing surface, different catalog revision — a body-only
                # edit, which is the common runbook-learning case. Nothing to
                # re-embed; just remember the new revision so the next turn takes
                # the fast path.
                self._remember(current)
                self._cached = (revision, current)
                return current

            keep = current.digests if current is not None else {}
            reuse = {
                sid: i
                for i, sid in enumerate(current.ids if current else ())
                if keep.get(sid) == digests.get(sid)
            }

            retrieval = np.zeros(
                (len(ids), self._retrieval_dim(current)), dtype=np.float32
            )
            ranking = np.zeros((len(ids), self._ranking_dim(current)), dtype=np.float32)
            # Anything the CURRENT table cannot supply is looked up in the digest
            # memo before it is sent to the encoders. `_cached` holds exactly one
            # table, so a narrower catalog — the sub-agent profile's filtered menu
            # — replaces it and takes the vectors for every excluded runbook with
            # it. The memo is what makes that recoverable: measured at 39 runbooks,
            # a single sub-agent turn used to cost the NEXT orchestrator turn
            # 5.3s of re-embedding, on the first-byte path, for a table it had
            # already built. Keyed by document digest rather than id so a
            # rewritten runbook misses and is re-embedded, exactly as before.
            stale: list[str] = []
            for i, sid in enumerate(ids):
                if sid in reuse and current is not None:
                    retrieval[i] = current.retrieval[reuse[sid]]
                    ranking[i] = current.ranking[reuse[sid]]
                    continue
                remembered = self._vec_memo.get(digests[sid])
                if remembered is not None:
                    retrieval[i], ranking[i] = remembered
                    self._vec_memo.move_to_end(digests[sid])
                    continue
                stale.append(sid)
            if stale:
                # ONE document per encoder call, never a batch. The int8 graphs
                # quantise activations dynamically — the scale is taken over the
                # whole input tensor — so a batched document's vector depended on
                # which OTHER documents happened to be stale alongside it: measured
                # at 6-7% relative L2 between the same runbook encoded alone and
                # encoded with the rest of the catalog, enough to change six
                # benchmark decisions between two tables built from identical
                # weights. Encoding singly makes a runbook's vector a function of
                # its own text, whichever path (export, cold load, reload, a
                # sub-agent's narrowed menu) happens to build it.
                positions = {sid: i for i, sid in enumerate(ids)}
                for sid in stale:
                    doc = [wanted[sid]]
                    retrieval[positions[sid]] = _run_encoder(
                        self._retriever, self._retriever_doc_tok, doc, normalize=True
                    )[0]
                    ranking[positions[sid]] = _run_encoder(
                        self._ranker, self._ranker_doc_tok, doc, normalize=False
                    )[0]

            table = RunbookTable(
                ids=ids,
                retrieval=retrieval,
                ranking=ranking,
                digests=digests,
                precision=self._precision,
            )
            self._remember(table)
            self._cached = (revision, table)
            return table

    def _remember(self, table: RunbookTable) -> None:
        """Hold every row of ``table`` in the digest memo.

        EVERY row, not just freshly encoded ones — the memo's whole job is to
        outlive the table it came from, and a row carried over from a previous
        table is about to become unreachable if the next sync is for a narrower
        catalog. Called on both sync paths, including the early return: the
        shipped ``vectors.npz`` reaches the process through that path, so
        skipping it there left the memo empty until the first re-embed and made
        the first profile alternation pay the full cost anyway.

        Caller holds ``self._lock``, or is the constructor.
        """
        for i, sid in enumerate(table.ids):
            digest = table.digests[sid]
            if digest in self._vec_memo:
                self._vec_memo.move_to_end(digest)
                continue
            # Copies, not row views: a view keeps the whole 2-D array alive,
            # which would pin every table the memo ever touched.
            self._vec_memo[digest] = (table.retrieval[i].copy(), table.ranking[i].copy())
        while len(self._vec_memo) > _VEC_MEMO_MAX:
            self._vec_memo.popitem(last=False)

    def _retrieval_dim(self, current: RunbookTable | None) -> int:
        if current is not None and current.retrieval.size:
            return int(current.retrieval.shape[1])
        # From meta, not from the graph's declared output shape: an ONNX
        # dimension can come back as a symbolic NAME rather than an int, and
        # int() on it raises at load time for no reason.
        return int(self._meta["retriever_hidden_size"])

    def _ranking_dim(self, current: RunbookTable | None) -> int:
        if current is not None and current.ranking.size:
            return int(current.ranking.shape[1])
        return int(self._meta["hidden_size"])

    # ---------------------------------------------------------------- select

    def _encode_pass(
        self,
        table: RunbookTable,
        text: str,
        active: set[str],
        spans: dict[str, float] | None = None,
    ) -> tuple[CandidateSet, dict[str, np.ndarray]]:
        """Build one complete, self-consistent set of head inputs for ``text``.

        Extracted so the docs pass CANNOT be built half from another query's
        evidence. Every tensor here is derived from this one string: the
        shortlist, each candidate's cosine to it, each candidate's rank within
        it. Mixing those across two different queries hands the head a
        combination it never saw in training — the trainer always rendered the
        shortlist and the ranker query from the same text — so the seam is closed
        by construction rather than by a comment asking the next reader to be
        careful.

        ``spans`` accumulates per-stage milliseconds when a caller supplies one.
        It is written to rather than returned so the docs pass can add its own
        timings to the same record without the caller stitching two dicts.

        Which runbooks end up on the ballot is ``candidates.assemble_candidates``,
        not a top-k slice: an active runbook dense retrieval missed is placed there
        anyway, because the head cannot keep a runbook it never sees and a
        successful selection is authoritative enough to evict one. Read that
        module for why, and for what it deliberately does not promise.
        """
        spans = {} if spans is None else spans
        # normalize=True is redundant — the retriever graph L2-normalises
        # internally — and kept because it is idempotent and makes the cosine
        # contract local: `sims` below is a plain dot product only if BOTH sides
        # are unit vectors, and that should not depend on reading the exporter.
        started = time.perf_counter()
        q_ret = _run_encoder(self._retriever, self._retriever_tok, [text], normalize=True)[0]
        spans["retriever_ms"] = _since(started)

        started = time.perf_counter()
        sims = table.retrieval @ q_ret
        chosen = assemble_candidates(sims, table.ids, active, self._k)
        spans["search_ms"] = _since(started)
        n = len(chosen)

        started = time.perf_counter()
        q_rank = _run_encoder(self._ranker, self._ranker_tok, [text], normalize=False)[0]
        spans["ranker_ms"] = _since(started)
        # Exactly ``self._k`` slots, always. The head's graph is static (see
        # export_two_stage: MultiheadAttention bakes the sequence length into its
        # reshapes), so a catalog smaller than k is served by padding with zeros
        # and mask 0 rather than by a shorter tensor.
        k = self._k
        cand = np.zeros((1, k, table.ranking.shape[1]), dtype=np.float32)
        cand[0, :n] = table.ranking[chosen.indices]
        feats = np.zeros((1, k, len(FEATURE_NAMES)), dtype=np.float32)
        feats[0, :n, 0] = chosen.cosine
        feats[0, :n, 1] = [1.0 if table.ids[i] in active else 0.0 for i in chosen.indices]
        feats[0, :n, 2] = chosen.rank_norm
        mask = np.zeros((1, k), dtype=np.float32)
        mask[0, :n] = 1.0
        return chosen, {
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
        spans: dict[str, float] = {}
        total_started = time.perf_counter()
        started = time.perf_counter()
        table = self.sync(catalog) if catalog is not None else self.table
        # Broken out because it is the one span that is not a forward pass and
        # the one that moves for a reason nobody would guess from a total: a
        # catalog edit re-embeds, and a profile alternation used to re-embed
        # everything (see the memo note in ``sync``). 5.3s hiding inside an
        # 11ms budget looked like "the selector got slow".
        spans["sync_ms"] = _since(started)
        if table is None or not table.ids:
            spans["total_ms"] = _since(total_started)
            return Selection(
                runbook_ids=[], docs_wanted=False, cardinality=0, scores={}, timings=spans
            )

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
        chosen, feed = self._encode_pass(table, text, active, spans)
        n = len(chosen)
        started = time.perf_counter()
        runbook_logits, docs_logit, card_logits = self._head.run(None, feed)
        spans["head_ms"] = _since(started)
        # Padded slots are already driven to -1e4 inside the graph, but they are
        # dropped here as well rather than trusted: a ranking is about to decide
        # what the turn loads, and a padding slot has no runbook id to name.
        probs = 1.0 / (1.0 + np.exp(-runbook_logits[0][:n]))
        count = int(np.argmax(card_logits[0]))
        order = np.argsort(-probs)
        ranked = [
            (table.ids[int(chosen.indices[int(j)])], float(probs[int(j)])) for j in order
        ]
        picked, rejected = admit_with_reasons(ranked, active, count, max_active)
        # Which actives dense retrieval missed and this turn had to seat by hand.
        # Empty on almost every turn today (32 of 38 runbooks are on the ballot
        # anyway) and the number to watch as the catalog grows: it is the rate at
        # which the old top-k-only policy would have been silently evicting
        # in-progress workflows.
        reserved_ids = tuple(
            table.ids[int(i)] for i, was in zip(chosen.indices, chosen.reserved, strict=True)
            if was
        )

        # THE DOCS DECISION IS TAKEN ON A DIFFERENT STRING FROM THE RUNBOOK ONE.
        #
        # The runbook query is deliberately context-laden: an ACTIVE_PREFIX naming
        # the loaded set, plus a 320-char tail of prior turns. Both are what let
        # the head separate a continuation from a topic shift. Neither belongs in
        # the docs decision, and shipping them there was a silent, near-total
        # outage of documentation lookup:
        #
        #   "what is a watcher?"                     docs_p = 0.9998
        #   ...same question, one runbook loaded       docs_p = 0.0020
        #   ...same question, one turn of history    docs_p = 0.00017
        #
        # The cause is in the corpus, not the architecture. In the 8,640-row
        # corpus this mitigation was written against, all 207 docs positives
        # carried an empty active set AND no history, while all 1,956 rows with
        # an active set were labelled not-docs. Worse than that separation being
        # perfect, 64 of those not-docs rows WERE documentation questions — the
        # same question labelled both ways, contradiction rather than absence —
        # so the head got a lossless shortcut, "anything in the context means
        # this is not a docs turn", and took it. Measured over 120 docs positives
        # with a runbook loaded, 5/120 recall; on the bare question, 113/120.
        #
        # The corpus has since moved (9,774 rows, 499 docs positives, 246 of them
        # carrying history, contradictions repaired and asserted against in
        # tests/unit/test_training_corpus_guards.py). THIS PASS STAYS ANYWAY:
        # the docs loss is still trained on the contextual render, so the bare
        # question remains the input distribution this logit is read from, and
        # the retrain moved the docs flag 14/19 -> 19/21 with it in place. Making
        # the second pass unnecessary means training the docs head on its own
        # bare view; that is the next step, not a completed one.
        #
        # So the docs logit is re-read from a SECOND COMPLETE PASS over the bare
        # user question — its own retrieval, its own shortlist, its own cosine
        # and rank features, and no active runbooks. A cheaper version of this
        # reused the primary pass's candidates and features and swapped only the
        # query vector; that is rejected deliberately, because it hands the head
        # a combination that cannot occur in training (a bare docs question
        # scored against another query's shortlist, carrying that query's
        # cosines and an `active` bit no docs positive ever had). The measurement
        # that made reuse look free — equal recall either way — could not have
        # detected the problem: k is 32 against 38 runbooks, so any two shortlists
        # must share at least 26 candidates and retrieval is nearly a no-op. At
        # 64 runbooks two top-32 sets can be disjoint, and the seam would open
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
            started = time.perf_counter()
            # Its own span, and a whole second pass: this is the largest single
            # cost the selector can pay on a turn, and folding it into the
            # totals would make the docs workaround invisible in exactly the
            # profile that would justify retiring it.
            _, docs_feed = self._encode_pass(table, docs_query, set(), {})
            _, docs_logit, _ = self._head.run(None, docs_feed)
            spans["docs_ms"] = _since(started)
        docs_p = float(1.0 / (1.0 + np.exp(-float(docs_logit[0]))))
        spans["total_ms"] = _since(total_started)
        return Selection(
            runbook_ids=picked,
            docs_wanted=docs_p >= docs_threshold,
            cardinality=count,
            scores=dict(ranked[:5]),
            reserved_ids=reserved_ids,
            ranked=tuple(ranked),
            timings=spans,
            rejected=rejected,
        )

    #: Probability a runbook must clear to displace or join an already-loaded set.
    #:
    #: The costs here are asymmetric, and the rule encodes that rather than
    #: treating every pick alike. A false positive is cheap-ish — a loaded
    #: runbook the turn didn't need costs its prompt bytes and a little model
    #: attention. But ADDING a runbook mid-conversation is the more expensive
    #: shape of that mistake: it re-encodes the loaded-runbooks block and
    #: everything after it, and a marginal runbook stacked onto an in-progress
    #: workflow dilutes the runbooks actually driving it. So a NEWCOMER — a
    #: runbook the model wants that is not currently loaded — must earn the slot
    #: when runbooks are already active: fresh turns admit at the model's word,
    #: busy turns demand real confidence.
    #:
    #: 0.75 as of the 2026-09-22 retrain, up from 0.65, and re-derived rather
    #: than carried: on this artifact 0.75 costs nothing on recall-pass at a
    #: fixed bonus floor and buys a trial of extras back at every one of them
    #: (measured over all 207 cases; ``training/calibrate_floors.py``).
    NEWCOMER_FLOOR_WHEN_ACTIVE = 0.75

    #: The recall bonus: the same asymmetry from the other side. A turn that
    #: MISSES its runbook answers without its runbook, the worst outcome
    #: available, while carrying one marginal runbook costs prompt bytes. So
    #: beyond the cardinality head's answer, any further runbook at or above this
    #: floor rides along up to the cap. Applied only when the model picked
    #: SOMETHING — an empty answer ("thanks!", off-domain chatter) stays empty,
    #: because polluting a no-task turn is how loading noise becomes the norm.
    #:
    #: 0.55 as of the 2026-09-22 retrain, and the number is NOT transferable —
    #: it is calibrated to this
    #: artifact's sigmoid. The head that preceded this one shipped 0.55, and the
    #: band genuinely moved: this head is trained with a slot-level BCE over k
    #: candidates rather than a 38-way per-label one, which compresses its
    #: probabilities downward. Re-run ``training/calibrate_floors.py`` after
    #: every export rather than carrying the constant across.
    #:
    #: Chosen at the KNEE, not the peak. On the artifact this floor was last
    #: calibrated against (207 cases, newcomer floor 0.75), recall-pass is flat
    #: at 194 from 0.45 to 0.50 and 193 from 0.55 to 0.60, while the extras fall
    #: 40 → 38; strict-pass moves the other way, 165 → 167, because the cases
    #: being given up are ones the bonus was padding. 0.55 is where that stops
    #: paying: 0.65 spends a second recall-pass case for one further extra.
    #:
    #: The extras figure is also the one gated (``MAX_LOCAL_EXTRA_RATE``, 0.20),
    #: and it is measured per RUNNER: the 0.45 setting read 41/207 = 0.1981 here
    #: and 42/207 = 0.2029 on a non-VNNI x86 CI runner, so a floor chosen at the
    #: local knee alone lands a red build on half the fleet. 0.55 is the lowest
    #: floor that still clears it with the int8 spread accounted for.
    #:
    #: Picking the benchmark's peak instead would be fitting a constant to a
    #: suite that has been inspected many times (see the caveat at the top of
    #: this file's companion, training/EXPERIMENTS.md).
    BONUS_FLOOR = 0.55


def admit(
    ranked: list[tuple[str, float]],
    active: set[str],
    count: int,
    max_active: int,
) -> list[str]:
    """The admission policy: which of the ranked runbooks the turn actually gets.

    Pure so it is testable without the ONNX graphs — every rule below has a unit
    test driving it with hand-built rankings (``tests/unit``).

    ``ranked`` is (runbook id, probability) in DESCENDING probability order;
    ``count`` is the cardinality head's answer; ``active`` is the prior set as
    the selector saw it (selectable ids only).
    """
    return admit_with_reasons(ranked, active, count, max_active)[0]


#: Why a ranked candidate did not make it into the answer. One per rule in
#: :func:`admit_with_reasons`, and the vocabulary the benchmark's stage
#: decomposition is built on.
REJECTED_CARDINALITY = "cardinality"
REJECTED_NEWCOMER_FLOOR = "newcomer_floor"
REJECTED_BONUS_FLOOR = "bonus_floor"
REJECTED_CAP = "cap"


def admit_with_reasons(
    ranked: list[tuple[str, float]],
    active: set[str],
    count: int,
    max_active: int,
) -> tuple[list[str], dict[str, str]]:
    """:func:`admit`, plus the rule that rejected each candidate it did not take.

    The reasons exist because "the turn did not load X" collapses four unrelated
    causes that want four different fixes, and a benchmark cannot tell them apart
    from the outside. It was tried from the outside first, and got two cases
    wrong in ways that pointed at the wrong subsystem: with the cardinality head
    answering zero, ``admit`` never evaluates a floor at all, but a reconstruction
    that checks floors first blames calibration; and a candidate the bonus scan
    stopped short of at the cap was blamed on the floor it never reached.

    So the reasons come from the routine that actually admits. That is the same
    reasoning as the shared candidate assembler and the shared document renderer:
    the diagnosis has to be produced by the code being diagnosed, or it drifts
    from it silently.

    Behaviour is IDENTICAL to the old ``admit``. Both loops now run to the end of
    the ranking rather than breaking early, which changes nothing about the
    result — once ``want`` is met the first loop can no longer append, and the
    ranking is descending so every candidate after the first sub-floor score is
    also sub-floor — and lets a reason be recorded for every candidate rather
    than only the ones examined before the break.
    """
    picked: list[str] = []
    rejected: dict[str, str] = {}
    want = min(max(count, 0), max_active)
    # Scan the WHOLE ranking until `want` candidates are accepted, not just the
    # first `count` rows. Bounding the scan to the count made a rejected
    # newcomer CONSUME its slot: cardinality says one, rank 1 is a filtered
    # newcomer, rank 2 (the active runbook) was never examined — and the turn came
    # back authoritatively empty, evicting a live workflow.
    for sid, prob in ranked:
        if len(picked) >= want:
            rejected[sid] = REJECTED_CARDINALITY
            continue
        # The earn-your-place rule: with a workflow already loaded, a runbook that
        # is NOT part of it needs NEWCOMER_FLOOR_WHEN_ACTIVE, while a runbook the
        # conversation is already running rides on the model's ranking alone. On
        # a fresh turn (nothing active) everything admits on ranking alone —
        # early in a conversation the cheap mistake is to include, not withhold.
        newcomer = bool(active) and sid not in active
        if newcomer and prob < SetwiseClassifier.NEWCOMER_FLOOR_WHEN_ACTIVE:
            rejected[sid] = REJECTED_NEWCOMER_FLOOR
            continue
        picked.append(sid)

    # The recall bonus (see BONUS_FLOOR): with a non-empty answer in hand, admit
    # any further runbook scoring at or above the floor, up to the cap. Newcomers
    # still owe their higher floor when a workflow is active. The ranking is
    # descending, so the first below-floor score ends the scan — deliberately for
    # EVERY candidate, active ones included: the bonus exists to rescue near-miss
    # rankings, not to re-admit a runbook the model scored below a coin flip just
    # because it used to be loaded.
    if picked:
        for sid, prob in ranked:
            if len(picked) >= max_active:
                rejected[sid] = REJECTED_CAP
                continue
            if sid in picked:
                continue
            if prob < SetwiseClassifier.BONUS_FLOOR:
                rejected[sid] = REJECTED_BONUS_FLOOR
                continue
            if (
                active
                and sid not in active
                and prob < SetwiseClassifier.NEWCOMER_FLOOR_WHEN_ACTIVE
            ):
                rejected[sid] = REJECTED_NEWCOMER_FLOOR
                continue
            picked.append(sid)

    for sid in picked:
        rejected.pop(sid, None)
    return picked, rejected


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


def _load_table(path: Path) -> RunbookTable | None:
    """Read a shipped table, or ``None`` if it predates the precision stamp."""
    data = np.load(path, allow_pickle=False)
    if "precision" not in data:
        return None
    ids = tuple(str(x) for x in data["ids"])
    digests = {sid: str(d) for sid, d in zip(ids, data["digests"], strict=True)}
    return RunbookTable(
        ids=ids,
        retrieval=data["retrieval"].astype(np.float32),
        ranking=data["ranking"].astype(np.float32),
        digests=digests,
        precision=str(data["precision"]),
    )


def save_table(path: Path, table: RunbookTable) -> None:
    np.savez(
        path,
        ids=np.array(table.ids),
        digests=np.array([table.digests[sid] for sid in table.ids]),
        retrieval=table.retrieval,
        ranking=table.ranking,
        precision=np.array(table.precision),
    )
