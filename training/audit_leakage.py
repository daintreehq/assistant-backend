"""Screen curated queries AND history against every evaluation suite.

    python -m training.audit_leakage --near

Exit 1 on overlap. Output identifies training rows only: never print evaluation
text or its labels to a data writer. Exact screening also runs at compile time;
near screening belongs before a data batch lands, not inside every compile.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from collections.abc import Iterator
from difflib import SequenceMatcher

from daintree_assistant_server.runbooks.catalog import default_runbooks_dir, load_catalog
from training.compile_corpus import _benchmark_strings, norm
from training.corpus_source import Example, load_examples

NEAR_THRESHOLD = 0.8


def _candidate_pairs(texts: list[str], protected: list[str]) -> Iterator[tuple[int, int]]:
    try:
        import numpy as np
        from rapidfuzz import fuzz, process
    except ImportError:
        # Functional without optional training dependencies, slower on the full
        # corpus. Both quick bounds upper-bound matching-block similarity.
        for i, text in enumerate(texts):
            for j, other in enumerate(protected):
                matcher = SequenceMatcher(None, text, other, autojunk=False)
                if (matcher.real_quick_ratio() > NEAR_THRESHOLD
                        and matcher.quick_ratio() > NEAR_THRESHOLD):
                    yield i, j
        return

    # LCS/Indel similarity is an upper bound on difflib's matching blocks. This
    # accelerates candidate discovery without losing any match above the cut.
    for start in range(0, len(texts), 512):
        scores = process.cdist(
            texts[start:start + 512], protected, scorer=fuzz.ratio,
            score_cutoff=100 * NEAR_THRESHOLD, dtype=np.float32, workers=2,
        )
        rows, cols = np.nonzero(scores)
        for i, j in zip(rows, cols, strict=True):
            yield start + int(i), int(j)


def find_overlaps(
    examples: list[Example], protected: set[str], *, near: bool = False
) -> list[dict]:
    """Return locations in training data, never matched evaluation strings."""
    locations: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for i, ex in enumerate(examples):
        locations[norm(ex.query)].append((i, "query"))
        for j, line in enumerate(ex.history):
            locations[norm(line.partition(":")[2] or line)].append((i, f"history[{j}]"))
    locations.pop("", None)

    matches = {t: ("exact", 1.0) for t in locations if t in protected}
    if near:
        texts = [t for t in locations if t not in protected]
        held_out = sorted(protected - {""})
        for i, j in _candidate_pairs(texts, held_out):
            text = texts[i]
            similarity = SequenceMatcher(None, text, held_out[j], autojunk=False).ratio()
            if similarity > NEAR_THRESHOLD and similarity > matches.get(text, ("", 0))[1]:
                matches[text] = ("near", similarity)

    out = []
    for text, (kind, similarity) in matches.items():
        for index, field in locations[text]:
            out.append({
                "example_index": index, "origin": examples[index].origin,
                "field": field, "kind": kind, "similarity": round(similarity, 6),
            })
    return sorted(out, key=lambda r: (r["example_index"], r["field"]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--near", action="store_true", help="also screen difflib similarity > 0.8")
    parser.add_argument("--json", action="store_true", help="emit training-row locations as JSON")
    args = parser.parse_args()
    examples = load_examples()
    protected = _benchmark_strings(load_catalog(default_runbooks_dir()))
    overlaps = find_overlaps(examples, protected, near=args.near)
    if args.json:
        print(json.dumps(overlaps, indent=2))
    else:
        affected = len({r["example_index"] for r in overlaps})
        print(f"{len(examples)} curated rows: {affected} overlap evaluation text")
        for origin, count in sorted(Counter(r["origin"] for r in overlaps).items()):
            print(f"  {origin}: {count} matching input field(s)")
    return int(bool(overlaps))


if __name__ == "__main__":
    raise SystemExit(main())
