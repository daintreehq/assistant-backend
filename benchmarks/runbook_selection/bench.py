"""Runbook-selection benchmark CLI. Offline and free: no API calls.

Run from the repo root:

    python -m benchmarks.runbook_selection.bench local       # the SHIPPED selector
    python -m benchmarks.runbook_selection.bench search "…"  # one query, as the MCP ranks it
    python -m benchmarks.runbook_selection.bench scale       # recall at 200/500/1000
    python -m benchmarks.runbook_selection.bench lint        # most-confusable runbook pairs
    python -m benchmarks.runbook_selection.bench list
    python -m benchmarks.runbook_selection.bench coverage
    python -m benchmarks.runbook_selection.bench pairs
    python -m benchmarks.runbook_selection.bench report results/<run>.json
    python -m benchmarks.runbook_selection.bench compare results/a.json results/b.json

``local`` is the release gate: it drives the shipped ONNX artifact — the
selector the search serves — deterministically, and reports WHICH STAGE lost
each missing runbook.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .cases import CaseError, load_cases
from .coverage import print_coverage
from .quality import print_verdict
from .reporting import load_doc, print_compare, save_doc


def _cmd_local(args: argparse.Namespace) -> int:
    from daintree_assistant_server.runbooks.catalog import load_catalog

    from .local_runner import print_categories, print_local, run_local

    # The free coverage preflight: this command is the release gate, and it
    # grades the cases that exist. A runbook nobody wrote
    # a case for is invisible to it by construction, so a catalog with a coverage
    # hole could be qualified by a run that never mentioned the gap. Skipped
    # under --filter, which is already refused by the verdict.
    catalog = load_catalog()
    cases = load_cases(catalog, id_filter=args.filter)
    if not args.filter:
        rows = print_coverage(catalog, cases)
        if not rows or not all(r.gate_ok for r in rows):
            gaps = [r.runbook_id for r in rows if not r.gate_ok]
            print(
                "\nrouting coverage has gaps, so this run cannot qualify the "
                f"catalog: {', '.join(gaps) or '<no runbooks>'}"
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
    return 0 if print_verdict(doc, "runbook_selection_local") else 1


def _cmd_search(args: argparse.Namespace) -> int:
    from daintree_assistant_server.runbooks.host_state import HostState

    from .search import print_search, read_queries, request_errors, search

    queries = list(args.queries) + (read_queries(Path(args.file)) if args.file else [])
    errors = request_errors(queries, args.max_results) if queries else ["give a query, or --file"]
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 2
    if args.lane and args.hosts is None:
        print("error: --lane is part of a reported host state; give --hosts too", file=sys.stderr)
        return 2
    host_state = (
        HostState(hosts=args.hosts, lane_reachable=args.lane) if args.hosts is not None else None
    )
    catalog, results, shrunk = search(queries, limit=args.max_results, host_state=host_state)
    print_search(catalog, queries, results, shrunk)
    return 0 if all(hits is not None for hits in results) else 1


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
    from daintree_assistant_server.runbooks.catalog import load_catalog

    cases = load_cases(load_catalog(), id_filter=args.filter)
    for case in cases:
        exp = case.required or ["<empty>"]
        print(f"{case.id:<38} [{case.category}] -> {', '.join(exp)}")
    print(f"\n{len(cases)} cases")
    return 0


def _cmd_coverage(args: argparse.Namespace) -> int:
    """Per-runbook case-coverage matrix. Exits 1 on a recall gap or a routing gap."""
    from daintree_assistant_server.runbooks.catalog import load_catalog

    catalog = load_catalog()
    rows = print_coverage(catalog, load_cases(catalog))
    # `rows and` matters: an empty catalog makes `all(...)` vacuously true, which
    # would turn the gate into a silent pass in exactly the situation it exists for.
    return 0 if rows and all(r.gate_ok for r in rows) else 1


def _cmd_pairs(args: argparse.Namespace) -> int:
    """The paired hub/lane scenarios, run on the shipped selector and reported by end.

    A report, not a gate: ``bench local`` scores these cases with all the others and
    is the verdict. This says, per scenario, which end missed, where it lost the
    runbook, and what each caller would actually have been handed. Exit 1 when a pair
    case fails, so a retrain-owed miss is loud rather than lost in the full run.
    """
    from daintree_assistant_server.runbooks.catalog import load_catalog

    from .local_runner import run_local
    from .pairs import pair_cases, print_pairs

    catalog = load_catalog()
    cases = pair_cases(load_cases(catalog))
    if not cases:
        print("no paired role cases", file=sys.stderr)
        return 2
    doc = run_local(catalog=catalog, cases=cases, quantized=not args.fp32)
    return 1 if print_pairs(doc, cases) else 0


def _cmd_report(args: argparse.Namespace) -> int:
    # Re-derives the verdict rather than trusting a stored `passed`, so an
    # artifact saved before a threshold existed is graded against today's — and so
    # replaying a failed run in CI stays a failure.
    doc = load_doc(Path(args.path))
    suite = str((doc.get("meta") or {}).get("benchmark") or "")
    if suite != "runbook_selection_local":
        # `print_verdict` returns True for a suite it does not know, so an
        # artifact from another suite would report a pass graded by nothing.
        print(f"unrecognised benchmark {suite!r} — this CLI cannot grade it")
        return 1
    from .local_runner import print_categories, print_local

    print_local(doc)
    print_categories(doc)
    return 0 if print_verdict(doc, suite) else 1


def _cmd_compare(args: argparse.Namespace) -> int:
    a, b = load_doc(Path(args.a)), load_doc(Path(args.b))
    suites = {str((doc.get("meta") or {}).get("benchmark") or "") for doc in (a, b)}
    if suites == {"runbook_selection_local"}:
        from .local_runner import print_local_compare

        try:
            print_local_compare(a, b)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
    if "runbook_selection_local" in suites:
        print("error: compare two bench local runs, or two of the older format", file=sys.stderr)
        return 2
    print_compare(a, b)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.runbook_selection.bench")
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

    search = sub.add_parser(
        "search",
        help="rank queries exactly as search_runbooks does, over this tree's catalog "
        "and the shipped artifact (free): score, and which runbooks are selected",
    )
    search.add_argument("queries", nargs="*", help="each one a separate search")
    search.add_argument("--file", help="more queries, one per line (# comments skipped)")
    search.add_argument(
        "--max-results", type=int, default=5, help="hits per query (default 5, as the tool)"
    )
    search.add_argument(
        "--hosts",
        choices=("none", "configured", "connected"),
        help="rank for a caller reporting this host state, as Daintree-Hosts does "
        "(default: unreported, the whole catalog)",
    )
    search.add_argument(
        "--lane",
        action="store_true",
        help="with --hosts: the caller is reachable by another machine's assistant",
    )
    search.set_defaults(func=_cmd_search)

    scale = sub.add_parser(
        "scale",
        help="oracle recall at 100/200/500/1000 runbooks (synthetic distractors, free). "
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
        help="rank runbook pairs by how confusable the retriever finds them, and "
        "flag the ones the catalog declares no carve-out for (no API calls)",
    )
    lint.add_argument("--top", type=int, default=20, help="rows to print (default 20)")
    lint.set_defaults(func=_cmd_lint)

    lst = sub.add_parser("list", help="list/validate cases and variants (no API calls)")
    lst.add_argument("--filter", help="substring filter on case id/category")
    lst.set_defaults(func=_cmd_list)

    cov = sub.add_parser(
        "coverage",
        help="per-runbook case-coverage matrix; exit 1 if any runbook has no required case, or "
        "declares confusable neighbours with no routing trap (no API calls)",
    )
    cov.set_defaults(func=_cmd_coverage)

    pairs = sub.add_parser(
        "pairs",
        help="the paired hub/lane scenarios on the shipped selector, per end: what was "
        "loaded, where a miss was lost, and any wrong-role load; exit 1 on a failure (free)",
    )
    pairs.add_argument("--fp32", action="store_true", help="score the fp32 graphs")
    pairs.set_defaults(func=_cmd_pairs)

    report = sub.add_parser("report", help="re-print a saved results file")
    report.add_argument("path")
    report.set_defaults(func=_cmd_report)

    compare = sub.add_parser(
        "compare",
        help="compare two saved results files; two bench local runs are diffed case by case",
    )
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
