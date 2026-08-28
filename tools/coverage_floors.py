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
    # Added 2026-08-24. The Authorization header, read two ways. `parse_bearer` is
    # the door's own grammar and `credential_candidates` decides what never
    # reaches a log line — and the second is the one a percentage protects badly,
    # because a branch that stops being exercised there does not fail anything, it
    # just quietly stops masking a shape of credential.
    "src/daintree_assistant_server/services/bearer.py": 98,
    # Added 2026-08-24. The token verifier. On this list for the obvious reason
    # and for a less obvious one: most of its branches are REFUSALS, and a refusal
    # that stops being exercised does not break a test — it silently starts
    # accepting something. 94 is the measured figure rounded down, per the ratchet
    # rule above — the five uncovered branches are the JWKS refresh's own
    # bookkeeping, not a verification rule.
    "src/daintree_assistant_server/services/supabase_auth.py": 94,
    # Added 2026-08-24 (measured 95.58%). `config.py` earned a floor when it took
    # on the OAuth deployment configuration: `auth_manifest_problems()` decides
    # what `/v1/daintree/auth/config` will PUBLISH to any caller, and that route
    # deliberately answers while the service is unready — so readiness is not a
    # second line of defence behind it. A validation branch that stops being
    # exercised is a manifest nobody checks, and the aggregate 90% is exactly
    # wide enough to absorb losing a few of them.
    "src/daintree_assistant_server/config.py": 95,
    # Added 2026-08-25 (measured 97%). The billing authority's client. On this
    # list because almost all of it is REFUSAL: every branch that is not the happy
    # path exists to keep an uncertain answer from becoming a verdict about
    # somebody's plan, and a refusal that stops being exercised does not fail a
    # test — it silently starts letting a malformed body through as a decision.
    "src/daintree_assistant_server/services/entitlement.py": 97,
    "src/daintree_assistant_server/errors.py": 93,
    "src/daintree_assistant_server/main.py": 95,
    "src/daintree_assistant_server/api/daintree_auth.py": 93,
    # Added 2026-08-25 (measured 98%). The account-status route. Small, and on
    # this list because of WHAT it decides rather than how much of it there is:
    # which callers it will answer for at all, and which of four `access` values
    # it reports. A branch that stops being exercised here either exposes an
    # account to a caller no longer entitled to read it, or reports an outage as
    # a settled fact about somebody's plan.
    "src/daintree_assistant_server/api/daintree_account.py": 98,
    # Added 2026-08-25 (measured 100%). The account response model. Tiny, and its
    # cross-field rules are the second boundary the brief asks for: the
    # entitlement client validates the WEBSITE's contract, this validates OURS. A
    # rule that stops being exercised does not fail — it just stops catching an
    # incoherent body that a future edit assembles.
    "src/daintree_assistant_server/contracts/account.py": 100,
    # Added 2026-08-25 (measured 97%). The gate on the two routes that spend
    # money. Every branch here is a decision about somebody's money or somebody's
    # session, and the two directions fail very differently: a lost refusal is
    # unbilled work, while a refusal that stops being an outage-shaped 503 is a
    # paying customer told to buy a second plan.
    "src/daintree_assistant_server/api/paid_access.py": 98,
    # Added 2026-08-25 (measured 100%). What may be written down about a paying
    # account. Tiny, and on this list for one reason: it carries the raw subject
    # so durable accounting has a key to join on, and every projection that
    # writes something out has to refuse it. A branch that stops being exercised
    # here is a raw identifier reaching a log pipeline.
    "src/daintree_assistant_server/services/account_context.py": 100,
    "src/daintree_assistant_server/providers/base.py": 92,
    # The SERVING adapter's implementation. `providers/baseten.py` subclasses it
    # and changes three class attributes plus a key decoder and the thinking
    # posture, so this is where
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
    # The SERVING adapter. On this list for the same reason Cerebras is: it holds
    # the server credential, and its whole job is the handful of ways its body
    # differs from the parent — the `reasoning_effort` floor above all, where a
    # regression is not an error but a silently more expensive server on every
    # turn. Nothing else would catch that.
    "src/daintree_assistant_server/providers/baseten.py": 97,
    # The FAST-RESPONSE adapter. On this list because it holds the second server
    # credential and because its whole job is a body that differs from the parent
    # in three specific ways — a regression in any of them is silent on the wire.
    "src/daintree_assistant_server/providers/cerebras.py": 97,
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
    "src/daintree_assistant_server/services/result_schema.py": 83,
    # Ratcheted 2026-08-23 to 94 (measured 94.96, rounded down) with the transcript state
    # machine, the shape caps and the image data-URL policy.
    "src/daintree_assistant_server/services/validation.py": 94,
    "src/daintree_assistant_server/services/turn_accounting.py": 99,
    # The fast-response stage. A reliability surface rather than a security one:
    # its contract is that it FAILS OPEN — every timeout, transport error and
    # rejected output must resolve to "no preamble" and leave the turn alone. An
    # uncovered branch here is a path that could raise into a real request.
    "src/daintree_assistant_server/services/fast_response.py": 91,
    "src/daintree_assistant_server/state/codec.py": 93,
    # The TRAINING-TRACE plane. It belongs on this list for the same reason the
    # state codec does: it is the code that decides what user content is written
    # down, under what pseudonym, and to where. A regression here is not a lost
    # diagnostic — it is content stored that should not have been, or identity
    # stored in the clear.
    #
    # Each floor is the measurement rounded DOWN, so it can only ratchet up.
    # `scope.py` at 85 is the lowest and is honest rather than rounded: its
    # uncovered lines are the nested `except` arms that exist so a storage failure
    # can never reach a turn, and driving each one needs a sink that fails in a
    # different specific place. The paths that MATTER — drop-on-oversize,
    # backpressure, failed-blob honesty, the bounded flush — are covered.
    "src/daintree_assistant_server/tracing/scope.py": 85,
    "src/daintree_assistant_server/tracing/recorder.py": 93,
    "src/daintree_assistant_server/tracing/eligibility.py": 93,
    "src/daintree_assistant_server/tracing/models.py": 96,
    # `scrub.py` and `redaction.py` are the two gates that decide what is written
    # down and what leaves as training data. Redaction is held at 100 deliberately:
    # for a boundary of that kind, every new executable branch should have been
    # exercised before it ships.
    "src/daintree_assistant_server/tracing/scrub.py": 93,
    "src/daintree_assistant_server/tracing/redaction.py": 100,
    "src/daintree_assistant_server/tracing/canonical.py": 84,
    "src/daintree_assistant_server/tracing/sinks/filesystem.py": 94,
    "src/daintree_assistant_server/tracing/sinks/gcs.py": 96,
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
