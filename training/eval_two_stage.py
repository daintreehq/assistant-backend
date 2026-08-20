"""The canonical evaluation: the SHIPPED two-stage artifact, under the SHIPPED policy.

Drives ``SetwiseClassifier`` loaded from the exported ONNX directory — not torch
checkpoints, not a different admission rule — and applies the decision procedure
``LocalSelector`` runs in production (visible-active projection, background-wake
rule, earn-your-place admission). Anything else measures a system that does not
ship.

Reports fp32 and int8 and the decision diff between them, because int8 is what
ships and "the logits moved a little" is not the question — "did any DECISION
change" is.

``--ablate`` re-runs the same artifact with the shortlist's evidence disturbed,
which is the only honest way to answer two questions this architecture invites:
does the head actually depend on the retrieval order, and does a skill that never
appeared in training still route?
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from benchmarks.skill_selection.cases import load_cases  # noqa: E402
from benchmarks.skill_selection.scoring import score_trial, summarize  # noqa: E402
from daintree_assistant_server.skills.catalog import (  # noqa: E402
    default_skills_dir,
    load_catalog,
)
from daintree_assistant_server.skills.local.documents import (  # noqa: E402
    last_user_text,
    query_document,
)
from daintree_assistant_server.skills.local.setwise import SetwiseClassifier  # noqa: E402

#: Release floor for the product metric (every required skill present). The
#: architecture this replaced measured 158/169; the family's error bar is ±2-3,
#: so the floor sits at the bottom of that band rather than at the peak.
RECALL_PASS_FLOOR = 152

ARTIFACT = (
    ROOT / "src" / "daintree_assistant_server" / "skills" / "local"
    / "artifacts" / "selector"
)


def run(clf: SetwiseClassifier, catalog, cases):  # type: ignore[no-untyped-def]
    """Score one artifact under the serving decision procedure."""
    selectable = catalog.selectable_ids
    results = []
    picked_all: list[list[str]] = []
    latencies: list[float] = []
    for case in cases:
        query = query_document(case.messages, turn_context=case.turn_context)
        eligible = [m for m in case.messages if m.role in ("user", "assistant", "tool")]
        started = time.perf_counter()
        if case.active and eligible and eligible[-1].role != "user":
            picked = list(case.active)
            docs_q = None
        else:
            visible = [sid for sid in case.active if sid in selectable]
            # Mirror the SERVER via the SHARED renderer: the docs decision is
            # taken on the bare newest user message, not the context-laden
            # ranking query. Scoring it any other way measures a behaviour that
            # does not ship — and the docs flag is exactly where that mattered,
            # since the head reads ~0 through an ACTIVE_PREFIX or a history tail.
            # `last_user_text` rather than a local re-implementation so a typed
            # or whitespace-heavy case cannot be scored on a string the server
            # would never build.
            sel = clf.select(query, visible, docs_query=last_user_text(case.messages))
            picked = [sid for sid in sel.skill_ids if sid in selectable]
            docs_q = "q" if sel.docs_wanted else None
        latencies.append((time.perf_counter() - started) * 1000)
        picked_all.append(picked)
        results.append(
            score_trial(
                case, 0, picked, degraded=False, latency_s=0.0, confidence=None,
                task_type=None, reason=None, prompt_tokens=0, completion_tokens=0,
                docs_query=docs_q,
            )
        )
    return summarize(cases, results), results, picked_all, latencies


def recall_pass_flags(cases, picked_all) -> list[bool]:  # type: ignore[no-untyped-def]
    flags = []
    for case, picked in zip(cases, picked_all, strict=True):
        got = set(picked)
        miss = set(case.required) - got
        extra = got - set(case.required) - set(case.optional)
        flags.append(not miss and (bool(case.required) or not extra))
    return flags


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", default=str(ARTIFACT))
    ap.add_argument(
        "--ablate",
        action="store_true",
        help="also score the shortlist REVERSED — the permutation-equivariance "
        "check. A head that reads order will move; one that does not will not.",
    )
    args = ap.parse_args()

    catalog = load_catalog(default_skills_dir())
    cases = load_cases(catalog)
    artifact = Path(args.artifact)
    meta = json.loads((artifact / "meta.json").read_text())
    print(
        f"artifact: k={meta['candidates']}  ordering={meta['ordering']}  "
        f"stage1={meta['retriever_base_model']}  stage2={meta['base_model']}"
    )
    if meta.get("holdout_skills"):
        print(f"  ZERO-SHOT PROBE: {', '.join(meta['holdout_skills'])} never trained on")

    outputs = {}
    for quantized in (False, True):
        tag = "int8" if quantized else "fp32"
        clf = SetwiseClassifier(artifact, quantized=quantized)
        clf.sync(catalog)
        summary, _results, picked, lat = run(clf, catalog, cases)
        outputs[tag] = (summary, picked, lat)
        lat_sorted = sorted(lat)
        rp = sum(recall_pass_flags(cases, picked))
        print(
            f"\n{tag}: recall-pass {rp}/{len(cases)} ({rp / len(cases) * 100:.1f}%)  "
            f"strict {summary['cases_passed']}/{summary['cases_total']}  "
            f"miss {summary['recall_miss_trials']} "
            f"extra {summary['precision_extra_trials']}  "
            f"docs {summary['docs']['correct_trials']}"
            f"/{summary['docs']['scored_trials']}  "
            f"p50 {lat_sorted[len(lat_sorted) // 2]:.1f} ms  "
            f"mean {statistics.mean(lat):.1f} ms"
        )

    diffs = [
        (c.id, a, b)
        for c, a, b in zip(cases, outputs["fp32"][1], outputs["int8"][1], strict=True)
        if a != b
    ]
    print(f"\nfp32 vs int8 decision diff: {len(diffs)} of {len(cases)} cases")
    for cid, a, b in diffs[:8]:
        print(
            f"  {cid}: fp32={[x.rsplit('.', 1)[-1] for x in a]} "
            f"int8={[x.rsplit('.', 1)[-1] for x in b]}"
        )

    flags = recall_pass_flags(cases, outputs["int8"][1])
    print("\nby category (int8, recall-pass — the product metric):")
    by: dict[str, list[bool]] = defaultdict(list)
    for case, ok in zip(cases, flags, strict=True):
        by[case.category].append(ok)
    for cat in sorted(by):
        rows = by[cat]
        print(f"  {cat:22} {sum(rows):3d}/{len(rows)}")

    print("\nmisses (int8):")
    for case, ok, picked in zip(cases, flags, outputs["int8"][1], strict=True):
        if not ok:
            print(
                f"  {case.id:42} want={[x.rsplit('.', 1)[-1] for x in case.required]} "
                f"got={[x.rsplit('.', 1)[-1] for x in picked]}"
            )

    if args.ablate:
        _ablate(artifact, catalog, cases, outputs["int8"][1])

    int8_rp = sum(flags)
    if int8_rp < RECALL_PASS_FLOOR:
        print(f"\nFAIL: int8 recall-pass {int8_rp} < floor {RECALL_PASS_FLOOR}")
        return 1
    return 0


def _ablate(artifact: Path, catalog, cases, baseline_picked) -> None:  # type: ignore[no-untyped-def]
    """Permutation-equivariance check, run through the real graph.

    Reverses the shortlist (and its features with it) before the head sees it.
    An equivariant head returns byte-identical decisions; a head that has learned
    "slot 0 usually wins" moves. Reported rather than asserted, because the
    positional variant is a legitimate configuration that WILL move here.
    """
    import numpy as np

    clf = SetwiseClassifier(artifact, quantized=True)
    clf.sync(catalog)
    selectable = catalog.selectable_ids
    moved = 0
    scored = 0
    # Only the SHORTLIST order is disturbed — the same candidates, handed to the
    # head back to front. The head's own ranking argsort stays intact, so any
    # movement here is the head reading position, not the scoring changing.
    for case, before in zip(cases, baseline_picked, strict=True):
        eligible = [m for m in case.messages if m.role in ("user", "assistant", "tool")]
        if case.active and eligible and eligible[-1].role != "user":
            continue  # a background wake never reaches the model at all
        query = query_document(case.messages, turn_context=case.turn_context)
        visible = [sid for sid in case.active if sid in selectable]
        after = _select_reversed(clf, query, visible, np)
        scored += 1
        moved += sorted(after) != sorted(before)
    print(
        f"\nshortlist-order ablation: {moved}/{scored} scored cases changed answer "
        "when the shortlist is reversed (0 == permutation-equivariant)"
    )


def _select_reversed(clf, query, active_ids, np):  # type: ignore[no-untyped-def]
    """Re-run one selection with the shortlist handed over in reverse order."""
    from daintree_assistant_server.skills.local.documents import ACTIVE_PREFIX
    from daintree_assistant_server.skills.local.setwise import (
        FEATURE_NAMES,
        _run_encoder,
        admit,
    )

    table = clf.table
    text = ACTIVE_PREFIX.format(", ".join(active_ids)) + query if active_ids else query
    q_ret = _run_encoder(clf._retriever, clf._retriever_tok, [text], normalize=True)[0]
    sims = table.retrieval @ q_ret
    k = min(clf._k, len(table.ids))
    top = np.argpartition(-sims, k - 1)[:k] if k < len(sims) else np.arange(len(sims))
    top = top[np.argsort(-sims[top])][::-1]

    q_rank = _run_encoder(clf._ranker, clf._ranker_tok, [text], normalize=False)[0]
    cand = table.ranking[top][None, :, :].astype(np.float32)
    active = set(active_ids)
    feats = np.zeros((1, k, len(FEATURE_NAMES)), dtype=np.float32)
    feats[0, :, 0] = sims[top]
    feats[0, :, 1] = [1.0 if table.ids[i] in active else 0.0 for i in top]
    feats[0, :, 2] = (np.arange(k, dtype=np.float32) / max(clf._k - 1, 1))[::-1]
    skill_logits, _docs, card_logits = clf._head.run(
        None,
        {
            "query": q_rank[None, :].astype(np.float32),
            "candidates": cand,
            "features": feats,
            "mask": np.ones((1, k), dtype=np.float32),
        },
    )
    probs = 1.0 / (1.0 + np.exp(-skill_logits[0]))
    ranked = [
        (table.ids[int(top[int(j)])], float(probs[int(j)]))
        for j in np.argsort(-probs)
    ]
    return admit(ranked, active, int(np.argmax(card_logits[0])), 3)


if __name__ == "__main__":
    raise SystemExit(main())
