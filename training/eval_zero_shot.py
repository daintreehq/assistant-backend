"""Does a runbook neither stage ever trained on actually route?

The whole reason for the two-stage architecture is that a runbook can be added
after the build and still be selected — no head row to grow, no retrain, no
degraded window. That claim is cheap to assert and worth measuring, so this
scores an artifact built with ``--holdout-runbooks`` on exactly the benchmark cases
whose REQUIRED answer is one of the held-out runbooks, and compares it against the
shipped artifact on the same cases.

The holdout must be applied to BOTH stages (``train_retriever --holdout-runbooks``
and ``train_setwise --holdout-runbooks``). Held out of stage 2 only, the runbook was
still shortlisted by an encoder that had trained on it, and the probe would be
measuring half the pipeline while reporting the whole one.
"""

from __future__ import annotations

import argparse
import json
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
from daintree_assistant_server.runbooks.local.documents import query_document  # noqa: E402
from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier  # noqa: E402

SHIPPED = (
    ROOT / "src" / "daintree_assistant_server" / "runbooks" / "local"
    / "artifacts" / "selector"
)


def pick(clf: SetwiseClassifier, case, selectable) -> list[str]:  # type: ignore[no-untyped-def]
    eligible = [m for m in case.messages if m.role in ("user", "assistant", "tool")]
    if case.active and eligible and eligible[-1].role != "user":
        return list(case.active)
    query = query_document(case.messages, turn_context=case.turn_context)
    visible = [sid for sid in case.active if sid in selectable]
    selection = clf.select(query, visible, exclude=case.exclude)
    return [sid for sid in selection.runbook_ids if sid in selectable]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifact", required=True, help="artifact built with --holdout-runbooks")
    ap.add_argument("--reference", default=str(SHIPPED))
    args = ap.parse_args()

    catalog = load_catalog(default_runbooks_dir())
    cases = load_cases(catalog)
    selectable = catalog.selectable_ids

    meta = json.loads((Path(args.artifact) / "meta.json").read_text())
    holdout = set(meta.get("holdout_runbooks") or [])
    if not holdout:
        raise SystemExit(
            f"{args.artifact} records no holdout_runbooks — it is an ordinary "
            "artifact, and scoring it here would prove nothing"
        )
    print(f"held out of BOTH stages: {', '.join(sorted(holdout))}")

    probe = SetwiseClassifier(Path(args.artifact))
    probe.sync(catalog)
    reference = SetwiseClassifier(Path(args.reference))
    reference.sync(catalog)

    # Cases whose REQUIRED answer includes a held-out runbook: the ones that can
    # only pass if a never-trained runbook was selected on the strength of its
    # embedding alone.
    target = [c for c in cases if set(c.required) & holdout]
    print(f"benchmark cases requiring a held-out runbook: {len(target)}\n")

    zero_shot_hits = 0
    reference_hits = 0
    for case in target:
        got = pick(probe, case, selectable)
        ref = pick(reference, case, selectable)
        ok = not (set(case.required) - set(got))
        ref_ok = not (set(case.required) - set(ref))
        zero_shot_hits += ok
        reference_hits += ref_ok
        print(
            f"  {'PASS' if ok else 'MISS'}  {case.id:40} "
            f"zero-shot={[x.rsplit('.', 1)[-1] for x in got]}"
            f"{'' if ok == ref_ok else '   (trained: ' + ('PASS' if ref_ok else 'MISS') + ')'}"
        )

    print(
        f"\nzero-shot recall on held-out runbooks: {zero_shot_hits}/{len(target)}"
        f"   (same artifact family WITH training: {reference_hits}/{len(target)})"
    )

    # The other half of the question: does holding three runbooks out damage the
    # rest of the catalog? It should not — nothing else lost any training data —
    # and if it does, the probe's headline number is not comparable.
    rest = [c for c in cases if c.required and not (set(c.required) & holdout)]
    probe_rest = sum(
        1 for c in rest if not (set(c.required) - set(pick(probe, c, selectable)))
    )
    ref_rest = sum(
        1 for c in rest if not (set(c.required) - set(pick(reference, c, selectable)))
    )
    print(f"remaining {len(rest)} cases: probe {probe_rest}, shipped {ref_rest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
