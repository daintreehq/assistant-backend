"""Score a proposed runbook-TEXT edit against the whole benchmark, without retraining.

Under the fixed-head classifier, editing a runbook's ``whenToUse`` moved
``routing_revision`` and needed a retrain before it took effect — so tuning
routing by text was a ~20-minute loop and nobody did it casually. The two-stage
selector re-embeds that one runbook on catalog load, so the same edit is live
immediately and costs one forward pass. Text is now the FAST routing lever.

Which makes the dangerous part the measurement, not the change. ``EXPERIMENTS.md``
records a carve-out that was locally correct and globally wrong: adding "do not
use to create a PLAIN worktree" to ``recipe.run-or-create`` fixed the case it was
written for and broke a neighbour, because it fenced on words the two runbooks
legitimately share. A change that helps the case in front of you and quietly
costs two others is the normal failure here, and it is invisible unless the whole
suite is re-scored.

So: propose the edit, run this, read BOTH columns. Nothing is written to disk —
the edit lives in an in-memory catalog overlay — so a bad idea costs nothing.

    python -m training.try_runbook_text \\
        --runbook daintree.review.review-change \\
        --when-to-use-file /tmp/proposed.txt

``--summary-file`` edits the summary instead; both may be given. The tool reports
every case whose answer changed, in both directions, and the net.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from benchmarks.runbook_selection.cases import load_cases  # noqa: E402
from daintree_assistant_server.runbooks.catalog import (  # noqa: E402
    default_runbooks_dir,
    load_catalog,
)
from daintree_assistant_server.runbooks.local.documents import query_document  # noqa: E402
from daintree_assistant_server.runbooks.local.paths_local import (  # noqa: E402
    selector_artifact_dir,
)
from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier  # noqa: E402


class _Meta:
    """One runbook's routing surface, with a field or two replaced."""

    def __init__(self, base: Any, *, summary: str | None, when_to_use: str | None) -> None:
        self.id = base.id
        self.title = base.title
        self.summary = summary if summary is not None else base.summary
        self.when_to_use = when_to_use if when_to_use is not None else base.when_to_use
        self.foundation = base.foundation


class _Runbook:
    def __init__(self, base: Any, meta: _Meta) -> None:
        self.id = base.id
        self.meta = meta


class EditedCatalog:
    """The live catalog with ONE runbook's routing text replaced, in memory only.

    Duck-typed rather than a real ``Catalog``: the selector needs exactly
    ``selectable_ids``, ``get`` and ``revision``, and building a real catalog
    would mean writing the proposed text to disk — which is the thing this tool
    exists to avoid.
    """

    def __init__(
        self, base: Any, runbook_id: str, *, summary: str | None, when_to_use: str | None
    ) -> None:
        self._base = base
        self._id = runbook_id
        self._runbook = _Runbook(
            base.get(runbook_id),
            _Meta(base.get(runbook_id).meta, summary=summary, when_to_use=when_to_use),
        )
        self.selectable_ids = base.selectable_ids
        # A distinct revision so ``sync`` cannot serve the unedited table from
        # its fast path. The digest check would catch it anyway; this keeps the
        # tool honest at the level above.
        self.revision = f"{base.revision}-proposed"

    def get(self, runbook_id: str) -> Any:
        return self._runbook if runbook_id == self._id else self._base.get(runbook_id)


def picks(clf: SetwiseClassifier, cases: Any, catalog: Any) -> list[list[str]]:
    selectable = catalog.selectable_ids
    out = []
    for case in cases:
        eligible = [m for m in case.messages if m.role in ("user", "assistant", "tool")]
        if case.active and eligible and eligible[-1].role != "user":
            out.append(list(case.active))
            continue
        query = query_document(case.messages, turn_context=case.turn_context)
        visible = [sid for sid in case.active if sid in selectable]
        out.append(
            [s for s in clf.select(query, visible, catalog=catalog).runbook_ids
             if s in selectable]
        )
    return out


def passed(case: Any, picked: list[str]) -> bool:
    got = set(picked)
    miss = set(case.required) - got
    extra = got - set(case.required) - set(case.optional)
    return not miss and (bool(case.required) or not extra)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runbook", required=True)
    ap.add_argument("--when-to-use-file")
    ap.add_argument("--summary-file")
    ap.add_argument("--artifact", default=str(selector_artifact_dir()))
    args = ap.parse_args()

    if not args.when_to_use_file and not args.summary_file:
        raise SystemExit("give --when-to-use-file and/or --summary-file")

    catalog = load_catalog(default_runbooks_dir())
    if args.runbook not in catalog.selectable_ids:
        raise SystemExit(f"{args.runbook} is not a selectable runbook")
    cases = load_cases(catalog)

    when = Path(args.when_to_use_file).read_text().strip() if args.when_to_use_file else None
    summary = Path(args.summary_file).read_text().strip() if args.summary_file else None
    proposed = EditedCatalog(catalog, args.runbook, summary=summary, when_to_use=when)

    clf = SetwiseClassifier(Path(args.artifact))
    before_table = clf.sync(catalog)
    before = picks(clf, cases, catalog)
    after_table = clf.sync(proposed)
    after = picks(clf, cases, proposed)

    moved = [s for s in after_table.ids if before_table.digests[s] != after_table.digests[s]]
    print(f"re-embedded {len(moved)} of {len(after_table.ids)} runbooks: {moved}")

    b_pass = sum(passed(c, p) for c, p in zip(cases, before, strict=True))
    a_pass = sum(passed(c, p) for c, p in zip(cases, after, strict=True))

    fixed, broken, churn = [], [], []
    for case, b, a in zip(cases, before, after, strict=True):
        if b == a:
            continue
        pb, pa = passed(case, b), passed(case, a)
        row = (case.id, b, a)
        (fixed if (pa and not pb) else broken if (pb and not pa) else churn).append(row)

    def show(title: str, rows: list[Any]) -> None:
        if not rows:
            return
        print(f"\n{title} ({len(rows)}):")
        for cid, b, a in rows:
            short = lambda xs: [x.rsplit(".", 1)[-1] for x in xs]  # noqa: E731
            print(f"  {cid:42} {short(b)} -> {short(a)}")

    show("FIXED", fixed)
    show("BROKEN", broken)
    show("changed but still same verdict", churn)

    print(
        f"\nrecall-pass {b_pass}/{len(cases)} -> {a_pass}/{len(cases)}  "
        f"(net {a_pass - b_pass:+d}; {len(fixed)} fixed, {len(broken)} broken)"
    )
    # A net-zero edit that churns a dozen cases is not neutral — it is a
    # reshuffle inside the noise band, and adopting it on a +1 is how a suite
    # gets fitted. Say so rather than printing a bare number.
    if fixed and broken:
        print(
            "NOTE: this edit trades cases in both directions. A carve-out that "
            "fences on words two runbooks legitimately share is a regression, not "
            "a clarification — see training/EXPERIMENTS.md."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
