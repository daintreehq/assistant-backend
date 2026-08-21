"""Utility-task accuracy benchmark CLI.

Run from the repo root (venv active, CEREBRAS_API_KEY in .env or the env):

    python -m benchmarks.utility_tasks.bench run                    # baseline prompts
    python -m benchmarks.utility_tasks.bench run --variant v2-foo   # a prompts/ overlay
    python -m benchmarks.utility_tasks.bench run --variant baseline --variant v2-foo
    python -m benchmarks.utility_tasks.bench run --filter terminal_judge
    python -m benchmarks.utility_tasks.bench list
    python -m benchmarks.utility_tasks.bench report results/<run>.json
    python -m benchmarks.utility_tasks.bench compare results/a.json results/b.json

Makes REAL, paid DeepSeek API calls (cases x trials per variant).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .cases import CaseError, load_cases
from .reporting import load_doc, print_compare, print_failures, print_summary, save_doc
from .runner import BenchConfig, BenchError, available_variants, run_bench


def _cmd_run(args: argparse.Namespace) -> int:
    variants = args.variant or ["baseline"]
    docs = []
    for variant in variants:
        config = BenchConfig(
            variant=variant,
            trials=args.trials,
            concurrency=args.concurrency,
            id_filter=args.filter,
            env_file=args.env_file,
        )
        doc = asyncio.run(run_bench(config))
        print_summary(doc)
        print_failures(doc)
        if not args.no_save:
            path = save_doc(doc, Path(args.out) if args.out and len(variants) == 1 else None)
            print(f"\nsaved: {path}")
        docs.append(doc)
    if len(docs) == 2:
        print_compare(docs[0], docs[1])
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    cases = load_cases(id_filter=args.filter)
    for case in cases:
        print(f"{case.id:<40} [{case.task}/{case.category}] -> {case.expect}")
    print(f"\n{len(cases)} cases")
    variants = available_variants()
    print(f"prompt variants: baseline (production){', ' if variants else ''}{', '.join(variants)}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    doc = load_doc(Path(args.path))
    print_summary(doc)
    print_failures(doc)
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    print_compare(load_doc(Path(args.a)), load_doc(Path(args.b)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.utility_tasks.bench")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the benchmark against live DeepSeek")
    run.add_argument("--variant", action="append",
                     help="prompt variant (repeatable; default: baseline)")
    run.add_argument("--trials", type=int, default=3, help="trials per case (default 3)")
    run.add_argument("--concurrency", type=int, default=6, help="parallel calls (default 6)")
    run.add_argument("--filter", help="substring filter on case id/category/task")
    run.add_argument("--env-file", help="path to a .env with CEREBRAS_API_KEY")
    run.add_argument("--out", help="explicit output JSON path (single variant only)")
    run.add_argument("--no-save", action="store_true", help="do not write a results file")
    run.set_defaults(func=_cmd_run)

    lst = sub.add_parser("list", help="list/validate cases and variants (no API calls)")
    lst.add_argument("--filter", help="substring filter on case id/category/task")
    lst.set_defaults(func=_cmd_list)

    report = sub.add_parser("report", help="re-print a saved results file")
    report.add_argument("path")
    report.set_defaults(func=_cmd_report)

    compare = sub.add_parser("compare", help="compare two saved results files")
    compare.add_argument("a")
    compare.add_argument("b")
    compare.set_defaults(func=_cmd_compare)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (BenchError, CaseError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
