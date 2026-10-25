"""Human-readable reporting and run persistence/comparison."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def save_doc(doc: dict[str, Any], out: Path | None = None) -> Path:
    if out is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        meta = doc["meta"]
        # Only the retired live bench names a variant. `bench local` is told apart
        # by its precision; `bench scale` has neither, so it falls back to its suite.
        model = f"{meta['host_model']}-" if meta.get("host_model") else ""
        label = meta.get("variant") or (
            f"local-{model}{meta['precision']}"
            if "precision" in meta
            else str(doc.get("suite", "run"))
        )
        out = RESULTS_DIR / f"{stamp}-{label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return out


def load_doc(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _pct(num: int, den: int) -> str:
    return f"{100.0 * num / den:.1f}%" if den else "n/a"


def print_summary(doc: dict[str, Any]) -> None:
    meta, s = doc["meta"], doc["summary"]
    lat, tok = s["latency_s"], s["tokens"]
    print(
        f"\n== runbook_selection | variant={meta['variant']} model={meta['model']} "
        f"trials={meta['trials']} =="
    )
    print(
        f"cases   {s['cases_passed']}/{s['cases_total']} strict "
        f"({_pct(s['cases_passed'], s['cases_total'])})   "
        f"trials {s['trials_passed']}/{s['trials_total']} "
        f"({_pct(s['trials_passed'], s['trials_total'])})   "
        f"degraded={s['degraded_trials']}"
    )
    print(
        f"errors  recall-miss trials={s['recall_miss_trials']} "
        f"(missing runbooks={s['missing_runbooks']})  "
        f"precision-extra trials={s['precision_extra_trials']}"
    )
    docs = s.get("docs") or {}
    if docs.get("scored_trials"):
        # The two counts stay split: a missed lookup answers a product question
        # from memory, a spurious one taxes every operational turn. One accuracy
        # figure would let the two cancel.
        print(
            f"docs    {docs['correct_trials']}/{docs['scored_trials']} trials "
            f"({_pct(docs['correct_trials'], docs['scored_trials'])})   "
            f"missed={docs['missed_lookup_trials']}  "
            f"spurious={docs['spurious_lookup_trials']}"
        )
    print(
        f"latency mean={lat['mean']}s p50={lat['p50']}s p90={lat['p90']}s "
        f"p99={lat['p99']}s max={lat['max']}s"
    )
    print(
        f"tokens  prompt mean={tok['prompt_mean']}  completion mean={tok['completion_mean']}"
    )
    print("per-category:")
    for cat, c in sorted(doc["summary"]["categories"].items()):
        print(
            f"  {cat:<18} {c['passed']}/{c['cases']} cases   "
            f"{c['trials_passed']}/{c['trials']} trials"
        )


def print_failures(doc: dict[str, Any], max_reason: int = 160) -> None:
    # A case fails if EITHER decision was wrong. Routing-only was the old rule,
    # and it let a run print "No failing cases" directly under a summary line
    # reporting docs misses — the one report shape that actively misleads.
    failing = [
        c
        for c in doc["cases"]
        if c["pass_count"] < len(c["trials"])
        or (c["expect"].get("docs") is not None
            and c.get("docs_pass_count", 0) < len(c["trials"]))
    ]
    if not failing:
        print("\nNo failing cases.")
        return
    print(f"\nFailing cases ({len(failing)}):")
    for case in failing:
        n = len(case["trials"])
        print(f"\n✗ {case['id']} [{case['category']}] — {case['pass_count']}/{n} trials passed")
        print(
            f"  active={case['active']} required={case['expect']['required']} "
            f"optional={case['expect']['optional']}"
        )
        if case["expect"].get("docs") is not None:
            print(f"  expect docs={case['expect']['docs']}")
        if case["note"]:
            print(f"  note: {case['note']}")
        for i, t in enumerate(case["trials"]):
            docs_wrong = t.get("docs_ok") is False
            if t["passed"] and not docs_wrong:
                continue
            problems = []
            if t["degraded"]:
                problems.append("DEGRADED")
            if t["missing"]:
                problems.append(f"missing={t['missing']}")
            if t["extra"]:
                problems.append(f"extra={t['extra']}")
            if docs_wrong:
                # Named by direction: a missed lookup and a spurious one are
                # opposite problems with opposite fixes.
                problems.append(
                    f"docs SPURIOUS={t['docs_query']!r}" if t.get("docs_query")
                    else "docs MISSED"
                )
            print(f"  trial {i}: picked={t['picked']}  {' '.join(problems)}")
            if t["reason"]:
                print(f"    reason: {t['reason'][:max_reason]}")


def print_compare(a: dict[str, Any], b: dict[str, Any]) -> None:
    sa, sb = a["summary"], b["summary"]
    la, lb = sa["latency_s"], sb["latency_s"]
    name_a, name_b = a["meta"]["variant"], b["meta"]["variant"]
    print(f"\n== compare: A={name_a} vs B={name_b} ==")
    print(
        f"strict cases   A {sa['cases_passed']}/{sa['cases_total']}   "
        f"B {sb['cases_passed']}/{sb['cases_total']}"
    )
    print(
        f"trial pass     A {sa['trials_passed']}/{sa['trials_total']}   "
        f"B {sb['trials_passed']}/{sb['trials_total']}"
    )
    print(
        f"latency p50    A {la['p50']}s   B {lb['p50']}s      "
        f"p90  A {la['p90']}s   B {lb['p90']}s"
    )
    ca = {c["id"]: c for c in a["cases"]}
    cb = {c["id"]: c for c in b["cases"]}
    fixed, broke = [], []
    for cid in sorted(set(ca) & set(cb)):
        a_ok = ca[cid]["pass_count"] == len(ca[cid]["trials"])
        b_ok = cb[cid]["pass_count"] == len(cb[cid]["trials"])
        if not a_ok and b_ok:
            fixed.append(cid)
        elif a_ok and not b_ok:
            broke.append(cid)
    print(f"fixed in B ({len(fixed)}): {', '.join(fixed) or '—'}")
    print(f"broken in B ({len(broke)}): {', '.join(broke) or '—'}")
    only_a, only_b = sorted(set(ca) - set(cb)), sorted(set(cb) - set(ca))
    if only_a:
        print(f"only in A: {', '.join(only_a)}")
    if only_b:
        print(f"only in B: {', '.join(only_b)}")
