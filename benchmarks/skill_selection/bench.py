"""Skill-selection benchmark CLI.

Run from the repo root (venv active, CEREBRAS_API_KEY in .env or the env):

    python -m benchmarks.skill_selection.bench run                    # baseline prompt
    python -m benchmarks.skill_selection.bench run --variant v2-foo   # a prompts/ variant
    python -m benchmarks.skill_selection.bench run --variant baseline --variant v2-foo
    python -m benchmarks.skill_selection.bench list
    python -m benchmarks.skill_selection.bench coverage
    python -m benchmarks.skill_selection.bench report results/<run>.json
    python -m benchmarks.skill_selection.bench compare results/a.json results/b.json

Only ``run`` makes API calls (REAL, paid DeepSeek: cases x trials per variant).
``list``, ``coverage``, ``report``, and ``compare`` are offline and free — they
need no key, so they work with ``CEREBRAS_API_KEY=`` blanked.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .._harness.quality import print_verdict
from .cases import CaseError, load_cases
from .coverage import print_coverage
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
            model=args.model,
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
    # Every variant must pass, not just the last one. A --variant A --variant B
    # compare is an experiment, and an experiment whose control regressed is a
    # result to look at rather than a run to wave through.
    #
    # Graded into a list first: `all(...)` over a generator short-circuits, so a
    # failing first variant would suppress the second's verdict — hiding half the
    # output of a run that has already been paid for.
    verdicts = [print_verdict(doc, "skill_selection") for doc in docs]
    passed = all(verdicts)
    # Non-zero on a QUALITY failure, not just an exception. Without this a
    # release step could run the selector corpus, watch cases fail on screen, and
    # still receive a successful process exit — which is what it did.
    return 0 if passed else 1


def _cmd_list(args: argparse.Namespace) -> int:
    from daintree_assistant_server.skills.catalog import load_catalog

    cases = load_cases(load_catalog(), id_filter=args.filter)
    for case in cases:
        exp = case.required or ["<empty>"]
        print(f"{case.id:<38} [{case.category}] -> {', '.join(exp)}")
    print(f"\n{len(cases)} cases")
    variants = available_variants()
    print(f"prompt variants: baseline (production){', ' if variants else ''}{', '.join(variants)}")
    return 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    """Per-skill case-coverage matrix. Exits 1 on a recall gap or a routing gap."""
    from daintree_assistant_server.skills.catalog import load_catalog

    catalog = load_catalog()
    rows = print_coverage(catalog, load_cases(catalog))
    # `rows and` matters: an empty catalog makes `all(...)` vacuously true, which
    # would turn the gate into a silent pass in exactly the situation it exists for.
    return 0 if rows and all(r.gate_ok for r in rows) else 1


def _cmd_report(args: argparse.Namespace) -> int:
    # Re-derives the verdict rather than trusting a stored `passed`, so an
    # artifact saved before a threshold existed is graded against today's — and so
    # replaying a failed run in CI stays a failure.
    doc = load_doc(Path(args.path))
    print_summary(doc)
    print_failures(doc)
    return 0 if print_verdict(doc, "skill_selection") else 1


def _cmd_compare(args: argparse.Namespace) -> int:
    print_compare(load_doc(Path(args.a)), load_doc(Path(args.b)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.skill_selection.bench")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the benchmark against live DeepSeek")
    run.add_argument("--variant", action="append",
                     help="prompt variant (repeatable; default: baseline)")
    run.add_argument("--trials", type=int, default=3, help="trials per case (default 3)")
    run.add_argument("--concurrency", type=int, default=6, help="parallel calls (default 6)")
    run.add_argument("--filter", help="substring filter on case id/category")
    run.add_argument("--env-file", help="path to a .env with CEREBRAS_API_KEY")
    run.add_argument(
        "--model",
        help=(
            "score a CANDIDATE model instead of the pinned one (paid). The pinned id "
            "stays hardcoded in config.py; this only changes what this run sends, and "
            "the result document records what it actually used. Run the same cases on "
            "both and `compare` the two files."
        ),
    )
    run.add_argument("--out", help="explicit output JSON path (single variant only)")
    run.add_argument("--no-save", action="store_true", help="do not write a results file")
    run.set_defaults(func=_cmd_run)

    lst = sub.add_parser("list", help="list/validate cases and variants (no API calls)")
    lst.add_argument("--filter", help="substring filter on case id/category")
    lst.set_defaults(func=_cmd_list)

    cov = sub.add_parser(
        "coverage",
        help="per-skill case-coverage matrix; exit 1 if any skill has no required case, or "
        "declares confusable neighbours with no routing trap (no API calls)",
    )
    cov.set_defaults(func=_cmd_coverage)

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
