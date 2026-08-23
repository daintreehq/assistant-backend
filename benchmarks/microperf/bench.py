"""Deterministic startup/concurrency/CPU micro-benchmark CLI.

Run from the repo root (venv active; NO API key needed — nothing here talks to
a model, the only "provider" is an in-process fake that answers instantly):

    python -m benchmarks.microperf.bench run
    python -m benchmarks.microperf.bench run --concurrency 64 --iters 50000
    python -m benchmarks.microperf.bench run --no-save
    python -m benchmarks.microperf.bench report results/<run>.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .._harness.results import load_doc, save_doc
from .perf import BenchConfig, PerfError, run_bench


def _fmt_ms(stats: dict[str, Any]) -> str:
    return f"{stats['min_ms']:>8.2f} ms min | {stats['median_ms']:>8.2f} ms median"


def _fmt_level(label: str, block: dict[str, Any]) -> str:
    p50 = block["p50_s"] * 1000
    p99 = block["p99_s"] * 1000
    return f"  {label:<24}p50 {p50:>9.2f} ms   p99 {p99:>9.2f} ms   (n={block['count']})"


def print_summary(doc: dict[str, Any]) -> None:
    meta = doc["meta"]
    print("\nmicroperf — deterministic startup + concurrency + CPU-throughput")
    print(f"  platform: {meta['platform']} (python {meta['python_version']})")
    print(f"  mode: {meta['mode']} — {meta['note']}")

    cold = doc["cold_start"]
    build = doc["build_container"]
    print(f"\ncold start ({cold['repeats']} fresh subprocesses):")
    print(f"  import package          {_fmt_ms(cold['import_package_ms'])}")
    print(f"  import app module       {_fmt_ms(cold['import_app_ms'])}")
    print(f"  import total            {_fmt_ms(cold['import_ms'])}")
    print(f"  build_container         {_fmt_ms(build['build_container_ms'])}  (in-process)")

    cat = doc["catalog"]
    deterministic = "yes" if cat["revision_deterministic"] else "NO — INVESTIGATE"
    print(f"\nrunbook catalog ({cat['runbooks']} runbooks):")
    print(f"  load_catalog            {_fmt_ms(cat['catalog_build_ms'])}")
    print(f"  revision deterministic  {deterministic}")

    codec = doc["state_codec"]
    verified = "" if codec["all_verified"] else "   [SOME TOKENS FAILED VERIFY]"
    print(f"\nstate codec ({codec['iters']} iters, {codec['token_bytes']}-byte token):")
    print(f"  issue                   {codec['issue_ops_per_sec']:>10,} ops/s")
    print(f"  verify                  {codec['verify_ops_per_sec']:>10,} ops/s{verified}")

    conc = doc["concurrency"]
    print("\norchestrator.complete — real path, fake 0-latency provider, non-streaming")
    print("(concurrent latency measured from submission, so it includes queueing):")
    print(_fmt_level("sequential baseline", conc["sequential"]))
    for level in conc["levels"]:
        label = f"k={level['concurrency']} concurrent"
        ratio = level["p99_inflation_ratio"]
        print(f"{_fmt_level(label, level)}   p99/seq-p50 x{ratio}")
    print(f"  provider calls/request  {conc['provider_calls_per_request']} (selector + main)")
    if conc["warning_count"]:
        print(f"  WARNINGS in respond bodies: {conc['warning_count']} (expected 0)")

    ready = doc["readiness"]
    if "error" in ready:
        print(f"\nreadiness: SKIPPED ({ready['error']})")
    else:
        print(
            f"\nreadiness: healthz={ready.get('healthz_status')} "
            f"readyz={ready.get('readyz_status')} "
            f"(runbooks={ready.get('runbooks')})"
        )

    s = doc["summary"]
    print("\nsummary:")
    print(
        f"  import {s['import_ms_median']} ms | build_container "
        f"{s['build_container_ms_median']} ms | catalog {s['catalog_build_ms_median']} ms"
    )
    print(
        f"  codec issue {s['codec_issue_ops_per_sec']:,}/s, "
        f"verify {s['codec_verify_ops_per_sec']:,}/s"
    )
    print(
        f"  single-call p50 {s['single_call_p50_ms']} ms; "
        f"k={meta['concurrency']} p50 {s['concurrent_p50_ms']} ms, "
        f"p99 {s['concurrent_p99_ms']} ms (x{s['p99_inflation_ratio']} of single p50)"
    )


def _cmd_run(args: argparse.Namespace) -> int:
    config = BenchConfig(
        env_file=args.env_file,
        concurrency=args.concurrency,
        iters=args.iters,
    )
    doc = run_bench(config)
    print_summary(doc)
    if not args.no_save:
        path = save_doc(doc, __file__, "deterministic", Path(args.out) if args.out else None)
        print(f"\nsaved: {path}")
    passed = doc["summary"].get("passed", True)
    if not passed:
        print("\nFAIL: readiness/determinism/provider-call guard did not hold "
              "(the perf numbers are not from a full production request)", file=sys.stderr)
    return 0 if passed else 1


def _cmd_report(args: argparse.Namespace) -> int:
    print_summary(load_doc(Path(args.path)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.microperf.bench")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the deterministic suite (no API calls)")
    run.add_argument("--env-file", help="path to a .env (defaults to the repo root .env)")
    run.add_argument(
        "--concurrency",
        type=int,
        default=32,
        help="primary K for the concurrent-orchestrator probe (default 32)",
    )
    run.add_argument(
        "--iters",
        type=int,
        default=20_000,
        help="state-codec issue/verify iterations (default 20000)",
    )
    run.add_argument("--out", help="explicit output JSON path")
    run.add_argument("--no-save", action="store_true", help="do not write a results file")
    run.set_defaults(func=_cmd_run)

    report = sub.add_parser("report", help="re-print a saved results file")
    report.add_argument("path")
    report.set_defaults(func=_cmd_report)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PerfError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
