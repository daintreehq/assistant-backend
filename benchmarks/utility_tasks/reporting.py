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
        out = RESULTS_DIR / f"{stamp}-{doc['meta']['variant']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return out


def load_doc(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _pct(num: int, den: int) -> str:
    return f"{100.0 * num / den:.1f}%" if den else "n/a"


def _group_lines(title: str, groups: dict[str, dict[str, int]]) -> None:
    print(f"{title}:")
    for name, g in sorted(groups.items()):
        err = f"  errors={g['errors']}" if g["errors"] else ""
        print(
            f"  {name:<24} {g['trials_passed']}/{g['trials']} trials "
            f"({_pct(g['trials_passed'], g['trials'])}){err}"
        )


def print_summary(doc: dict[str, Any]) -> None:
    meta, s = doc["meta"], doc["summary"]
    lat, tok = s["latency_s"], s["tokens"]
    print(
        f"\n== utility_tasks | variant={meta['variant']} model={meta['model']} "
        f"trials={meta['trials']} =="
    )
    print(
        f"cases   {s['cases_passed']}/{s['cases_total']} strict "
        f"({_pct(s['cases_passed'], s['cases_total'])})   "
        f"trials {s['trials_passed']}/{s['trials_total']} "
        f"({_pct(s['trials_passed'], s['trials_total'])})   "
        f"errors={s['error_trials']}"
    )
    print(
        f"latency mean={lat['mean']}s p50={lat['p50']}s p90={lat['p90']}s "
        f"p99={lat['p99']}s max={lat['max']}s"
    )
    print(f"tokens  prompt mean={tok['prompt_mean']}  completion mean={tok['completion_mean']}")
    _group_lines("per-task", s["by_task"])
    _group_lines("per-category", s["by_category"])


def print_failures(doc: dict[str, Any], max_reason: int = 160) -> None:
    failing = [c for c in doc["cases"] if c["pass_count"] < len(c["trials"])]
    if not failing:
        print("\nNo failing cases.")
        return
    print(f"\nFailing cases ({len(failing)}):")
    for case in failing:
        n = len(case["trials"])
        print(
            f"\n✗ {case['id']} [{case['task']}/{case['category']}] — "
            f"{case['pass_count']}/{n} trials passed"
        )
        print(f"  expect={case['expect']}")
        if case["note"]:
            print(f"  note: {case['note']}")
        for i, t in enumerate(case["trials"]):
            if t["passed"]:
                continue
            if t["error"]:
                print(f"  trial {i}: ERROR {t['error'][:max_reason]}")
                continue
            print(f"  trial {i}: mismatch={t['mismatch']}")
            reason = t["output"].get("reason") or t["output"].get("summary")
            if reason:
                print(f"    reason: {reason[:max_reason]}")


def _run_label(doc: dict[str, Any]) -> str:
    # variant alone is ambiguous once a winner is promoted (both runs then say
    # "baseline"); the start timestamp identifies the actual run.
    meta = doc["meta"]
    return f"{meta['variant']} @ {meta.get('started_at', '?')}"


def print_compare(a: dict[str, Any], b: dict[str, Any]) -> None:
    sa, sb = a["summary"], b["summary"]
    la, lb = sa["latency_s"], sb["latency_s"]
    print(f"\n== compare: A={_run_label(a)} vs B={_run_label(b)} ==")
    print(
        f"strict cases   A {sa['cases_passed']}/{sa['cases_total']}   "
        f"B {sb['cases_passed']}/{sb['cases_total']}"
    )
    print(
        f"trial pass     A {sa['trials_passed']}/{sa['trials_total']} "
        f"({_pct(sa['trials_passed'], sa['trials_total'])})   "
        f"B {sb['trials_passed']}/{sb['trials_total']} "
        f"({_pct(sb['trials_passed'], sb['trials_total'])})"
    )
    print(f"errors         A {sa['error_trials']}   B {sb['error_trials']}")
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
