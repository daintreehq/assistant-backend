"""The committed release baseline, and the bands a change may move it within.

Benchmark results are gitignored, which is right for a local run and useless for
a release comparison: "did the prompt get bigger?" cannot be answered from a
directory nobody keeps. This records the deterministic facts that describe what
the server actually sends, so a regression shows up as a failing test in the PR
that caused it rather than as a surprise on a bill.

**What is deliberately NOT here:** anything that needs a paid call. Live latency,
skill-selection accuracy and injection results belong in the nightly gate; a
baseline that cannot be checked offline would either be skipped in CI or make CI
cost money on every push.

**Bands, not equality.** Each metric declares how far it may move before someone
has to look. They are chosen by how the number behaves, not by uniform taste:

* the base prompt is the highest-leverage text in the system and ships on every
  request, so it gets a tight band;
* the selector prompt grows with the catalog by design, so it gets a loose one;
* skill bodies churn — the learning harness rewrites them — so they are recorded
  and not banded at all;
* zero-tolerance guards are not bands. Zero is the value.

Regenerate deliberately, reviewing the diff:

    python -m tools.baseline capture > benchmarks/baseline.json
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "benchmarks" / "baseline.json"
INVENTORY_PATH = REPO_ROOT / "benchmarks" / "_harness" / "fixtures" / "cli_tool_inventory.json"

# metric -> fractional tolerance. Absent means "recorded, never enforced".
BANDS: dict[str, float] = {
    # Every request carries this. 5% is roughly one new rule; more than that
    # should be a decision, not a drift.
    "base_prompt_bytes": 0.05,
    # Moves only when the cli/ submodule pin moves, which should be visible.
    "tool_inventory_bytes": 0.10,
    # The shipped selector graphs (both encoders + the set-wise head). Moves
    # only on a retrain+export, which is a deliberate act; a silent change here
    # means the artifact was touched without the runbook.
    "selector_onnx_bytes": 0.30,
}


def capture() -> dict[str, Any]:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from daintree_assistant_server.config import (
        OPENROUTER_MODEL,
        PUBLIC_MODEL_ID,
    )
    from daintree_assistant_server.prompts.loader import PromptLoader
    from daintree_assistant_server.providers.policy import DEFAULT_POLICY
    from daintree_assistant_server.skills.catalog import load_catalog
    from daintree_assistant_server.skills.local.documents import routing_revision
    from daintree_assistant_server.skills.local.paths_local import selector_artifact_dir

    loader = PromptLoader()
    catalog = load_catalog()
    artifact = selector_artifact_dir()
    selector_meta = json.loads((artifact / "meta.json").read_text())
    selector_labels = json.loads((artifact / "labels.json").read_text())
    graphs = [
        artifact / "retriever" / "model_int8.onnx",
        artifact / "ranking" / "model_int8.onnx",
        artifact / "head.onnx",
    ]
    inventory_raw = INVENTORY_PATH.read_bytes()
    inventory = json.loads(inventory_raw)

    return {
        "identity": {
            "model": OPENROUTER_MODEL,
            "public_model": PUBLIC_MODEL_ID,
            "privacy_mode": DEFAULT_POLICY.privacy.value,
            "default_endpoint_sort": DEFAULT_POLICY.sort.value,
        },
        "selector": {
            # Selection is LOCAL and two-stage: these describe the shipped
            # artifact — total graph size, the shortlist width, and how the
            # shortlist's order is treated.
            "selector_onnx_bytes": sum(g.stat().st_size for g in graphs),
            "candidates": selector_meta.get("candidates", 0),
            "ordering": selector_meta.get("ordering", ""),
            "trained_skills": len(selector_labels),
            # RECORDED, NOT ENFORCED, and the difference is the architecture.
            # The set-wise head has no per-skill weights, so a catalog that has
            # moved since training is answered normally rather than degrading
            # open — this is a "how far has the corpus drifted from the live
            # catalog" quality signal for a human reading a release, not a
            # correctness gate. Under the previous fixed head it was a gate.
            "routing_revision_at_train": selector_meta.get(
                "routing_revision_at_train", ""
            ),
            "routing_revision_matches_catalog": (
                selector_meta.get("routing_revision_at_train")
                == routing_revision(catalog)
            ),
        },
        "prompt": {
            "base_prompt_bytes": len(loader.base_prompt().encode("utf-8")),
            "skill_body_bytes_total": sum(
                len(s.body.encode("utf-8")) for s in catalog.skills
            ),
        },
        "catalog": {
            "skills": len(catalog.skills),
            "foundations": sum(1 for s in catalog.skills if s.meta.foundation),
            "revision": catalog.revision,
        },
        "tools": {
            "count": len(inventory),
            "tool_inventory_bytes": len(inventory_raw),
            # Names only: schema wording changes constantly, the SURFACE does not.
            "names_sha256": hashlib.sha256(
                "\n".join(sorted(t["function"]["name"] for t in inventory)).encode()
            ).hexdigest(),
        },
    }


def _flat(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for section in data.values() for k, v in section.items()}


def compare(current: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    """Band violations, as human sentences. Empty means within tolerance."""
    now, was = _flat(current), _flat(baseline)
    problems: list[str] = []
    for metric, tolerance in BANDS.items():
        if metric not in was or metric not in now:
            problems.append(f"{metric} is missing from the baseline or the capture")
            continue
        before, after = was[metric], now[metric]
        if before == 0:
            continue
        drift = (after - before) / before
        if abs(drift) > tolerance:
            problems.append(
                f"{metric} moved {drift:+.1%} ({before:,} -> {after:,}), outside its "
                f"±{tolerance:.0%} band"
            )
    return problems


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "capture":
        print(json.dumps(capture(), indent=2, sort_keys=True))
        return 0
    problems = compare(capture(), json.loads(BASELINE_PATH.read_text(encoding="utf-8")))
    if problems:
        print("Baseline regression:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(
            "\nIf the change is intended, refresh with "
            "`python -m tools.baseline capture > benchmarks/baseline.json` and "
            "explain the move in the commit message.",
            file=sys.stderr,
        )
        return 1
    print("Baseline within bands.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
