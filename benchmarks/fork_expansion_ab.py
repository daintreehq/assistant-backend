"""Fork vs direct spawn: time to four executable spawn calls, same start state.

PAID. Every run makes real upstream calls, so this is not in the deterministic
dashboard and never runs in CI. Drive it by hand with a live `.env`:

    FORKED_TERMINAL_CALLS_MODE is set per arm by the harness; do not set it.
    .venv/bin/python -m benchmarks.fork_expansion_ab 3

Both arms start AFTER the worktrees are verified — an unseeded run has the
direct arm spend its turn on `worktree.list` while the fork arm spawns, which
compares two different turns and flatters the fork.

Result as of 2026-08-24 (3 scenarios x 3 runs): fork 15.1s mean / 13.1s median
against direct 16.0s / 13.7s, inside a 5-40s spread — no measurable difference.
See TODO.md for what that means for the feature.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, "src")
from daintree_assistant_server.config import Settings
from daintree_assistant_server.contracts.daintree_api import RespondRequest
from daintree_assistant_server.main import build_container

_INVENTORY = Path(__file__).resolve().parent / "_harness" / "fixtures" / "cli_tool_inventory.json"
with _INVENTORY.open(encoding="utf-8") as _handle:
    INV = json.load(_handle)
KEEP = {"agentTask__spawnForEdits", "worktree__list", "fs__read", "fs__list",
        "context__snapshot", "terminal__sendCommand", "queue__resolve"}
TOOLS = [t for t in INV if t["function"]["name"] in KEEP]

def seeded(user: str, worktrees: list[str]) -> list[dict[str, Any]]:
    """Both arms start AFTER the worktrees are verified, so neither pays for that."""
    listing = {"items": [{"id": w, "path": w, "branch": w.rsplit("/", 1)[-1],
                          "isMain": False} for w in worktrees]}
    return [
        {"role": "user", "content": user},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_wt", "type": "function",
            "function": {"name": "worktree__list", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_wt", "content": json.dumps(listing)},
    ]

SCENARIOS = [
    ("release",  "I've created four worktrees for the release push. Put a claude agent on "
                 "each, watching all four. Backend owns error handling and retries; frontend "
                 "owns the state bugs; tests owns missing coverage on the payment path; perf "
                 "owns the slow startup. They must not touch each other's areas. Go ahead.",
     ["/repo/backend", "/repo/frontend", "/repo/tests", "/repo/perf"]),
    ("issues",   "Four issues are ready and each has its own worktree. Put a claude agent on "
                 "each with a watcher: #101 flaky login redirect, #102 CSV export encoding, "
                 "#103 slow dashboard query, #104 stale cache on logout. One issue per agent, "
                 "no overlap. Go ahead.",
     ["/repo/i101", "/repo/i102", "/repo/i103", "/repo/i104"]),
    ("explore",  "Four worktrees are ready. Put a claude agent on each to INVESTIGATE only, "
                 "no edits, watching each: one maps the auth flow, one maps the billing "
                 "module, one audits the migration history, one traces the websocket "
                 "lifecycle. Go ahead.",
     ["/repo/auth", "/repo/billing", "/repo/migrations", "/repo/ws"]),
]

async def run(mode: str, name: str, user: str, wts: list[str], n: int) -> dict[str, Any]:
    os.environ["FORKED_TERMINAL_CALLS_MODE"] = mode
    container = build_container(Settings())
    container.docs_client = None
    req = RespondRequest.model_validate({
        "protocol_version": 2,
        "session": {"id": f"b-{mode}-{name}-{n}", "turn_id": "t2", "round": 1},
        "input": {"messages": seeded(user, wts), "tools": TOOLS},
        "startup": {}, "generation": {"stream": False}})
    t0 = time.perf_counter()
    try:
        out = await container.orchestrator.complete(req, request_id=f"b-{mode}-{name}-{n}")
        ms = (time.perf_counter() - t0) * 1000
        msg = out.body.get("message", {})
        calls = msg.get("tool_calls") or []
        spawns = [c for c in calls if c["function"]["name"] == "agentTask__spawnForEdits"]
        chars = sum(len(json.loads(c["function"]["arguments"]).get("taskPrompt", ""))
                    for c in spawns)
        usage = out.body.get("usage") or {}
        return {"ms": ms, "spawns": len(spawns), "chars": chars,
                "ptok": usage.get("prompt_tokens"), "ctok": usage.get("completion_tokens"),
                "cached": usage.get("cached_tokens")}
    except Exception as exc:
        return {"ms": (time.perf_counter()-t0)*1000, "spawns": -1, "chars": 0,
                "error": type(exc).__name__}
    finally:
        await container.aclose()

async def main() -> None:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    table: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for name, user, wts in SCENARIOS:
        for mode in ("disabled", "enabled"):
            rows = []
            for i in range(runs):
                r = await run(mode, name, user, wts, i)
                rows.append(r)
                print(f"{name:<8} {mode:<9} run{i}: {r['ms']:>7.0f} ms  "
                      f"spawns={r['spawns']:<3} prompt_chars={r['chars']:<5} "
                      f"ptok={r.get('ptok')} cached={r.get('cached')}", flush=True)
            table[(name, mode)] = rows
    print("\n" + "=" * 78)
    print(f"{'scenario':<10} {'arm':<10} {'mean ms':>9} {'median ms':>10} "
          f"{'4 spawns':>9} {'prompt chars':>13}")
    print("-" * 78)
    for name, _, _ in SCENARIOS:
        for mode in ("disabled", "enabled"):
            rows = table[(name, mode)]
            ok = sum(1 for r in rows if r["spawns"] == 4)
            mean = statistics.mean(r["ms"] for r in rows)
            med = statistics.median(r["ms"] for r in rows)
            chars = statistics.mean(r["chars"] for r in rows)
            arm = "direct" if mode == "disabled" else "fork"
            print(f"{name:<10} {arm:<10} {mean:>9.0f} {med:>10.0f} "
                  f"{ok:>6}/{len(rows)} {chars:>13.0f}")
    print("-" * 78)
    for mode in ("disabled", "enabled"):
        allrows = [r for (n, m), rs in table.items() if m == mode for r in rs]
        arm = "direct" if mode == "disabled" else "fork"
        print(f"{'ALL':<10} {arm:<10} {statistics.mean(r['ms'] for r in allrows):>9.0f} "
              f"{statistics.median(r['ms'] for r in allrows):>10.0f} "
              f"{sum(1 for r in allrows if r['spawns']==4):>6}/{len(allrows)} "
              f"{statistics.mean(r['chars'] for r in allrows):>13.0f}")

asyncio.run(main())
