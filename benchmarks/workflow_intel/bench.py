"""Workflow-intelligence live benchmark CLI.

    python -m benchmarks.workflow_intel.bench list           # validate cases (no calls)
    python -m benchmarks.workflow_intel.bench run            # live (paid)
    python -m benchmarks.workflow_intel.bench run --trials 3
    python -m benchmarks.workflow_intel.bench report results/<f>.json

Makes REAL, paid DeepSeek calls.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from .._harness.cases import CaseError
from .._harness.env import EnvError
from .._harness.quality import print_verdict
from .._harness.results import load_doc, save_doc
from .runner import load_cases, run_bench


def _print(doc: dict[str, Any]) -> None:
    s = doc["summary"]
    print(f"\n== workflow_intel (live) | trials={doc['meta']['trials']} ==")
    print(f"cases {s['cases_total']}   first_try_valid {s['first_try_valid_rate']:.3f}   "
          f"repair {s['repair_rate']:.3f}   error(502) {s['error_rate']:.3f}")
    lat = s["latency_s"]
    print(f"latency p50 {lat['p50_s']:.2f}s  p90 {lat['p90_s']:.2f}s")
    for task, b in s["by_task"].items():
        extra = f"  focus_acc={b['focus_accuracy']:.3f}" if "focus_accuracy" in b else ""
        print(f"  {task:26} n={b['n']}  first_try={b['first_try_valid_rate']:.3f}  "
              f"repair={b['repair_rate']:.3f}  err={b['error_rate']:.3f}{extra}")
    for c in doc["cases"]:
        if not c["ok"] or c["repaired"]:
            print(f"    - {c['id']:34} ok={c['ok']} calls={c['model_calls']} "
                  f"{c['error'] or ''}")


def _cmd_run(args: argparse.Namespace) -> int:
    doc = asyncio.run(run_bench(trials=args.trials, id_filter=args.filter, env_file=args.env_file))
    _print(doc)
    passed = print_verdict(doc, "workflow_intel")
    if not args.no_save:
        path = save_doc(doc, __file__, "live", Path(args.out) if args.out else None)
        print(f"\nsaved: {path}")
    # Non-zero on a QUALITY failure, not just an exception. Without this the
    # command is a printout: a release step could run it, read `0`, and promote
    # plans the client cannot apply, or a next_action naming
    # a tool that does not exist.
    return 0 if passed else 1


def _cmd_list(args: argparse.Namespace) -> int:
    for c in load_cases(args.filter):
        print(f"{c['id']:34} [{c.get('category','')}] -> {c['task']}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    # Re-derives the verdict rather than trusting a stored `passed`, so an
    # artifact saved before a threshold existed is graded against today's — and so
    # replaying a failed run in CI stays a failure.
    doc = load_doc(Path(args.path))
    _print(doc)
    return 0 if print_verdict(doc, "workflow_intel") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.workflow_intel.bench")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run against live DeepSeek")
    run.add_argument("--trials", type=int, default=1, help="trials per case (default 1)")
    run.add_argument("--filter", help="substring filter on case id/category")
    run.add_argument("--env-file", help="path to a .env with TOGETHER_API_KEY")
    run.add_argument("--out", help="explicit output JSON path")
    run.add_argument("--no-save", action="store_true")
    run.set_defaults(func=_cmd_run)
    lst = sub.add_parser("list", help="validate cases (no API calls)")
    lst.add_argument("--filter")
    lst.set_defaults(func=_cmd_list)
    report = sub.add_parser("report", help="re-print a saved results file")
    report.add_argument("path")
    report.set_defaults(func=_cmd_report)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (CaseError, EnvError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
