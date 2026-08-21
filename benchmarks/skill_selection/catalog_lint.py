"""Which skills the SELECTOR finds confusable, and whether the catalog says so.

## The gap this fills

`bench coverage` already gates the routing graph the catalog DECLARES: a skill
whose whenToUse names a neighbour owes a trap case proving it can tell them
apart. That is the right gate and it has one blind spot — it can only see
confusions somebody thought to write down, in the spelling it can parse.

This looks from the other end. It embeds every routing document with the shipped
retriever and ranks the pairs by cosine, which is the selector's own opinion
about what looks alike. A pair the model finds close and the catalog says nothing
about is a confusion nobody has written a carve-out for and no trap tests.

That is how the catalog's 27 unqualified carve-outs were found. The closest
pairs with no id-level edge turned out to be almost exactly the ones whose
carve-out WAS written, in a spelling the coverage gate cannot parse — "use the
competition skill", "use competition" — so the pairs the retriever finds most
alike were the pairs its own tooling was blind to. This report is the worklist
for `test_every_carve_out_defers_to_a_fully_qualified_id`, ordered by which one
the model is most likely to get wrong.

## What "declared" here does and does not mean

It means an id-level edge: one of the two skills names the other by fully
qualified id, which is exactly what `bench coverage` parses — the same function,
`coverage.declared_neighbours`, rather than a second substring scan that could
drift from it. It does NOT mean the pair is tested. Coverage gates per SKILL
(a skill with any declared neighbour owes one trap case), not per edge, so a
newly named neighbour usually adds no new gating. Reading "declared" as "covered"
would overstate what the green column buys.

## Why it is a report and not a gate

Cosine between two routing documents has no absolute meaning: the live catalog's
closest pair is around 0.5, which would be unremarkable in another corpus, and it
moves whenever a whenToUse is edited. Any threshold would be a number invented to
match today's data. It is also a proxy rather than a measurement — two documents
scoring alike means their retrieval scores correlate, not that real queries pull
up both, and certainly not that the set-wise head (a different encoder, plus
query context and per-candidate features) confuses them. The gate that belongs in
CI is the fully-qualified-carve-out ratchet, which counts something exact. This
ranks what to look at.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from daintree_assistant_server.skills.catalog import Catalog, default_skills_dir, load_catalog
from daintree_assistant_server.skills.local.setwise import SetwiseClassifier

from .coverage import declared_neighbours
from .local_runner import ARTIFACT


@dataclass(frozen=True)
class Pair:
    """Two skills, how alike the retriever finds them, and who says so."""

    a: str
    b: str
    cosine: float
    #: True when either skill's whenToUse names the other by fully qualified id.
    #: That is an EDGE, not coverage: `bench coverage` gates per skill, so an
    #: id-level edge does not by itself demand a trap for this pair.
    declared: bool


def confusion_pairs(
    *, catalog: Catalog | None = None, artifact: Path | None = None
) -> list[Pair]:
    """Every skill pair, most confusable first."""
    catalog = catalog if catalog is not None else load_catalog(default_skills_dir())
    clf = SetwiseClassifier(artifact or ARTIFACT)
    table = clf.sync(catalog)

    sims = table.retrieval @ table.retrieval.T
    np.fill_diagonal(sims, -1.0)

    # The SAME parser the coverage gate uses, not a second substring scan. Two
    # readings of "does this skill name that one" would drift, and this report's
    # whole job is to say which pairs that parser cannot see.
    neighbours = declared_neighbours(catalog)

    pairs = [
        Pair(
            a=table.ids[i],
            b=table.ids[j],
            cosine=round(float(sims[i, j]), 4),
            declared=(
                table.ids[j] in neighbours.get(table.ids[i], ())
                or table.ids[i] in neighbours.get(table.ids[j], ())
            ),
        )
        for i in range(len(table.ids))
        for j in range(i + 1, len(table.ids))
    ]
    pairs.sort(key=lambda p: -p.cosine)
    return pairs


def print_lint(pairs: list[Pair], *, top: int = 20) -> dict[str, Any]:
    undeclared = [p for p in pairs if not p.declared]
    print(f"\nmost confusable skill pairs, by the retriever's own embedding ({len(pairs)} pairs)\n")
    print(f"  {'cosine':>7}  {'declared':<10}  pair")
    for pair in pairs[:top]:
        mark = "yes" if pair.declared else "NO — no id edge"
        print(
            f"  {pair.cosine:7.4f}  {mark:<10}  "
            f"{pair.a.split('.', 1)[-1]} <-> {pair.b.split('.', 1)[-1]}"
        )

    print(
        "\npairs with no id-level edge, closest first — neither skill names the "
        "other by\nfully qualified id, so `bench coverage` cannot see the "
        "confusion at all:"
    )
    for pair in undeclared[:top]:
        print(
            f"  {pair.cosine:7.4f}  {pair.a.split('.', 1)[-1]} <-> "
            f"{pair.b.split('.', 1)[-1]}"
        )

    return {
        "pairs_total": len(pairs),
        "undeclared_total": len(undeclared),
        "closest_cosine": pairs[0].cosine if pairs else 0.0,
        "closest_undeclared_cosine": undeclared[0].cosine if undeclared else 0.0,
    }
