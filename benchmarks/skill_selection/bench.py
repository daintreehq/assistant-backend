"""Skill-selection benchmark CLI.

Run from the repo root (venv active, TOGETHER_API_KEY in .env or the env):

    python -m benchmarks.skill_selection.bench local                  # SHIPPED selector, free
    python -m benchmarks.skill_selection.bench scale                  # recall at 200/500/1000
    python -m benchmarks.skill_selection.bench run                    # baseline prompt
    python -m benchmarks.skill_selection.bench run --variant v2-foo   # a prompts/ variant
    python -m benchmarks.skill_selection.bench run --variant baseline --variant v2-foo
    python -m benchmarks.skill_selection.bench list
    python -m benchmarks.skill_selection.bench coverage
    python -m benchmarks.skill_selection.bench report results/<run>.json
    python -m benchmarks.skill_selection.bench compare results/a.json results/b.json

``local`` is the one that qualifies a release: it drives the shipped ONNX
artifact — the selector that actually serves — offline, deterministically, for
free, and reports WHICH STAGE lost each missing skill. ``run`` drives the
prompt-based selector live against a paid API; that selector has not been on the
serving path since selection moved local, so it is a comparator, not the
authority.

Only ``run`` makes API calls (REAL, paid: cases x trials per variant). ``local``,
``list``, ``coverage``, ``report``, and ``compare`` are offline and free — they
need no key, so they work with ``TOGETHER_API_KEY=`` blanked.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .._harness.quality import print_verdict
from .cases import CaseError, load_cases
from .coverage import print_coverage
from .reporting import load_doc, print_compare, print_failures, print_summary, save_doc


def _cmd_local(args: argparse.Namespace) -> int:
    from daintree_assistant_server.skills.catalog import load_catalog

    from .local_runner import print_categories, print_local, run_local

    # The SAME free preflight the dashboard runs before this suite, and it
    # belongs on both paths for the same reason: this command is advertised as
    # the release gate, and it grades the cases that exist. A skill nobody wrote
    # a case for is invisible to it by construction, so a catalog with a coverage
    # hole could be qualified by a run that never mentioned the gap. Skipped
    # under --filter, which is already refused by the verdict.
    catalog = load_catalog()
    cases = load_cases(catalog, id_filter=args.filter)
    if not args.filter:
        rows = print_coverage(catalog, cases)
        if not rows or not all(r.gate_ok for r in rows):
            gaps = [r.skill_id for r in rows if not r.gate_ok]
            print(
                "\nrouting coverage has gaps, so this run cannot qualify the "
                f"catalog: {', '.join(gaps) or '<no skills>'}"
            )
            return 1

    doc = run_local(
        catalog=catalog, cases=cases, quantized=not args.fp32, id_filter=args.filter
    )
    print_local(doc)
    print_categories(doc)
    if not args.no_save:
        path = save_doc(doc, Path(args.out) if args.out else None)
        print(f"\nsaved: {path}")
    return 0 if print_verdict(doc, "skill_selection_local") else 1


def _cmd_scale(args: argparse.Namespace) -> int:
    from .scale import print_sweep, sweep

    sizes = tuple(int(x) for x in args.sizes.split(",")) if args.sizes else None
    ks = tuple(int(x) for x in args.ks.split(",")) if args.ks else None
    doc = sweep(**{k: v for k, v in (("sizes", sizes), ("ks", ks)) if v})
    print_sweep(doc)
    if not args.no_save:
        print(f"\nsaved: {save_doc(doc, Path(args.out) if args.out else None)}")
    return 0


def _cmd_lint(args: argparse.Namespace) -> int:
    from .catalog_lint import confusion_pairs, print_lint

    print_lint(confusion_pairs(), top=args.top)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    from daintree_assistant_server.skills.catalog import load_catalog

    cases = load_cases(load_catalog(), id_filter=args.filter)
    for case in cases:
        exp = case.required or ["<empty>"]
        print(f"{case.id:<38} [{case.category}] -> {', '.join(exp)}")
    print(f"\n{len(cases)} cases")
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
    # Both suites write into the same results directory, and they are graded by
    # different gates. Read which one produced the file rather than assuming:
    # regrading a local artifact against the paid selector's thresholds is how a
    # healthy run gets reported as a failure, and the reverse is worse.
    suite = str((doc.get("meta") or {}).get("benchmark") or "skill_selection")
    if suite not in ("skill_selection", "skill_selection_local"):
        # `print_verdict` returns True for a suite it does not know, so an
        # artifact carrying an unrecognised `meta.benchmark` would report a pass
        # having been graded by nothing at all.
        print(f"unrecognised benchmark {suite!r} — this CLI cannot grade it")
        return 1
    if suite == "skill_selection_local":
        from .local_runner import print_categories, print_local

        print_local(doc)
        print_categories(doc)
    else:
        print_summary(doc)
        print_failures(doc)
    return 0 if print_verdict(doc, suite) else 1


def _cmd_compare(args: argparse.Namespace) -> int:
    print_compare(load_doc(Path(args.a)), load_doc(Path(args.b)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.skill_selection.bench")
    sub = parser.add_subparsers(dest="command", required=True)

    local = sub.add_parser(
        "local",
        help="score the SHIPPED ONNX selector offline (free, deterministic) — the "
        "release authority, with per-stage loss attribution and latency spans",
    )
    local.add_argument("--filter", help="substring filter on case id/category")
    local.add_argument(
        "--fp32",
        action="store_true",
        help="score the fp32 graphs instead of the int8 ones that ship",
    )
    local.add_argument("--out", help="explicit output JSON path")
    local.add_argument("--no-save", action="store_true", help="do not write a results file")
    local.set_defaults(func=_cmd_local)


    scale = sub.add_parser(
        "scale",
        help="oracle recall at 100/200/500/1000 skills (synthetic distractors, free). "
        "Answers what k the shortlist needs before the catalog needs it",
    )
    scale.add_argument(
        "--sizes", help="comma-separated catalog sizes (default 39,100,200,500,1000)"
    )
    scale.add_argument("--ks", help="comma-separated shortlist widths (default 16,32,48,64,96)")
    scale.add_argument("--out", help="explicit output JSON path")
    scale.add_argument("--no-save", action="store_true")
    scale.set_defaults(func=_cmd_scale)

    lint = sub.add_parser(
        "lint",
        help="rank skill pairs by how confusable the retriever finds them, and "
        "flag the ones the catalog declares no carve-out for (no API calls)",
    )
    lint.add_argument("--top", type=int, default=20, help="rows to print (default 20)")
    lint.set_defaults(func=_cmd_lint)

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
    except CaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
