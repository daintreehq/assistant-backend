"""Re-calibrate the admission floors against a freshly exported artifact.

``SetwiseClassifier.NEWCOMER_FLOOR_WHEN_ACTIVE`` and ``BONUS_FLOOR`` are
probabilities, and a probability only means something relative to the sigmoid
that produced it. A retrain can move the whole band without changing a single
decision boundary visible from the code, which is why the constants' own
docstrings say to re-validate after every export — this is the thing that does
it, instead of leaving it to be remembered.

The two numbers to read together:

- **recall-pass** — every required runbook present. A miss answers without its
  runbook, the worst outcome available, so this is the product metric.
- **extras** — runbooks loaded that the case neither required nor allowed. Each
  costs its body in prompt bytes on every round of the turn, so this is the
  price being paid for the recall.

The floors trade one directly against the other. Pick the lowest floor that has
stopped buying recall — below that point every step is extras only.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from benchmarks.runbook_selection.cases import load_cases  # noqa: E402
from daintree_assistant_server.runbooks.catalog import (  # noqa: E402
    default_runbooks_dir,
    load_catalog,
)
from daintree_assistant_server.runbooks.local import setwise  # noqa: E402
from daintree_assistant_server.runbooks.local.documents import query_document  # noqa: E402
from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier  # noqa: E402

ARTIFACT = (
    ROOT / "src" / "daintree_assistant_server" / "runbooks" / "local"
    / "artifacts" / "selector"
)


def score(clf: SetwiseClassifier, catalog, cases) -> tuple[int, int, float]:  # type: ignore[no-untyped-def]
    selectable = catalog.selectable_ids
    recall_pass = 0
    extras = 0
    loaded = 0
    for case in cases:
        eligible = [m for m in case.messages if m.role in ("user", "assistant", "tool")]
        if case.active and eligible and eligible[-1].role != "user":
            picked = list(case.active)
        else:
            query = query_document(case.messages, turn_context=case.turn_context)
            visible = [sid for sid in case.active if sid in selectable]
            picked = [
                sid for sid in clf.select(query, visible).runbook_ids if sid in selectable
            ]
        got = set(picked)
        miss = set(case.required) - got
        extra = got - set(case.required) - set(case.optional)
        recall_pass += not miss and (bool(case.required) or not extra)
        extras += len(extra)
        loaded += len(picked)
    return recall_pass, extras, loaded / len(cases)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", default=str(ARTIFACT))
    ap.add_argument("--bonus", nargs="*", type=float,
                    default=[0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65])
    ap.add_argument("--newcomer", nargs="*", type=float, default=[0.55, 0.65, 0.75])
    args = ap.parse_args()

    catalog = load_catalog(default_runbooks_dir())
    cases = load_cases(catalog)
    clf = SetwiseClassifier(Path(args.artifact), quantized=True)
    clf.sync(catalog)

    original = (
        setwise.SetwiseClassifier.NEWCOMER_FLOOR_WHEN_ACTIVE,
        setwise.SetwiseClassifier.BONUS_FLOOR,
    )
    print(f"shipped floors: newcomer={original[0]} bonus={original[1]}")
    print(f"\n{'newcomer':>9} {'bonus':>6} {'recall-pass':>12} {'extras':>7} {'mean set':>9}")
    try:
        for newcomer in args.newcomer:
            setwise.SetwiseClassifier.NEWCOMER_FLOOR_WHEN_ACTIVE = newcomer
            for bonus in args.bonus:
                setwise.SetwiseClassifier.BONUS_FLOOR = bonus
                rp, extras, mean_set = score(clf, catalog, cases)
                flag = "  <- shipped" if (newcomer, bonus) == original else ""
                print(
                    f"{newcomer:>9.2f} {bonus:>6.2f} {rp:>7d}/{len(cases)} "
                    f"{extras:>7d} {mean_set:>9.2f}{flag}"
                )
    finally:
        (
            setwise.SetwiseClassifier.NEWCOMER_FLOOR_WHEN_ACTIVE,
            setwise.SetwiseClassifier.BONUS_FLOOR,
        ) = original
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
