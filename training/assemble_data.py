"""Turn the generation workflow's raw agent output into validated training sets.

Reads the workflow journal (one JSON result per agent), validates every skill id
against the live catalog, deduplicates, and writes four JSONL corpora:

    selection.jsonl — one row per turn: query, history, prior active set,
    expected skill ids, docs label, empty-answer kind. The classifier's whole
    diet; the docs and cardinality labels are derived from it at train time.

## The contamination gate

The 150 cases in ``benchmarks/skill_selection/cases/`` are the held-out
evaluation set and the only evidence that any of this works. Generating agents
were told not to read them, but "was told not to" is not a control — the models
share a domain, and a plausible query for "rename my terminals" is a short
sentence that two independent writers could easily land on identically.

So overlap is MEASURED here, not assumed away. Any generated query that
normalises to the same string as a benchmark query is dropped and counted. A
silent leak would not fail anything; it would just quietly turn the benchmark
into a training-set score, which is the one outcome that would make every number
in the final report meaningless.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.skill_selection.cases import load_cases  # noqa: E402
from daintree_assistant_server.skills.catalog import (  # noqa: E402
    default_skills_dir,
    load_catalog,
)
from training.common import render_training_query  # noqa: E402

DATA = ROOT / "training" / "data"

#: The domain foundation. Paired with a selected specific DOWNSTREAM by the
#: backend, so it must never appear in a training target beside one.
_FOUNDATION = "daintree.foundation"


def _norm(text: str) -> str:
    """Normalise a query for duplicate and contamination comparison."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _load_journal(path: Path) -> list[dict]:
    results = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("type") == "result" and isinstance(row.get("result"), dict):
            results.append(row["result"])
    return results


def _benchmark_queries(catalog: object) -> set[str]:
    """Every string that appears in the held-out corpus, normalised."""
    out: set[str] = set()
    for case in load_cases(catalog):
        for msg in case.messages:
            content = msg.content
            if isinstance(content, str):
                out.add(_norm(content))
        if case.turn_context:
            out.add(_norm(case.turn_context))
    out.discard("")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--journal",
        required=True,
        nargs="+",
        help="one or more workflow journal.jsonl files (later runs fill gaps in earlier ones)",
    )
    # Cumulative by DEFAULT. Journals are session-local and not reconstructible
    # from a fresh clone, so the committed corpus IS the base: a retrain extends
    # it rather than rebuilding from whatever journals happen to be on disk.
    # Pass --base '' to rebuild from journals alone.
    ap.add_argument(
        "--base",
        default=str(DATA / "selection.jsonl"),
        help="committed corpus carried forward and extended (empty string disables)",
    )
    args = ap.parse_args()

    catalog = load_catalog(default_skills_dir())
    valid_ids = set(catalog.selectable_ids)
    held_out = _benchmark_queries(catalog)
    print(f"catalog: {len(valid_ids)} selectable ids | held-out strings: {len(held_out)}")

    results: list[dict] = []
    for journal in args.journal:
        rows = _load_journal(Path(journal))
        print(f"  {journal}: {len(rows)} agent results")
        results.extend(rows)
    per_skill = [r for r in results if "skill_id" in r]
    bundles = [r for r in results if "items" in r]
    print(f"journal: {len(per_skill)} per-skill agents, {len(bundles)} cross-cutting bundles")

    stats: Counter[str] = Counter()
    seen_inputs: dict[str, tuple[str, ...]] = {}
    selection: list[dict] = []

    def emit(
        query: str,
        expect: list[str],
        *,
        history: list[str] | None = None,
        active: list[str] | None = None,
        source: str,
        is_docs: bool | None = None,
        hard_negatives: list[str] | None = None,
        empty_kind: str | None = None,
    ) -> None:
        query = " ".join(query.split())
        if not query:
            stats["dropped_empty"] += 1
            return
        # Canonicalise BEFORE any key is computed: a "specific+foundation" row and
        # its specific-only twin must collide in dedupe, not both be admitted.
        if len(expect) > 1 and _FOUNDATION in expect:
            expect = [i for i in expect if i != _FOUNDATION]
            stats["foundation_unpaired"] += 1
        # The gate covers HISTORY too — history is part of the rendered model
        # input (render_training_query), so a benchmark message pasted into a
        # generated history is exactly as much leakage as one in the query.
        if _norm(query) in held_out or any(
            _norm(h.partition(":")[2] or h) in held_out for h in (history or [])
        ):
            stats["dropped_contaminated"] += 1
            return
        # A skill cannot be both the answer and a counter-example of itself.
        if hard_negatives and set(expect) & set(hard_negatives):
            hard_negatives = [h for h in hard_negatives if h not in expect]
            stats["hard_negative_overlap_stripped"] += 1
        # Key on the SERVING render of the input — the exact string the model
        # trains on (render_training_query: role-stripped, history-budgeted) plus
        # the active set in order — not a hand-rolled concatenation that keeps
        # untruncated history the model never sees. Keying on input+label let
        # two contradictory labels for one input both in — measured: "do i have
        # any timers set" trained as foundation AND as watch-outcome. First
        # admission wins; later conflicts are counted so a noisy generator shows
        # up in the stats instead of the gradients.
        # Validate ids BEFORE the row can claim a dedupe key. Recording the key
        # first let an unknown-id row poison the input: it reserved the slot,
        # was then dropped, and the next VALID row for the same rendered input
        # was discarded as a duplicate/conflict against a label that never
        # landed in the corpus.
        bad = [i for i in [*expect, *(active or []), *(hard_negatives or [])] if i not in valid_ids]
        if bad:
            stats["dropped_unknown_id"] += 1
            return
        input_key = (
            _norm(render_training_query(query, history))
            + "|" + ",".join(active or [])
        )
        if input_key in seen_inputs:
            prior = seen_inputs[input_key]
            if prior != tuple(sorted(expect)):
                stats["dropped_conflicting_label"] += 1
            else:
                stats["dropped_duplicate"] += 1
            return
        seen_inputs[input_key] = tuple(sorted(expect))
        stats[source] += 1
        selection.append({
            "query": query,
            "history": history or [],
            "active_ids": active or [],
            "expect_ids": expect,
            "is_docs": is_docs,
            "hard_negatives": hard_negatives or [],
            "source": source,
            # Which KIND of empty this example teaches — an acknowledgement and
            # a weather question are both empty but not the same lesson. Kept as
            # corpus metadata for balancing/analysis; the classifier itself
            # learns "empty" from the zero-cardinality label.
            "empty_kind": empty_kind if not expect else None,
        })

    for block in per_skill:
        sid = block["skill_id"]
        if sid not in valid_ids:
            stats["skipped_agent_bad_skill"] += 1
            continue
        for p in block.get("positives", []):
            # Round two labels each positive with the SITUATION it was imagined
            # from rather than a style tag; round one used "style". Both are
            # metadata the trainer ignores — only the query and the label matter —
            # so accept either shape rather than versioning the corpus.
            emit(p["query"], [sid], source="positive", is_docs=False)
        for n in block.get("boundary_negatives", []):
            emit(
                n["query"],
                list(n["correct_skill_ids"]),
                source="boundary",
                is_docs=False,
                hard_negatives=[sid],
            )
        for f in block.get("followups", []):
            emit(
                f["query"],
                list(f["keeps"]),
                history=list(f.get("history", [])),
                active=list(f.get("keeps", [])),
                source="followup",
                is_docs=False,
            )


    # The committed corpus is the reconstructible base (journals are
    # session-local); admit it through the SAME gates as fresh rows.
    if args.base:
        base_path = Path(args.base)
        if base_path.exists():
            base_rows = [
                json.loads(line)
                for line in base_path.read_text().splitlines()
                if line.strip()
            ]
            print(f"base corpus: {len(base_rows)} rows from {base_path}")
            for r in base_rows:
                emit(
                    r["query"], list(r["expect_ids"]),
                    history=r.get("history"), active=r.get("active_ids"),
                    source=r.get("source", "base"), is_docs=r.get("is_docs"),
                    hard_negatives=r.get("hard_negatives"),
                    empty_kind=r.get("empty_kind"),
                )

    for bundle in bundles:
        items = bundle["items"]
        shape = set(items[0].keys())
        if "kind" in shape and "skill_ids" in shape:
            # Off-domain bundle: general-knowledge junk that must select nothing,
            # plus deliberately-included near misses that must still route.
            for it in items:
                ids = list(it.get("skill_ids", []))
                emit(
                    it["query"],
                    ids,
                    source="off_domain" if it["kind"] == "off_domain" else "near_miss",
                    is_docs=False,
                    empty_kind="off_domain",
                )
        elif "kind" in shape:
            for it in items:
                emit(it["query"], [], history=list(it.get("history", [])),
                     source="no_task", is_docs=False, empty_kind="no_task")
        elif "is_docs" in shape:
            for it in items:
                # A docs question with no skills is an empty answer reached by a
                # different route than chatter: there IS a question, it just gets
                # answered from retrieved pages rather than a runbook. It trains
                # the no-task sentinel, not the off-domain one — an off-domain
                # sentinel firing on a product question would suppress the
                # documentation lookup along with the skill.
                emit(it["query"], list(it.get("skill_ids", [])),
                     source="docs", is_docs=bool(it["is_docs"]), empty_kind="no_task")
        elif "active_ids" in shape:
            # Three generators share this schema; they are separated by what the
            # item actually says, not by which run produced it, so a re-assembly
            # from any mix of journals still labels them consistently.
            #
            # The distinction matters because `source` drives the balanced
            # sampler in train_classifier: these are the three shapes the model
            # was measurably under-served on, and lumping them together would
            # hide that.
            for it in items:
                q = it["query"].strip()
                if q.startswith("["):
                    # A runtime event, not a user message — the generator was told
                    # to bracket wakes.
                    kind = "wake"
                elif not it["expect_ids"]:
                    # Loaded work, and a turn that still needs no runbook: an
                    # acknowledgement, chatter, or an off-domain aside mid-session.
                    # Distinct from `no_task`, which is almost all bare openers
                    # with no history at all.
                    kind = "no_task_stateful"
                else:
                    kind = "topic_shift"
                emit(q, list(it["expect_ids"]), history=list(it.get("history", [])),
                     active=list(it.get("active_ids", [])), source=kind, is_docs=False)
        elif "expect_ids" in shape:
            for it in items:
                emit(it["query"], list(it["expect_ids"]), source="multi_skill", is_docs=False)
        elif "skill_ids" in shape:
            # Foundation gap-fill: single direct control-plane actions, plain
            # state reads, and the specific-skill boundary. Labelled by target so
            # per-source counts stay readable — the empty-answer rows here are
            # boundary cases, not chatter.
            for it in items:
                ids = list(it.get("skill_ids", []))
                emit(
                    it["query"],
                    ids,
                    source=(
                        "foundation" if ids == ["daintree.foundation"]
                        else "foundation_boundary"
                    ),
                    is_docs=False,
                )
        else:
            raise ValueError(f"unrecognised generated bundle shape: {sorted(shape)}")

    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "selection.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in selection)
    )


    print("\nassembled:")
    for k, v in sorted(stats.items()):
        print(f"  {k:26} {v}")
    print(f"\n  selection.jsonl  {len(selection)}")

    if stats["dropped_contaminated"]:
        print(f"\n  NOTE: dropped {stats['dropped_contaminated']} queries that collided "
              f"with the held-out benchmark corpus.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
