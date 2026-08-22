"""CLI for the live latency suite.

    python -m benchmarks.latency.bench run                     # all scenarios
    python -m benchmarks.latency.bench run --trials 7
    python -m benchmarks.latency.bench run --scenario cold_    # id prefix filter
    python -m benchmarks.latency.bench run --label baseline    # names the results file
    python -m benchmarks.latency.bench compare A.json B.json   # baseline vs improved

Makes REAL, paid calls. ``compare`` is offline.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from daintree_assistant_server.modes import STANDARD_MODEL

from .._harness.env import EnvError, require_api_key
from .._harness.results import load_doc, save_doc
from . import runner as R

# The metric each scenario kind is judged on — the number a human should look at
# first for that shape. A streaming turn is judged on first byte because that is
# what the user perceives; everything else has no first byte to speak of.
HEADLINE = {"turn": "ttft_s", "task": "total_s", "selector": "total_s"}


def _headline_key(row: dict[str, Any]) -> str:
    key = HEADLINE.get(row["kind"], "total_s")
    return key if key in row else "total_s"


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}s"


def _print_table(doc: dict[str, Any]) -> None:
    rows = doc["scenarios"]
    print(f"\n{'scenario':<28} {'metric':<8} {'p50':>8} {'p90':>8} {'max':>8}  notes")
    print("-" * 96)
    for row in rows:
        if row.get("failures") and row["failures"] == row["trials"]:
            print(f"{row['id']:<28} {'ALL TRIALS FAILED':<8}")
            continue
        key = _headline_key(row)
        block = row.get(key) or {}
        notes: list[str] = []
        if row.get("repair_rate"):
            notes.append(f"repair {row['repair_rate']:.0%}")
        if row.get("finish_reasons", {}).get("length"):
            notes.append(f"length×{row['finish_reasons']['length']}")
        if row.get("selector_run_rate") is not None:
            notes.append(f"sel {row['selector_run_rate']:.0%}")
        if row.get("cache_hit_ratio_p50") is not None:
            notes.append(f"cache {row['cache_hit_ratio_p50']:.0%}")
        if row.get("endpoints"):
            notes.append("/".join(row["endpoints"]))
        if row.get("failures"):
            notes.append(f"{row['failures']} failed")
        print(
            f"{row['id']:<28} {key.replace('_s',''):<8} "
            f"{_fmt(block.get('p50_s')):>8} {_fmt(block.get('p90_s')):>8} "
            f"{_fmt(block.get('max_s')):>8}  {', '.join(notes)}"
        )
    print()


def _cmd_run(args: argparse.Namespace) -> int:
    settings = R.build_settings_for_bench(args.env_file)
    try:
        require_api_key(settings)
    except EnvError as exc:
        print(f"error: {exc}")
        return 2

    scenarios = R.load_scenarios(args.scenario)
    if not scenarios:
        print("error: no scenarios matched")
        return 2
    print(
        f"latency: {len(scenarios)} scenarios × {args.trials} trials "
        f"(+1 discarded warmup) against {STANDARD_MODEL}"
    )

    started = datetime.now(UTC).isoformat()
    payload = R.run(settings, scenarios, trials=args.trials)
    doc = {
        "meta": {
            "benchmark": "latency",
            "mode": "live",
            "model": STANDARD_MODEL,
            "trials": args.trials,
            "label": args.label,
            "started_at": started,
            "finished_at": datetime.now(UTC).isoformat(),
        },
        **payload,
    }
    _print_table(doc)
    out = save_doc(doc, __file__, args.label)
    print(f"saved: {out}")
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    base = load_doc(Path(args.baseline))
    new = load_doc(Path(args.improved))
    by_id = {r["id"]: r for r in new["scenarios"]}

    print(f"\n{'scenario':<28} {'metric':<7} {'baseline':>10} {'improved':>10} {'change':>10}")
    print("-" * 70)
    for row in base["scenarios"]:
        other = by_id.get(row["id"])
        if other is None:
            continue
        key = _headline_key(row)
        b = (row.get(key) or {}).get("p50_s")
        a = (other.get(key) or {}).get("p50_s")
        if b is None or a is None:
            continue
        # Negative = faster. Reported against the baseline so "-40%" reads as
        # "40% of the baseline latency removed".
        delta = (a - b) / b * 100 if b else 0.0
        print(
            f"{row['id']:<28} {key.replace('_s',''):<7} "
            f"{b:>9.2f}s {a:>9.2f}s {delta:>9.1f}%"
        )
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.latency.bench")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="run the live latency suite")
    run_p.add_argument("--trials", type=int, default=5, help="scored trials (default 5)")
    run_p.add_argument("--scenario", help="only scenarios whose id starts with this")
    run_p.add_argument("--label", default="run", help="results-file label")
    run_p.add_argument("--env-file", help="path to a .env (default: repo root)")
    run_p.set_defaults(func=_cmd_run)

    cmp_p = sub.add_parser("compare", help="compare two results documents")
    cmp_p.add_argument("baseline")
    cmp_p.add_argument("improved")
    cmp_p.set_defaults(func=_cmd_compare)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
