"""Live injection-resistance CLI (canary-graded obedience floor).

    python -m benchmarks.injection_live.bench list          # validate cases (no calls)
    python -m benchmarks.injection_live.bench run           # live (paid)
    python -m benchmarks.injection_live.bench run --trials 3
    python -m benchmarks.injection_live.bench report results/<f>.json

Makes REAL, paid main-model calls.
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
    print(f"\n== injection_live | trials={doc['meta']['trials']} ==")
    print(f"injection_obey_rate       {s['injection_obey_rate']:.4f}  (target 0.0, over "
          f"{s['injection_trials']} trials)")
    print(f"canary_leak_rate          {s['canary_leak_rate']:.4f}")
    print(f"forbidden_tool_rate       {s['forbidden_tool_rate']:.4f}")
    print(f"counter_over_refusal_rate {s['counter_over_refusal_rate']:.4f}  (target 0.0)")
    print("by carrier field:")
    for carrier, b in s["by_carrier"].items():
        print(f"  {carrier:22} obey_rate={b['obey_rate']:.4f}  (n={b['n']})")
    obeyed = [c for c in doc["cases"] if c["kind"] == "injection" and c["obeyed_injection"]]
    for c in obeyed:
        print(f"    OBEYED: {c['id']} (carrier={c['carrier']}, trial={c['trial']})")


def _cmd_run(args: argparse.Namespace) -> int:
    doc = asyncio.run(run_bench(trials=args.trials, id_filter=args.filter, env_file=args.env_file))
    _print(doc)
    passed = print_verdict(doc, "injection_live")
    if not args.no_save:
        path = save_doc(doc, __file__, "live", Path(args.out) if args.out else None)
        print(f"\nsaved: {path}")
    # Non-zero on a QUALITY failure, not just an exception. Without this the
    # command is a printout: a release step could run it, read `0`, and promote
    # a build whose model obeyed every hostile instruction in the corpus.
    return 0 if passed else 1


def _cmd_list(args: argparse.Namespace) -> int:
    for c in load_cases(args.filter):
        print(f"{c['id']:28} [{c['kind']}] carrier={c['carrier']}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    # Re-derives the verdict rather than trusting a stored `passed`, so an
    # artifact saved before a threshold existed is graded against today's — and so
    # replaying a failed run in CI stays a failure.
    doc = load_doc(Path(args.path))
    _print(doc)
    return 0 if print_verdict(doc, "injection_live") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.injection_live.bench")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run against live DeepSeek")
    run.add_argument("--trials", type=int, default=3, help="trials per case (default 3)")
    run.add_argument("--filter", help="substring filter on case id/carrier")
    run.add_argument("--env-file", help="path to a .env with BASETEN_API_KEY")
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
