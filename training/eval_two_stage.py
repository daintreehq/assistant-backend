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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from benchmarks.skill_selection.cases import load_cases  # noqa: E402
from benchmarks.skill_selection.local_runner import STAGES, run_local  # noqa: E402
from daintree_assistant_server.skills.catalog import (  # noqa: E402
    default_skills_dir,
    load_catalog,
)
from daintree_assistant_server.skills.local.documents import query_document  # noqa: E402
from daintree_assistant_server.skills.local.setwise import SetwiseClassifier  # noqa: E402

#: Release floor for the product metric (every required skill present). The
#: architecture this replaced measured 158/169; the family's error bar is ±2-3,
#: so the floor sits at the bottom of that band rather than at the peak.
RECALL_PASS_FLOOR = 152

ARTIFACT = (
    ROOT / "src" / "daintree_assistant_server" / "skills" / "local"
    / "artifacts" / "selector"
)


# The scoring loop lives in ``benchmarks/skill_selection/local_runner.py`` and is
# imported rather than reimplemented here. It was duplicated for a while, and the
# duplicate is exactly the kind that rots: two loops applying "the serving
# decision procedure" drift a rule at a time — a projection here, a docs string
# there — and the number a release is qualified on stops matching the number a
# retrain is judged on, with nothing to say which one moved.
#
# What stays here is what this script uniquely does: run the SAME evaluation
# through both precisions and diff the DECISIONS, and probe the head's
# permutation-equivariance against the real graph.


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
        doc = run_local(artifact=artifact, catalog=catalog, cases=cases, quantized=quantized)
        outputs[tag] = doc
        summary = doc["summary"]
        total = summary["spans_ms"]["total_ms"]
        print(
            f"\n{tag}: recall-pass {summary['recall_pass_cases']}/{summary['cases_total']} "
            f"({summary['recall_pass_cases'] / summary['cases_total'] * 100:.1f}%)  "
            f"strict {summary['cases_passed']}/{summary['cases_total']}  "
            f"miss {summary['recall_miss_trials']} "
            f"extra {summary['precision_extra_trials']}  "
            f"docs {summary['docs']['correct_trials']}"
            f"/{summary['docs']['scored_trials']}  "
            f"p50 {total['p50']:.1f} ms  mean {total['mean']:.1f} ms"
        )
        stages = summary["stages"]
        print(
            "     lost to: "
            + "  ".join(f"{name}={stages[name]}" for name in STAGES if stages[name])
            + (f"  candidate_miss={stages['candidate_miss']}" if not any(stages.values()) else "")
        )

    diffs = [
        (c.id, a, b)
        for c, a, b in zip(
            cases, outputs["fp32"]["picked"], outputs["int8"]["picked"], strict=True
        )
        if a != b
    ]
    print(f"\nfp32 vs int8 decision diff: {len(diffs)} of {len(cases)} cases")
    for cid, a, b in diffs[:8]:
        print(
            f"  {cid}: fp32={[x.rsplit('.', 1)[-1] for x in a]} "
            f"int8={[x.rsplit('.', 1)[-1] for x in b]}"
        )

    int8 = outputs["int8"]
    print("\nby category (int8, recall-pass — the product metric):")
    for cat, block in sorted(int8["summary"]["recall_pass_categories"].items()):
        print(f"  {cat:22} {block['passed']:3d}/{block['cases']}")

    print("\nlosses (int8), by the stage that lost them:")
    for loss in int8["losses"]:
        p = "  —  " if loss["p"] is None else f"{loss['p']:.3f}"
        print(f"  {loss['case']:42} {loss['stage']:18} p={p}  {loss['skill']}")

    if args.ablate:
        _ablate(artifact, catalog, cases, outputs["int8"]["picked"])

    # The release gate, not a second opinion. `run_local` already stamped the
    # full verdict — candidate recall, per-category floors, docs, latency — and
    # ignoring it here would let a retrain pass this script while failing the
    # dashboard on the same numbers, which is the split this whole suite exists
    # to close. The recall-pass floor below stays as an explicit, separately
    # movable line for the training loop.
    ok = bool(int8["summary"].get("passed"))
    if not ok:
        print("\nQUALITY GATE: FAIL")
        for failure in int8["summary"].get("quality_failures", []):
            print(f"  - {failure}")

    int8_rp = int(int8["summary"]["recall_pass_cases"])
    if int8_rp < RECALL_PASS_FLOOR:
        print(f"\nFAIL: int8 recall-pass {int8_rp} < floor {RECALL_PASS_FLOOR}")
        return 1
    return 0 if ok else 1


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
    """Re-run one selection with the ballot handed over in reverse order.

    The BALLOT is the serving one — ``assemble_candidates``, actives seated —
    with only its slot order reversed, features carried along. Rebuilding a
    plain top-k here instead would make the ablation measure a policy the server
    does not run, and would silently drop any reserved candidate from the probe.
    """
    from daintree_assistant_server.skills.local.candidates import assemble_candidates
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
    active = set(active_ids)
    chosen = assemble_candidates(sims, table.ids, active, clf._k)
    k = len(chosen)
    top = chosen.indices[::-1]

    q_rank = _run_encoder(clf._ranker, clf._ranker_tok, [text], normalize=False)[0]
    cand = table.ranking[top][None, :, :].astype(np.float32)
    feats = np.zeros((1, k, len(FEATURE_NAMES)), dtype=np.float32)
    feats[0, :, 0] = chosen.cosine[::-1]
    feats[0, :, 1] = [1.0 if table.ids[i] in active else 0.0 for i in top]
    feats[0, :, 2] = chosen.rank_norm[::-1]
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
