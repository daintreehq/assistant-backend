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
    "src/daintree_assistant_server/providers/openrouter.py": 89,
    "src/daintree_assistant_server/providers/policy.py": 93,
    "src/daintree_assistant_server/services/deadlines.py": 99,
    "src/daintree_assistant_server/services/result_schema.py": 83,
    "src/daintree_assistant_server/services/validation.py": 92,
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
