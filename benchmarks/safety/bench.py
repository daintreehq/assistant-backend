"""Deterministic safety/correctness gauntlet CLI.

    python -m benchmarks.safety.bench run                  # free, no network
    python -m benchmarks.safety.bench run --no-save
    python -m benchmarks.safety.bench report results/<f>.json

Drives the REAL production guards (request validation, result_schema,
tool-call repair, sanitization, the state-token codec, workflow validation)
over hostile/edge corpora. 100% deterministic — no API key, no model calls,
CI-safe. Exit code 1 when any zero-tolerance counter is non-zero or a check
fails; do NOT "fix" that by weakening a case — a red run is a real guard hole.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .._harness.cases import CaseError
from .._harness.env import EnvError, build_settings
from .._harness.results import load_doc, save_doc
from .reporting import print_failures, print_summary
from .runner import run_suite


def _cmd_run(args: argparse.Namespace) -> int:
    settings = build_settings(args.env_file)
    doc = run_suite(settings)
    print_summary(doc)
    print_failures(doc)
    if not args.no_save:
        path = save_doc(doc, __file__, "deterministic",
                        Path(args.out) if args.out else None)
        print(f"\nsaved: {path}")
    return 0 if doc["summary"]["all_pass"] else 1


def _cmd_report(args: argparse.Namespace) -> int:
    doc = load_doc(Path(args.path))
    print_summary(doc)
    print_failures(doc)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.safety.bench")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the deterministic safety gauntlet")
    run.add_argument("--env-file", help="path to a .env (defaults to the repo root .env)")
    run.add_argument("--out", help="explicit output JSON path")
    run.add_argument("--no-save", action="store_true", help="do not write a results file")
    run.set_defaults(func=_cmd_run)

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
