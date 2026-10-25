"""Where a ``bench local`` run lost its required runbooks, by request shape.

    python -m training.loss_inventory benchmarks/runbook_selection/results/<run>.json

Search sends the selector a lone user message with nothing loaded. The benchmark
also replays the old assistant's turns (an active set, prior history, a background
wake), which the classifier was trained on and still answers. A loss in the second
group says nothing about production search, and a retrain judged on the total can
trade one for the other unseen. So every loss is split by the shape of its case:

- ``bare``: one user message, no active set, no turn context, as search sends it.
- ``active`` / ``history`` / ``wake``: the historical multi-turn contract.
- ``docs``: a docs-classification case, scored for the docs head search never reads.

Each loss is also marked trained or untrained against the artifact's ``labels.json``.
Prints case ids and runbook ids only, never case text.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from benchmarks.runbook_selection.cases import BenchCase, load_cases  # noqa: E402
from daintree_assistant_server.runbooks.catalog import load_catalog  # noqa: E402
from daintree_assistant_server.runbooks.local.paths_local import (  # noqa: E402
    selector_artifact_dir,
)


def shape(case: BenchCase) -> str:
    if case.turn_context:
        return "wake"
    if case.active:
        return "active"
    if len(case.messages) > 1:
        return "history"
    if case.category == "docs-classification":
        return "docs"
    return "bare"


def inventory(run: dict[str, Any], artifact: Path) -> dict[str, Any]:
    cases = {c.id: c for c in load_cases(load_catalog())}
    trained = set(json.loads((artifact / "labels.json").read_text()))
    losses = [
        {
            "case": loss["case"],
            "runbook": loss["runbook"],
            "stage": loss["stage"],
            "shape": shape(cases[loss["case"]]),
            "trained": loss["runbook"] in trained,
            "p": loss.get("p"),
        }
        for loss in run["losses"]
    ]
    shapes = Counter(shape(c) for c in cases.values())
    return {
        "cases_by_shape": dict(shapes),
        "by_shape_and_stage": {
            f"{s}/{st}": n
            for (s, st), n in sorted(Counter((x["shape"], x["stage"]) for x in losses).items())
        },
        "by_runbook": dict(Counter(x["runbook"] for x in losses).most_common()),
        "untrained_losses": sum(not x["trained"] for x in losses),
        "forbidden": {
            case: hits
            for case, hits in zip(run["case_ids"], run["forbidden"], strict=True)
            if hits
        },
        "losses": sorted(losses, key=lambda x: (x["shape"], x["stage"], x["runbook"], x["case"])),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="a bench local --out JSON")
    parser.add_argument("--artifact", type=Path, default=None)
    args = parser.parse_args()
    run = json.loads(args.run.read_text())
    print(json.dumps(inventory(run, args.artifact or selector_artifact_dir()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
