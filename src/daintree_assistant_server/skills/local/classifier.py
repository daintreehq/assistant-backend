"""The shipped skill selector: one encoder, three heads, one forward pass.

## The architecture, and why it is this one

    conversation text -> bge-base encoder -> mean-pooled 768-d vector
                                            |
                        +-------------------+-------------------+
                        |                   |                   |
                  32 skill logits      1 docs logit      4 cardinality logits
                   (which skills)    (retrieve docs?)     (how many skills)

The skill head's weight matrix IS a set of 32 learned skill vectors — logit_i is
row i dotted with the pooled message embedding. That makes "embed the message,
embed every skill, compare" and "one linear head" the same computation, except
the head *learns* the skill vectors instead of inheriting them. It also means the
cost of a bigger catalog is one extra ROW (768 floats), not a wider input: at 150
skills the head is ~115k parameters and the matmul is microseconds.

## Why a cardinality head instead of a threshold

A per-skill threshold cannot answer "this turn needs zero" and "this turn needs
two" at the same operating point — it can only trade one against the other.
Measured: sweeping the threshold moved the benchmark between 75 and 113 of 150
without ever getting both right. The cardinality head predicts the COUNT directly
(96% accurate on held-out synthetic), and the count then decides how many of the
ranked skills to take. Zero is a first-class answer rather than the absence of
one.

## What this replaced

A bi-encoder shortlist plus a fine-tuned cross-encoder reranker. That design was
built, trained and measured, and the reranker was actively harmful: on an
identical shortlist it ranked the correct skill first LESS often than the
bi-encoder that produced the shortlist (recall@1 0.699 vs 0.779), pushing the
right answer off rank 1 in 20 cases and pulling it on in 9. Its cost was also the
whole latency budget — 309ms of a 312ms pipeline. Removing it took the selector
from 309ms to ~2ms and raised accuracy at the same time.

The bi-encoder survives as an optional fusion signal (it is the better pure
ranker) and as the zero-shot path for skills added after the last training run.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

#: Prefix carrying conversation STATE into the query text.
#:
#: Continuation and topic-shift turns are near-identical in wording and differ
#: only in which skills are already open, so the model cannot separate them from
#: the words alone. Measured without it: continuation 9/9 and topic-shift 1/9 at
#: one history budget, 4/9 and 6/9 at another — a pure trade with no setting
#: winning both.
ACTIVE_PREFIX = "[loaded: {}] "

#: Sequence cap. Conversation queries measured p90 ~64 tokens on the benchmark;
#: 256 is generous and costs nothing, since attention cost tracks the ACTUAL
#: batch length rather than the cap.
MAX_TOKENS = 256


@dataclass(frozen=True)
class Selection:
    """One turn's answer."""

    skill_ids: list[str]
    docs_wanted: bool
    #: Predicted number of skills, before the cap is applied.
    cardinality: int
    #: Per-skill probability, for logging and diagnostics.
    scores: dict[str, float]


class SkillClassifier:
    """Multi-label skill selection in a single ONNX forward pass."""

    def __init__(self, directory: Path, *, quantized: bool = True) -> None:
        """int8 is the shipped precision — for MEMORY, and it is safe here.

        Measured on the bge-base export: int8 is 300MB resident against fp32's
        665MB, and fp32 plus the server does not fit the container's memory
        budget. Unlike the earlier MiniLM export (where int8 flipped which skill
        ranked first and was rejected), bge-base int8 PASSES the export's
        decision-parity check — same top skill, same cardinality — and scored at
        least as well on the benchmark. The parity gate in
        ``training/export_classifier.py`` is what keeps this safe across
        retrains: a future export whose int8 changes decisions will say so.
        """
        graph = directory / ("model_int8.onnx" if quantized else "model.onnx")
        opts = ort.SessionOptions()
        # One intra-op thread: the serving shape is many small concurrent
        # requests on a 1-vCPU container, so parallelism belongs at the request
        # level, not inside a 2ms forward pass.
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            str(graph), opts, providers=["CPUExecutionProvider"]
        )

        meta = json.loads((directory / "meta.json").read_text())
        self._labels: list[str] = json.loads((directory / "labels.json").read_text())
        self._n_labels = len(self._labels)
        self._docs_index = int(meta["docs_index"])
        self._card_start = self._n_labels + 1
        self._card_classes = int(meta["cardinality_classes"])
        self._revision: str = meta.get("routing_revision", "")

        # An artifact is three files that must agree — a labels.json from one
        # export beside a model.onnx from another loads fine and maps every
        # logit to the wrong skill. Caught at load, loudly, rather than served.
        if len(set(self._labels)) != self._n_labels:
            raise ValueError("classifier labels.json contains duplicate ids")
        if self._docs_index != self._n_labels:
            raise ValueError(
                f"classifier meta docs_index={self._docs_index} but labels={self._n_labels}"
            )
        expected_out = self._n_labels + 1 + self._card_classes
        if int(meta["n_out"]) != expected_out:
            raise ValueError(
                f"classifier meta n_out={meta['n_out']} != labels+1+card={expected_out}"
            )
        graph_out = self._session.get_outputs()[0].shape[-1]
        if isinstance(graph_out, int) and graph_out != expected_out:
            raise ValueError(
                f"classifier graph emits {graph_out} logits, meta expects {expected_out}"
            )

        self._tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=MAX_TOKENS)
        pad = self._tokenizer.token_to_id("[PAD]")
        self._tokenizer.enable_padding(pad_id=int(pad or 0), pad_token="[PAD]")

    @property
    def labels(self) -> list[str]:
        return list(self._labels)

    @property
    def routing_revision(self) -> str:
        """The ROUTING surface this head was trained against.

        Stamped at TRAIN time (documents.routing_revision — a hash over
        id/title/summary/whenToUse of every selectable skill, deliberately NOT
        the body-inclusive catalog revision, so skill-learning body edits do not
        stale the head). The caller recomputes the same hash from the live
        catalog and refuses to serve on mismatch: a stale head fails silently
        and confidently otherwise, which is the worst failure available here.
        """
        return self._revision

    #: Probability a skill must clear to displace or join an already-loaded set.
    #:
    #: The costs here are asymmetric, and the rule encodes that rather than
    #: treating every pick alike. A false positive is cheap-ish — a loaded
    #: runbook the turn didn't need costs its prompt bytes and a little model
    #: attention. But ADDING a skill mid-conversation is the more expensive
    #: shape of that mistake: it re-encodes the loaded-skills block and
    #: everything after it, and a marginal runbook stacked onto an in-progress
    #: workflow dilutes the runbooks that are actually driving it. So a
    #: NEWCOMER — a skill the model wants that is not currently loaded — must
    #: earn the slot when skills are already active: fresh turns admit at the
    #: model's word, busy turns demand real confidence.
    NEWCOMER_FLOOR_WHEN_ACTIVE = 0.65

    def select(
        self,
        query: str,
        active_ids: Sequence[str] = (),
        *,
        max_active: int = 3,
        docs_threshold: float = 0.5,
    ) -> Selection:
        text = ACTIVE_PREFIX.format(", ".join(active_ids)) + query if active_ids else query
        encoded = self._tokenizer.encode(text)
        ids = np.array([encoded.ids], dtype=np.int64)
        mask = np.array([encoded.attention_mask], dtype=np.int64)
        logits = self._session.run(
            None, {"input_ids": ids, "attention_mask": mask}
        )[0][0]

        skill_logits = logits[: self._n_labels]
        probs = 1.0 / (1.0 + np.exp(-skill_logits))
        count = int(np.argmax(logits[self._card_start : self._card_start + self._card_classes]))
        order = np.argsort(-probs)

        active = set(active_ids)
        picked: list[str] = []
        for i in order[: max(count, 0)]:
            sid = self._labels[int(i)]
            if len(picked) >= max_active:
                break
            # The earn-your-place rule: with a workflow already loaded, a skill
            # that is NOT part of it needs NEWCOMER_FLOOR_WHEN_ACTIVE, while a
            # skill the conversation is already running rides on the model's
            # ranking alone. On a fresh turn (nothing active) everything admits
            # on ranking alone — early in a conversation the cheap mistake is
            # to include, not to withhold.
            newcomer = active and sid not in active
            if newcomer and float(probs[int(i)]) < self.NEWCOMER_FLOOR_WHEN_ACTIVE:
                continue
            picked.append(sid)

        docs_p = float(1.0 / (1.0 + np.exp(-logits[self._docs_index])))
        return Selection(
            skill_ids=picked,
            docs_wanted=docs_p >= docs_threshold,
            cardinality=count,
            scores={self._labels[int(i)]: float(probs[int(i)]) for i in order[:5]},
        )
