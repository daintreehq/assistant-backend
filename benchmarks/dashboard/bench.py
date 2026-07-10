"""One command that runs the whole benchmark portfolio and emits one JSON report.

    python -m benchmarks.dashboard.bench run                      # det + live if key present
    python -m benchmarks.dashboard.bench run --deterministic-only # free, CI-able (no key)
    python -m benchmarks.dashboard.bench run --live               # force live suites (paid)
    python -m benchmarks.dashboard.bench report results/<f>.json
    python -m benchmarks.dashboard.bench compare results/a.json results/b.json

The deterministic-only mode is the one worth wiring into CI: free, no key, and it
gates every prompt/skill/assembler edit on the cache-stability, safety, micro-perf,
and orchestrator-correctness numbers.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .._harness.results import load_doc, save_doc
from .aggregate import run_dashboard


def _flat(d: dict[str, Any]) -> str:
    return "  ".join(f"{k}={v}" for k, v in d.items())


def _print(doc: dict[str, Any]) -> None:
    print(f"\n== benchmark dashboard | {doc['meta']['mode']} ==")
    if doc["meta"].get("live_note"):
        print(f"note: {doc['meta']['live_note']}")
    for name, block in doc["suites"].items():
        if not block.get("ok"):
            print(f"  [FAIL] {name:16} {block.get('error')}")
            continue
        tag = "ok  " if block.get("passed", True) else "FAIL"
        print(f"  [{tag}] {name:16} {_flat(block.get('headline', {}))}")


def _compare(a: dict[str, Any], b: dict[str, Any]) -> None:
    print("\n== dashboard compare (headlines) ==")
    names = sorted(set(a["suites"]) | set(b["suites"]))
    for name in names:
        ha = a["suites"].get(name, {}).get("headline", {})
        hb = b["suites"].get(name, {}).get("headline", {})
        keys = sorted(set(ha) | set(hb))
        for k in keys:
            va, vb = ha.get(k), hb.get(k)
            mark = "" if va == vb else "  <-- changed"
            print(f"  {name}.{k}: {va} -> {vb}{mark}")


def _cmd_run(args: argparse.Namespace) -> int:
    include_live = not args.deterministic_only
    doc = run_dashboard(
        include_live=include_live or args.live,
        env_file=args.env_file,
        live_trials=args.live_trials,
    )
    _print(doc)
    if not args.no_save:
        label = doc["meta"]["mode"].replace("-", "_")
        path = save_doc(doc, __file__, label, Path(args.out) if args.out else None)
        print(f"\nsaved: {path}")
    all_ok = all(b.get("ok") and b.get("passed", True) for b in doc["suites"].values())
    return 0 if all_ok else 1


def _cmd_report(args: argparse.Namespace) -> int:
    _print(load_doc(Path(args.path)))
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    _compare(load_doc(Path(args.a)), load_doc(Path(args.b)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.dashboard.bench")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the portfolio and emit one JSON report")
    run.add_argument("--deterministic-only", action="store_true",
                     help="skip the live (paid) suites — free, CI-able")
    run.add_argument("--live", action="store_true", help="force the live suites (needs a key)")
    run.add_argument("--live-trials", type=int, help="override trials for the live suites")
    run.add_argument("--env-file", help="path to a .env")
    run.add_argument("--out", help="explicit output JSON path")
    run.add_argument("--no-save", action="store_true")
    run.set_defaults(func=_cmd_run)
    report = sub.add_parser("report", help="re-print a saved dashboard file")
    report.add_argument("path")
    report.set_defaults(func=_cmd_report)
    compare = sub.add_parser("compare", help="diff two dashboard headline sets")
    compare.add_argument("a")
    compare.add_argument("b")
    compare.set_defaults(func=_cmd_compare)
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
