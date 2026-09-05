"""Validate cases, replay the harness, or explicitly run paid qualification.

python -m benchmarks.main_behavior.bench validate
python -m benchmarks.main_behavior.bench replay --mode isolation
python -m benchmarks.main_behavior.bench live --mode end-to-end --filter no-op
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from daintree_assistant_server.config import EXECUTOR_MODEL, Settings
from daintree_assistant_server.observability import configure_logging
from daintree_assistant_server.runbooks.catalog import load_catalog

from .._harness.env import build_settings, make_upstream_provider
from .._harness.fixtures import load_tool_inventory
from .harness import (
    CASES_DIR,
    IsolationSelector,
    RecordingProvider,
    digest,
    load_cases,
    run_case,
)

ROOT = Path(__file__).resolve().parents[2]


def identity() -> dict[str, Any]:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["rtk", "proxy", "git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def files_hash(paths: list[Path]) -> str:
        hasher = hashlib.sha256()
        for path in sorted(paths):
            hasher.update(str(path.relative_to(ROOT)).encode() + b"\0")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(65536), b""):
                    hasher.update(chunk)
        return hasher.hexdigest()

    source = ROOT / "src/daintree_assistant_server"
    return {
        "backend_sha": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
        "cli_pin": git("ls-tree", "HEAD", "cli").split()[2],
        "catalog_revision": load_catalog().revision,
        "tool_inventory_sha256": digest(load_tool_inventory()),
        "prompts_sha256": files_hash(
            list((source / "prompts").rglob("*.md")) + list((source / "prompts").rglob("*.j2"))
        ),
        "selector_sha256": files_hash(
            list((source / "runbooks/local/artifacts").rglob("*.json"))
            + list((source / "runbooks/local/artifacts").rglob("*.onnx"))
        ),
        "cases_sha256": files_hash(list(CASES_DIR.glob("*.yaml"))),
        "harness_sha256": files_hash(list(Path(__file__).parent.glob("*.py"))),
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    from daintree_assistant_server.main import build_container

    cases = load_cases(args.filter)
    whole = load_cases()
    if not cases:
        raise ValueError("No cases selected")
    if not 1 <= args.trials <= 5:
        raise ValueError("trials must be 1..5")
    ceiling = sum(c.get("max_rounds", 6) for c in cases) * args.trials
    if args.command == "live" and ceiling > args.max_calls:
        raise ValueError(
            f"Selected cases permit {ceiling} main calls; --max-calls is {args.max_calls}"
        )
    interval = getattr(args, "request_interval", 10.0)
    if not 0 <= interval <= 60:
        raise ValueError("request-interval must be 0..60 seconds")
    run_identity = identity()
    overrides = {
        "LOG_LEVEL": "WARNING",
        "TRACE_CAPTURE_MODE": "off",
        "TRACE_SINK": "null",
        "OPENROUTER_API_KEY": "",
    }
    settings = (
        build_settings(args.env_file, **overrides)
        if args.command == "live"
        else Settings(
            _env_file=None,
            STATE_SIGNING_KEY_CURRENT="behavior-bench-not-a-secret",
            BASETEN_API_KEY="",
            CEREBRAS_API_KEY="",
            **overrides,
        )
    )
    delegate = (
        make_upstream_provider(settings, timeout=settings.main_timeout_seconds)
        if args.command == "live"
        else None
    )
    provider = RecordingProvider(delegate, request_interval=interval)
    try:
        container = build_container(
            settings,
            provider=provider,
            selector=IsolationSelector() if args.mode == "isolation" else None,
        )
    except Exception:
        await provider.aclose()
        raise
    # This battery measures orchestration against synthetic local evidence.
    # Do not accidentally contact documentation MCP from an offline replay.
    container.orchestrator.docs_client = None
    results = []
    halted = False
    try:
        for trial in range(args.trials):
            for case in cases:
                try:
                    results.append(
                        await run_case(container, provider, case, mode=args.mode, trial=trial)
                    )
                except Exception as exc:
                    # Preserve completed results, stop spending, and omit exception
                    # text that may contain provider/request content.
                    results.append(
                        {
                            "id": case["id"],
                            "trial": trial,
                            "checks_passed": False,
                            "failures": ["backend_error:" + type(exc).__name__],
                            "review_required": True,
                            "review": case["review"],
                        }
                    )
                    halted = True
                    break
            if halted:
                break
    finally:
        await container.aclose()
    if args.transcripts:
        # Explicit opt-in, local-only. Never part of the content-free summary.
        args.transcripts.parent.mkdir(parents=True, exist_ok=True)
        args.transcripts.write_text(json.dumps(results, indent=2) + "\n")
        args.transcripts.chmod(0o600)
    for result in results:
        result.pop("transcript", None)
        result.pop("final", None)
    return {
        "meta": {
            "benchmark": "main_behavior",
            "execution": args.command,
            "mode": args.mode,
            "model": EXECUTOR_MODEL if delegate else "scripted-replay",
            "generated_at": datetime.now(UTC).isoformat(),
            "identity": run_identity,
            "docs_retrieval": "disabled",
            "request_interval_s": interval if delegate else None,
            "case_ids_expected": [c["id"] for c in whole],
            "trials": args.trials,
        },
        "summary": {
            "trials_total": len(results),
            "checks_passed": sum(r["checks_passed"] for r in results),
            "complete_corpus": not halted and len(results) == len(whole) * args.trials,
            "model_quality_measured": args.command == "live",
            "semantic_review_complete": False,
            "release_qualified": False,
        },
        "cases": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["validate", "replay", "live"])
    parser.add_argument("--mode", choices=["isolation", "end-to-end"], default="isolation")
    parser.add_argument("--filter")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument(
        "--max-calls",
        type=int,
        default=36,
        help="Live main-call ceiling (provider retries may cost additional requests)",
    )
    parser.add_argument(
        "--request-interval",
        type=float,
        default=10.0,
        help="Minimum seconds between live main calls (default: 10)",
    )
    parser.add_argument("--env-file")
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--transcripts",
        type=Path,
        help="Opt-in local full synthetic-session output; treat as sensitive",
    )
    args = parser.parse_args()
    configure_logging("WARNING")
    if args.command == "validate":
        cases = load_cases(args.filter)
        if not cases:
            parser.error("No cases selected")
        print(f"Validated {len(cases)} main-behavior cases; no model calls made.")
        return
    try:
        result = asyncio.run(run(args))
    except ValueError as exc:
        parser.error(str(exc))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result["summary"], indent=2))
    if result["summary"]["checks_passed"] != result["summary"]["trials_total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
