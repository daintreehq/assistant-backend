"""Prompt cost/cache flagship CLI.

    python -m benchmarks.prompt_cost.bench run                 # deterministic (free)
    python -m benchmarks.prompt_cost.bench run --live          # + live cache/TTFB (paid)
    python -m benchmarks.prompt_cost.bench report results/<f>.json

The deterministic half is free and CI-able; ``--live`` adds real, paid DeepSeek
calls that validate the byte-proxy against real ``cached_tokens`` and measure
first-byte latency.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from .._harness.env import EnvError, build_settings, require_deepseek_key
from .._harness.results import load_doc, save_doc
from .deterministic import run_deterministic


def _print(doc: dict[str, Any]) -> None:
    is_det = doc.get("meta", {}).get("mode") == "deterministic"
    det = doc.get("deterministic", doc if is_det else {})
    if det:
        s = det["summary"]
        print("\n== prompt_cost (deterministic) ==")
        print(f"wire encoder matches httpx : {s['wire_encoder_matches_httpx']}")
        print(f"min system+tools stable    : {s['min_system_tools_stable_continuity']:.4f} "
              "(continuation/runtime/startup/new-session — target 1.0)")
        print(f"topic-shift system+tools   : {s['topic_shift_system_tools_stable']} "
              "(expected drop, not a failure)")
        for t in det["transitions"]:
            print(f"  {t['transition']:24} sys+tools={t['system_tools_stable']:.4f}  "
                  f"full={t['full_prefix']:.4f}")
        print("  layer budget (bytes):")
        for L in sorted(det["layer_budget"]["layers"], key=lambda x: -x["bytes"]):
            print(f"    {L['layer']:24} {L['bytes']:>7}B {L['byte_pct']:>5}%  "
                  f"~{L['token_est']} tok")
        sp = det["selector_prompt_cost"]
        print(f"  selector prompt ~{sp['selector_prompt_token_est']} tok / "
              f"{sp['catalog_skills']} skills")
        print(f"  state token bytes: "
              f"{[s['token_bytes'] for s in det['state_token_size']['sizes']]}")
    live = doc.get("live")
    if live:
        s = live["summary"]
        print("\n== prompt_cost (live) ==")
        print(f"warm tool-region cache ratio : {s['warm_tool_region_cache_ratio']:.4f}")
        print(f"streaming TTFB p50           : {s['streaming_ttfb_p50_s']:.3f}s")
        print(f"composed first-byte p50      : {s['composed_first_byte_p50_s']:.3f}s")


def _cmd_run(args: argparse.Namespace) -> int:
    settings = build_settings(args.env_file)
    doc: dict[str, Any] = {
        "meta": {"benchmark": "prompt_cost", "mode": "full" if args.live else "deterministic"},
        "deterministic": run_deterministic(settings),
    }
    if args.live:
        require_deepseek_key(settings)  # fail fast before any work
        from .live import run_live

        doc["live"] = asyncio.run(run_live(settings, trials=args.trials))
    _print(doc)
    if not args.no_save:
        label = "full" if args.live else "deterministic"
        path = save_doc(doc, __file__, label, Path(args.out) if args.out else None)
        print(f"\nsaved: {path}")
    passed = doc["deterministic"]["summary"].get("passed", False)
    if not passed:
        print("\nFAIL: cache-stability floor or wire-encoder check regressed", file=sys.stderr)
    return 0 if passed else 1


def _cmd_report(args: argparse.Namespace) -> int:
    _print(load_doc(Path(args.path)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.prompt_cost.bench")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the cost/cache benchmark")
    run.add_argument("--live", action="store_true", help="add live cache/TTFB (paid)")
    run.add_argument("--trials", type=int, default=3, help="live latency trials (default 3)")
    run.add_argument("--env-file", help="path to a .env with DEEPSEEK_API_KEY")
    run.add_argument("--out", help="explicit output JSON path")
    run.add_argument("--no-save", action="store_true")
    run.set_defaults(func=_cmd_run)
    report = sub.add_parser("report", help="re-print a saved results file")
    report.add_argument("path")
    report.set_defaults(func=_cmd_report)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except EnvError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
