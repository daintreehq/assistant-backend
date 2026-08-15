"""One command that runs the whole benchmark portfolio and emits one JSON report.

    python -m benchmarks.dashboard.bench run                      # det + live if key present
    python -m benchmarks.dashboard.bench run --deterministic-only # free, CI-able (no key)
    python -m benchmarks.dashboard.bench run --live               # force live suites (paid)
    python -m benchmarks.dashboard.bench qualify-release          # paid; a missing key FAILS
    python -m benchmarks.dashboard.bench report results/<f>.json
    python -m benchmarks.dashboard.bench compare results/a.json results/b.json

The deterministic-only mode is the one worth wiring into CI: free, no key, and it
gates every prompt/skill/assembler edit on the cache-stability, safety, micro-perf,
and orchestrator-correctness numbers.

`qualify-release` is the paid counterpart, and differs from `run --live` in one
way that matters: a missing key SKIPS the live suites in the everyday dashboard
(convenient, and correct there) but FAILS here. A release qualification that could
not reach the model has qualified nothing, and recording that as a green run is
the failure mode this whole gate exists to prevent.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .._harness.quality import VERDICTS
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
        # The reasons, not just the verdict. A red suite whose output is one word
        # sends the reader to the JSON; the failing thresholds are the whole point.
        for failure in block.get("summary", {}).get("quality_failures", []):
            print(f"           - {failure}")


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
    require_live = bool(getattr(args, "require_live", False))
    include_live = not args.deterministic_only
    doc = run_dashboard(
        include_live=include_live or args.live or require_live,
        env_file=args.env_file,
        live_trials=args.live_trials,
        require_live=require_live,
    )
    _print(doc)
    if not args.no_save:
        label = doc["meta"]["mode"].replace("-", "_")
        path = save_doc(doc, __file__, label, Path(args.out) if args.out else None)
        print(f"\nsaved: {path}")
    all_ok = all(b.get("ok") and b.get("passed", True) for b in doc["suites"].values())
    return 0 if all_ok else 1


def _regrade(doc: dict[str, Any]) -> dict[str, Any]:
    """Re-derive each live suite's verdict from its stored meta + summary.

    Trusting the artifact's own `passed` would make this command useless for the
    case it exists for: a dashboard archived BEFORE these gates existed carries
    `passed: true` beside an `injection_obey_rate` of 1.0, because the old rule
    only asked whether the benign controls were answered. Replaying it must reach
    today's verdict, not repeat yesterday's.
    """
    for name, block in doc.get("suites", {}).items():
        if name not in VERDICTS or not block.get("ok"):
            continue
        verdict = VERDICTS[name]({"meta": block.get("meta", {}),
                                  "summary": block.get("summary", {})})
        block["passed"] = verdict.passed
        block.setdefault("summary", {})["quality_failures"] = list(verdict.failures)
    return doc


def _cmd_report(args: argparse.Namespace) -> int:
    # Non-zero when replaying a failed artifact, so an archived dashboard is
    # usable as a CI gate rather than only as a printout.
    doc = _regrade(load_doc(Path(args.path)))
    _print(doc)
    # `passed` DEFAULTS TO FALSE for a live suite: a block for a gated suite with
    # no verdict is an ungraded one, and an ungraded security suite is not green.
    all_ok = all(
        b.get("ok") and b.get("passed", name not in VERDICTS)
        for name, b in doc["suites"].items()
    )
    return 0 if all_ok else 1


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
    run.add_argument("--require-live", action="store_true",
                     help="fail (rather than skip) the live suites when no key is present")
    run.set_defaults(func=_cmd_run)
    qualify = sub.add_parser(
        "qualify-release",
        help="paid release qualification: every suite, and a missing key is a FAILURE",
    )
    # Three trials, not the dashboard's default of one. At one trial the 0.95 /
    # 0.98 floors are arithmetically "every case must pass" over a seven- and a
    # five-case corpus; they only become rate targets with a larger denominator.
    # Also the minimum the injection gate accepts at all.
    qualify.add_argument("--live-trials", type=int, default=3,
                         help="trials for the live suites (default 3)")
    qualify.add_argument("--env-file", help="path to a .env")
    qualify.add_argument("--out", help="explicit output JSON path")
    qualify.add_argument("--no-save", action="store_true")
    qualify.set_defaults(func=_cmd_run, deterministic_only=False, live=True, require_live=True)
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
