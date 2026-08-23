"""Per-module coverage floors for the security and reliability surfaces.

A single global percentage is a poor gate: it stays green while the modules that
must not regress do, because a thousand well-covered lines elsewhere absorb the
loss. These floors name the code where a gap is not a style problem —
authentication, error mapping, the state token, retries, provider parsing,
request validation, deadlines, and redaction.

**Floors are a RATCHET, not a target.** Each is the measurement at the time it
was added, rounded DOWN to the nearest whole percent, so it can only be raised.
Lowering one is a deliberate act that shows up in review and needs a reason in
the commit message — which is the point, because the alternative is a number
that quietly follows coverage downward.

Usage (CI runs this after `pytest --cov --cov-report=json`):

    python -m tools.coverage_floors coverage.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# module path (as coverage reports it) -> minimum branch-aware percent.
#
# Measured 2026-08-14 against the full offline suite. `result_schema` is the one
# entry deliberately below the rest: it validates a client-supplied JSON schema
# and its uncovered lines are depth/complexity rejections that need a generated
# corpus to reach. It is recorded honestly at its real number rather than
# excluded, so the gap stays visible.
FLOORS: dict[str, int] = {
    "src/daintree_assistant_server/auth.py": 95,
    "src/daintree_assistant_server/errors.py": 93,
    "src/daintree_assistant_server/main.py": 95,
    "src/daintree_assistant_server/api/daintree_auth.py": 93,
    "src/daintree_assistant_server/providers/base.py": 92,
    # The SERVING adapter's implementation. `providers/together.py` subclasses it
    # and changes three class attributes plus a key decoder, so this is where
    # almost every line a real request executes actually lives — it holds the
    # credential, scrubs it out of echoed upstream error bodies, and owns the
    # error taxonomy the CLI branches on.
    #
    # 88 rather than a sibling's 92 because the mid-stream transport-failure
    # handlers are awkward to drive through a mock transport, not because less of
    # it matters — recorded honestly at its real number rather than rounded up,
    # so the gap stays visible. (A `providers/cerebras.py` floor of 92 sat above
    # this line until that adapter was deleted; the runner fails on a named file
    # that does not exist, which is how it was caught.)
    "src/daintree_assistant_server/providers/openrouter.py": 88,
    "src/daintree_assistant_server/providers/together.py": 90,
    # Added 2026-08-20 (measured 94.70%). The orchestrator earned a floor when the
    # streaming fold became a COST-CONTAINMENT surface as well as a correctness
    # one: the runaway guard is the only thing standing between a degenerating
    # endpoint and the full token ceiling, it lives on the least-covered kind of
    # path (a mid-stream raise), and a global percentage would absorb its loss
    # without moving.
    "src/daintree_assistant_server/services/chat_orchestrator.py": 94,
    # Added 2026-08-23 (measured 100%). A SPEND surface: it is the only thing
    # that can refuse a turn before the upstream is paid for, and its arithmetic
    # is the input to a 413 a caller sees. Small enough that a global percentage
    # would never notice it regressing, and easy to break by accident — a role
    # check that stops matching, or a `max_tokens` change that quietly folds the
    # reservation back into the refusal, both leave every existing test passing.
    "src/daintree_assistant_server/services/context_budget.py": 100,
    "src/daintree_assistant_server/services/deadlines.py": 99,
    # Added with the forked terminal-spawn feature (measured 2026-08-23 at 100,
    # 100, 100 and 94.59; rounded down). These earn floors for the same reason
    # `auth` and `validation` do — they are the code that decides whether an
    # internal, server-only function call can reach a client, and whether a
    # model that just read untrusted terminal output can move where an agent
    # runs. A global percentage would absorb a regression in any of them without
    # moving.
    "src/daintree_assistant_server/contracts/forked_terminals.py": 100,
    "src/daintree_assistant_server/services/forked_terminal_calls.py": 100,
    "src/daintree_assistant_server/services/forked_tool_call_rewriter.py": 100,
    "src/daintree_assistant_server/services/fork_prompt_expansion.py": 94,
    "src/daintree_assistant_server/services/result_schema.py": 83,
    # Ratcheted 2026-08-23 to 94 (measured 94.96, rounded down) with the transcript state
    # machine, the shape caps and the image data-URL policy.
    "src/daintree_assistant_server/services/validation.py": 94,
    "src/daintree_assistant_server/services/turn_accounting.py": 99,
    "src/daintree_assistant_server/state/codec.py": 93,
    "src/daintree_assistant_server/text_utils.py": 90,
}


def _percent(entry: dict[str, object]) -> float:
    summary = entry.get("summary")
    assert isinstance(summary, dict)
    percent = summary["percent_covered"]
    assert isinstance(percent, (int, float))
    return float(percent)


def main(path: str) -> int:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    files = data.get("files", {})

    missing = sorted(f for f in FLOORS if f not in files)
    if missing:
        # A renamed or deleted module must not silently drop its floor.
        print("coverage floors reference modules not in the report:", file=sys.stderr)
        for name in missing:
            print(f"  {name}", file=sys.stderr)
        print(
            "\nIf the module moved, update tools/coverage_floors.py in the same "
            "commit — a floor that names nothing enforces nothing.",
            file=sys.stderr,
        )
        return 1

    failures: list[str] = []
    improved: list[str] = []
    for name, floor in sorted(FLOORS.items()):
        actual = _percent(files[name])
        if actual + 1e-9 < floor:
            failures.append(f"  {name}: {actual:.1f}% < {floor}% floor")
        elif actual >= floor + 2:
            improved.append(f"  {name}: {actual:.1f}% (floor {floor}%)")

    if improved:
        print("Coverage improved well past its floor — consider raising it:")
        print("\n".join(improved))

    if failures:
        print("\nCoverage floor breached on security/reliability modules:", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        print(
            "\nThese are the surfaces where an untested branch is a security or "
            "correctness hole rather than a style gap. Add the test; do not lower "
            "the floor without saying why in the commit message.",
            file=sys.stderr,
        )
        return 1

    print(f"All {len(FLOORS)} security/reliability coverage floors met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "coverage.json"))
