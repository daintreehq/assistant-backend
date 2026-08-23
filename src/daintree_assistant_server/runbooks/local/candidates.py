"""Which runbooks reach the set-wise head — and why each one is there.

Stage 1 answers "what does this turn look like?". This module answers the
different question the head actually needs answered: "what must be on the ballot
before anyone votes?" Those come apart the moment the catalog outgrows the
shortlist, and the gap between them is a correctness hole rather than a quality
one.

## The hole this closes

Candidates used to be exactly the top ``k`` by cosine. The ``active`` feature —
the bit that tells the head "this candidate is the runbook already driving the
conversation" — could therefore only be set on a slot that dense retrieval had
independently decided to return. An active runbook retrieval did not rank was not
scored low; it was **absent**, and a runbook the head never sees is one it cannot
keep.

That is invisible today and gets worse in a specific, measurable way. Two things
hide it. The live catalog is 38 selectable runbooks against ``k=32``, so 84% of it
is on the ballot regardless; and the query carries an ``ACTIVE_PREFIX`` that
names the loaded ids literally, which the retriever reads as a strong signal for
those very documents.

That second one is a real mitigation and it is worth being precise about how far
it goes, because it is why this was not already on fire. Measured against the
shipped int8 retriever and the live catalog, the rank of an active runbook inside
the full 38 is:

    1 active,  vague follow-up ("how's that going?")            rank 0
    1 active,  long unrelated message                           rank 1
    3 actives, vague follow-up                                  ranks 0, 1, 6
    3 actives, long unrelated message                           ranks 8, 10, 11

So the prefix protects a single active runbook almost perfectly and degrades as it
is diluted — by more actives sharing it, and by more user text surrounding it.

It was tempting to extrapolate from there (rank 11 of 38 is the 29th percentile,
which would be rank 145 at 500 runbooks) and that extrapolation turned out to be
wrong. ``bench scale`` measures it directly against catalogs padded to 1,000
runbooks, and the prefix keeps holding: every active-and-required runbook reaches the
ballot on dense retrieval alone at every size tested. See "What this deliberately
is NOT" below — this is insurance against a failure mode, not a fix for one that
has been observed.

What is not extrapolation is the shape of the failure when it does occur. The
messages that dilute the prefix are exactly the ones a running workflow produces:

    "how's that going?"      "try again, the other way"      "now verify it"
    "that failed — fix it"   "keep watching"                 "same for the second one"

And because a SUCCESSFUL selection is authoritative — its ids become the whole
active set (``runbooks/active_set.py``) — a retrieval miss on a turn like that does
not merely fail to re-add the runbook. It **evicts** it, confidently,
mid-workflow. That is why the guarantee is worth having even with no measured
save behind it: the cost is one displaced dense candidate, and the failure it
removes is unrecoverable by anything downstream.

No downstream calibration can repair that. A floor can only re-rank candidates
that exist. So active runbooks are placed on the ballot by construction, and the
head is left to decide the actual question, which it is good at: keep, or
replace on a topic shift.

## What this deliberately is NOT

Reserving a candidate is not retaining it. A reserved runbook arrives with its
real cosine and no thumb on the scale — on a clean topic shift the head scores
it low and admission drops it, which is the behaviour a test in
``tests/unit/test_local_candidates.py`` pins. "Guaranteed a hearing" and
"guaranteed a slot" are different promises and only the first one is made here.

Nor is it a priority mechanism. There is no hand-maintained rank that lets a
favoured runbook win regardless of intent; the only thing that reserves a slot is
a fact about the conversation (this runbook is open) that the head already has a
trained feature for.

And it is not, on the evidence available, a repair for a loss anyone has
measured. ``bench scale`` pads the catalog to 1,000 runbooks with derived
distractors and scores the actives a turn should KEEP — active and also required
— against dense retrieval alone: 13 of 13 at every size, all the way out. The
``ACTIVE_PREFIX`` holds. What does degrade is coverage of *every* visible active
(1.00 → 0.43 across the same range), but most of that is topic-shift cases where
dropping the old runbook is the right answer.

So this is INSURANCE. It costs nothing, and it converts the one failure mode that
no downstream floor can repair into an impossibility — but the honest reading of
the numbers is that the benchmark has 13 continuation-shaped cases and none of
them were at risk. The gap is the corpus, not the finding
(``training/EXPERIMENTS.md``, "Catalog scale").

## What the SHIPPED weights do with a reserved candidate — and why a retrain is owed

Getting a candidate onto the ballot is a policy change. Getting the head to KEEP
it is a weights change, and this artifact was trained before reservation
existed. Measured, by blending one active runbook's retrieval vector toward a
target cosine and re-running the whole selection ("how is that going?", that
runbook loaded):

    cosine   p(runbook)   cardinality   admitted
     0.60      0.999         1         yes
     0.50      0.998         1         yes
     0.40      0.994         1         yes
     0.30      0.989         0         no
     0.20      0.957         0         no
     0.10      0.613         0         no
     0.00      0.078         0         no

Two things to read out of that. The per-candidate probability tracks the cosine
feature hard — 0.999 to 0.078 across the range — and, more decisively, the
CARDINALITY head calls the turn empty from 0.30 down even while the runbook itself
still scores 0.989. So a reserved candidate whose true cosine is genuinely low is
seated, scored, and then dropped by a count of zero.

That is the honest state of this change: the mechanism and the train/serve
parity land here, the benefit lands on the next retrain. The trainer now builds
its ballots with this same function, so reserved candidates appear in training
with ``active=1``, ``rank_norm=1.0`` and a real low cosine — the exact
combination the shipped weights have never been shown. Until then reservation is
a no-op on the live catalog (it never fires; ``tests/unit/test_local_selection.py``
pins that) and correct-but-inert at scale.

## Train/serve parity

The trainer has always done half of this: ``training/train_setwise.py`` swaps a
missing POSITIVE into the weakest candidate slot so a stage-1 miss is not
back-propagated as a stage-2 error. That is a training-only device about labels.
Reservation here is a serving policy about state — but it lands in the same
place, the weakest slot, and so presents the head with a slot shape it has seen
throughout training. The trainer and ``training/eval_two_stage.py`` call this
same function, so the candidate ids and feature tensors they build are the ones
the server builds.

Two details carry that parity:

* **Rank.** A reserved candidate takes ``rank_norm = 1.0`` — the weakest
  position — rather than its true rank in the full catalog. At 500 runbooks a true
  rank of 200 would render as ``200/(k-1)``, a value some six times anything the
  head saw in training, for no gain: the feature's job is to say "retrieval did
  not like this one", and 1.0 already says it.
* **Cosine.** A reserved candidate keeps its ACTUAL cosine, on both sides. The
  trainer used to write the shortlist minimum for a swapped-in positive, since
  it discarded the full similarity row — defensible while the ballot was a pure
  top-k, because an off-ballot runbook scores at or below that minimum by
  definition. Reservation broke that: once a deeply-ranked active can be seated,
  the ballot minimum IS that active's cosine, and a positive swapped in beside
  it would inherit a number far below its own. The trainer now keeps the full
  similarity row and writes the true value, so the two sides agree exactly.
"""

from __future__ import annotations

from collections.abc import Container, Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt


def _rank(sims: npt.NDArray[np.float32], n: int) -> npt.NDArray[np.int64]:
    """The ``n`` best rows, by descending cosine, ties broken by table position.

    A full ``lexsort`` rather than the ``argpartition`` this used to be, and the
    reason is train/serve parity rather than taste. The tie-break has to be
    TOTAL, at the k boundary as well as within the shortlist: ``argpartition``
    does not specify WHICH of a set of tied rows it returns, and numpy's default
    sort is unstable, so the trainer and the server could disagree about both the
    membership and the ``rank_norm`` of two equal-scoring runbooks. That is a skew
    with no error attached to it — exactly the failure mode this whole pipeline
    is built to avoid. Table position is id order (the table is built from
    sorted ids), so the secondary key is stable across processes, versions and
    architectures.

    The O(n) → O(n log n) it gives up is not a real cost here. Measured on this
    machine, the full sort is FASTER than the partition at today's 38 runbooks and
    trails it by 3.5 µs at 500 and 8.4 µs at 1,000 — against a selector budget of
    ~11,500 µs, or 0.07% at the far end of the planned catalog. Exact float32
    ties between distinct runbook documents do not occur in practice; the point is
    that when they do, both sides break them the same way.
    """
    return np.lexsort((np.arange(len(sims)), -sims))[:n].astype(np.int64, copy=False)


@dataclass(frozen=True)
class CandidateSet:
    """One turn's ballot: which table rows are scored, and what the head is told.

    Every array is parallel and in SLOT order — the order the head receives them
    in, which carries no meaning (the head has no positional encoding; see
    ``setwise.py``). Retrieval evidence travels in ``cosine`` and ``rank_norm``
    instead, which is what lets a reserved candidate be added without lying
    about how retrieval scored it.
    """

    #: Row indices into the runbook table, in slot order.
    indices: npt.NDArray[np.int64]
    #: Cosine of each candidate to the query. Real values, reserved ones too.
    cosine: npt.NDArray[np.float32]
    #: Normalised retrieval rank; 1.0 for a reserved candidate (see module docs).
    rank_norm: npt.NDArray[np.float32]
    #: True where the candidate was force-included rather than retrieved.
    reserved: npt.NDArray[np.bool_]

    def __len__(self) -> int:
        return int(self.indices.shape[0])


def assemble_candidates(
    sims: npt.NDArray[np.float32],
    ids: Sequence[str],
    active: Container[str],
    k: int,
) -> CandidateSet:
    """Build the candidate ballot for one query.

    ``sims`` is the cosine of the query against EVERY row of the runbook table,
    parallel to ``ids``; ``active`` holds the runbook ids already loaded, already
    projected to selectable ones by the caller. ``k`` is the head's slot count,
    which fixes the ``rank_norm`` scale even when fewer candidates exist.

    Returns at most ``min(k, len(ids))`` candidates: dense retrieval's best,
    with every active runbook it missed swapped in over the weakest candidate that
    is not itself active. ``ids`` is assumed unique — ``Catalog`` guarantees it,
    and a repeated id would give one runbook two votes in the set-wise pass.

    Raises ``ValueError`` on a malformed similarity row. That is deliberate and
    it is the SAFE direction: the caller is ``InferencePool.run``, which turns an
    exception into a degrade-open reuse of the prior active set. The alternative
    — assembling a ballot out of a NaN row and letting the head score it — comes
    back as a *successful* selection, and a successful selection is authoritative
    enough to replace the whole active set with whatever garbage won.
    """
    if sims.ndim != 1 or sims.shape[0] != len(ids):
        raise ValueError(
            f"similarity row has shape {sims.shape}, expected one score per "
            f"each of {len(ids)} runbooks"
        )
    if sims.size and not np.isfinite(sims).all():
        raise ValueError("similarity row is not finite; refusing to rank on it")

    total = len(ids)
    n = min(k, total)
    if n <= 0:
        return CandidateSet(
            np.zeros(0, dtype=np.int64),
            np.zeros(0, dtype=np.float32),
            np.zeros(0, dtype=np.float32),
            np.zeros(0, dtype=bool),
        )

    scale = float(max(k - 1, 1))
    rank_norm = np.arange(n, dtype=np.float32) / scale
    reserved = np.zeros(n, dtype=bool)

    top = _rank(sims, n)
    if n >= total:
        # The whole catalog is on the ballot, so there is nothing to reserve and
        # nothing to displace — every active runbook is already here.
        return CandidateSet(top, sims[top].astype(np.float32), rank_norm, reserved)

    retrieved = set(top.tolist())
    missing = [i for i in range(total) if ids[i] in active and i not in retrieved]
    if missing:
        # Closest first, so that if there are somehow more absent actives than
        # displaceable slots, the ones the query is nearest survive the cut.
        missing.sort(key=lambda i: (-float(sims[i]), ids[i]))
        # Fill from the weakest end, stepping over any slot already holding an
        # active runbook: evicting one open runbook to seat another would trade
        # the bug for itself. Mirrors the trainer's positive-swap loop.
        slot = n - 1
        for row in missing:
            while slot >= 0 and ids[int(top[slot])] in active:
                slot -= 1
            if slot < 0:
                break
            top[slot] = row
            rank_norm[slot] = 1.0
            reserved[slot] = True
            slot -= 1

    return CandidateSet(top, sims[top].astype(np.float32), rank_norm, reserved)
