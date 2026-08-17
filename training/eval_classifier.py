"""The canonical evaluation: the SHIPPED artifact, under the SHIPPED policy.

This drives ``SkillClassifier`` loaded from the exported ONNX directory — not a
torch checkpoint, not a fusion path, not a different admission rule — and applies
the same decision procedure ``LocalSelector`` runs in production (visible-active
projection, background-wake rule, earn-your-place admission inside the
classifier). Anything else measures a system that does not ship; an earlier
evaluator did exactly that and would have let a broken int8 export pass the
documented workflow untested.

Reports both precisions and their decision diff, because int8 is what ships and
"the logits moved a little" is not the question — "did any DECISION change" is.
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
from daintree_assistant_server.skills.local.classifier import SkillClassifier  # noqa: E402
from daintree_assistant_server.skills.local.documents import (  # noqa: E402
    query_document,
    routing_revision,
)

ARTIFACT = (
    ROOT / "src" / "daintree_assistant_server" / "skills" / "local"
    / "artifacts" / "classifier"
)


def run(clf: SkillClassifier, catalog, cases):  # type: ignore[no-untyped-def]
    """Score one graph under the serving decision procedure."""
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
            sel = clf.select(query, visible)
            picked = [sid for sid in sel.skill_ids if sid in selectable]
            docs_q = "q" if sel.docs_wanted else None
        latencies.append((time.perf_counter() - started) * 1000)
        picked_all.append(picked)
        results.append(score_trial(
            case, 0, picked, degraded=False, latency_s=0.0, confidence=None,
            task_type=None, reason=None, prompt_tokens=0, completion_tokens=0,
            docs_query=docs_q,
        ))
    return summarize(cases, results), results, picked_all, latencies


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", default=str(ARTIFACT))
    args = ap.parse_args()

    catalog = load_catalog(default_skills_dir())
    cases = load_cases(catalog)
    artifact = Path(args.artifact)

    stamped = json.loads((artifact / "meta.json").read_text()).get("routing_revision", "")
    live = routing_revision(catalog)
    match = "MATCH" if stamped == live else "STALE — serving would degrade open"
    print(f"routing revision: artifact {stamped[:28]}  live {live[:28]}  [{match}]")

    outputs = {}
    for quantized in (False, True):
        tag = "int8" if quantized else "fp32"
        clf = SkillClassifier(artifact, quantized=quantized)
        summary, results, picked, lat = run(clf, catalog, cases)
        outputs[tag] = (summary, results, picked, lat)
        lat.sort()
        print(f"\n{tag}: {summary['cases_passed']}/{summary['cases_total']} "
              f"({summary['cases_passed'] / summary['cases_total'] * 100:.1f}%)  "
              f"miss {summary['recall_miss_trials']} extra {summary['precision_extra_trials']}  "
              f"docs {summary['docs']['correct_trials']}/{summary['docs']['scored_trials']}  "
              f"p50 {lat[len(lat) // 2]:.1f} ms  mean {statistics.mean(lat):.1f} ms")

    diffs = [
        (c.id, a, b)
        for c, a, b in zip(cases, outputs["fp32"][2], outputs["int8"][2], strict=True)
        if a != b
    ]
    print(f"\nfp32 vs int8 decision diff: {len(diffs)} of {len(cases)} cases")
    for cid, a, b in diffs[:10]:
        print(f"  {cid}: fp32={[x.rsplit('.', 1)[-1] for x in a]} "
              f"int8={[x.rsplit('.', 1)[-1] for x in b]}")

    print("\nby category (int8, the shipped precision):")
    by: dict[str, list] = defaultdict(list)
    for c, r in zip(cases, outputs["int8"][1], strict=True):
        by[c.category].append(r)
    for cat in sorted(by):
        rs = by[cat]
        print(f"  {cat:22} {sum(r.passed for r in rs):3d}/{len(rs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
