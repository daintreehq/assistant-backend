"""Session-trace correctness CLI (deterministic — no network, no key).

    python -m benchmarks.session_trace.bench run
    python -m benchmarks.session_trace.bench list
    python -m benchmarks.session_trace.bench report results/<f>.json
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from .._harness.cases import CaseError
from .._harness.results import load_doc, save_doc
from .runner import load_traces, run_bench


def _print(doc: dict[str, Any]) -> None:
    s = doc["summary"]
    print("\n== session_trace (deterministic) ==")
    print(f"traces {s['traces_passed']}/{s['traces_total']}   "
          f"turns {s['turns_passed']}/{s['turns_total']}")
    print(f"cadence_accuracy       {s['cadence_accuracy']:.4f}")
    print(f"active_set_accuracy    {s['active_set_accuracy']:.4f}")
    print(f"state_continuity_rate  {s['state_continuity_rate']:.4f}")
    print(f"selector_run_rate      {s['selector_run_rate']:.4f} (runs/turn cost lever)")
    for tr in doc["traces"]:
        flag = "ok " if tr["passed"] else "FAIL"
        print(f"  [{flag}] {tr['id']}")
        for t in tr["turns"]:
            if not t["passed"]:
                fails = [k for k, v in t["checks"].items() if not v]
                print(f"        turn {t['turn_id']}: FAILED {fails}  active={t['active']} "
                      f"selector_ran={t['selector_ran']}")


def _cmd_run(args: argparse.Namespace) -> int:
    doc = asyncio.run(run_bench(id_filter=args.filter, env_file=args.env_file))
    _print(doc)
    if not args.no_save:
        path = save_doc(doc, __file__, "deterministic", Path(args.out) if args.out else None)
        print(f"\nsaved: {path}")
    return 0 if doc["summary"]["turns_passed"] == doc["summary"]["turns_total"] else 1


def _cmd_list(args: argparse.Namespace) -> int:
    for t in load_traces(args.filter):
        print(f"{t['id']:32} {len(t['turns'])} turns  (session {t['session_id']})")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    _print(load_doc(Path(args.path)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.session_trace.bench")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="drive the traces through the real orchestrator")
    run.add_argument("--filter", help="substring filter on trace id")
    run.add_argument("--env-file", help="path to a .env")
    run.add_argument("--out", help="explicit output JSON path")
    run.add_argument("--no-save", action="store_true")
    run.set_defaults(func=_cmd_run)
    lst = sub.add_parser("list", help="list/validate traces (no run)")
    lst.add_argument("--filter")
    lst.set_defaults(func=_cmd_list)
    report = sub.add_parser("report", help="re-print a saved results file")
    report.add_argument("path")
    report.set_defaults(func=_cmd_report)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
