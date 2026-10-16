"""The shipped selector must serve the documents it was trained on, the same way every time.

Two failures, both silent, both found by measurement rather than by any error:

1. **Truncated documents.** Runbook documents are embedded under the artifact's
   ``doc_max_length``, and a longer one loses its END — which is exactly where a
   ``whenToUse`` keeps its "do not use for X, use Y" fences, the text that separates
   neighbouring runbooks. ``watch-outcome`` reached 510 tokens against a 384 cap and
   two more reached 398, so all three routed on a description with its boundaries
   cut off.
2. **Batch-dependent vectors.** The int8 encoders quantise activations dynamically,
   over the whole input tensor, so a document embedded in a batch came out 6-7%
   different (relative L2) from the same document embedded alone — and which other
   documents shared its batch depended on cache history. ``sync`` now encodes one
   document per call; this pins it.
"""

from __future__ import annotations

import json

import numpy as np
import pytest
from tokenizers import Tokenizer

from daintree_assistant_server.runbooks.catalog import default_runbooks_dir, load_catalog
from daintree_assistant_server.runbooks.local.documents import runbook_document
from daintree_assistant_server.runbooks.local.paths_local import selector_artifact_dir

_ARTIFACT = selector_artifact_dir()


def _artifact_present() -> bool:
    head = _ARTIFACT / "head.onnx"
    # Absent until `training/fetch_selector.sh` has run; the real graph is megabytes.
    return head.exists() and head.stat().st_size > 1_000_000


@pytest.mark.skipif(not _artifact_present(), reason="selector model not fetched (training/fetch_selector.sh)")
def test_every_runbook_document_fits_the_shipped_document_cap() -> None:
    meta = json.loads((_ARTIFACT / "meta.json").read_text())
    cap = int(meta["doc_max_length"])
    catalog = load_catalog(default_runbooks_dir())
    over: list[str] = []
    for stage in ("retriever", "ranking"):
        tok = Tokenizer.from_file(str(_ARTIFACT / stage / "tokenizer.json"))
        tok.no_truncation()
        tok.no_padding()
        for sid in catalog.selectable_ids:
            doc = runbook_document(catalog.get(sid).meta, variant=meta.get("doc_variant", "full"))
            n = len(tok.encode(doc).ids)
            if n > cap:
                over.append(f"{stage}: {sid} is {n} tokens")
    assert not over, (
        f"runbook documents longer than the selector's doc_max_length ({cap}) lose their "
        "END — the whenToUse exclusion fences — silently. Shorten the runbook's "
        "title/summary/whenToUse, or retrain with a larger cap:\n  " + "\n  ".join(over)
    )


@pytest.mark.skipif(not _artifact_present(), reason="selector model not fetched (training/fetch_selector.sh)")
def test_a_runbook_vector_does_not_depend_on_what_it_was_synced_with() -> None:
    from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier

    catalog = load_catalog(default_runbooks_dir())
    from daintree_assistant_server.runbooks.catalog import Catalog

    # Every seventh runbook, closed over `requires`: a sampled runbook that requires
    # another (a domain foundation requires daintree.foundation) refuses to load alone.
    by_id = {r.id: r for r in catalog.runbooks}
    picked = {r.id for r in catalog.runbooks[::7]}
    pending = list(picked)
    while pending:
        for ref in by_id[pending.pop()].meta.requires:
            if ref not in picked:
                picked.add(ref)
                pending.append(ref)
    narrow = Catalog([r for r in catalog.runbooks if r.id in picked])
    assert 0 < len(narrow) < len(catalog), "fixture must actually narrow the catalog"

    def fresh_table(cat):  # type: ignore[no-untyped-def]
        clf = SetwiseClassifier(_ARTIFACT)
        # Forget the shipped table and the memo so every document is ENCODED here,
        # which is the path whose batching used to leak into the vectors.
        clf._cached = None
        clf._vec_memo.clear()
        return clf.sync(cat)

    full = fresh_table(catalog)
    alone = fresh_table(narrow)
    for i, sid in enumerate(alone.ids):
        j = full.ids.index(sid)
        np.testing.assert_allclose(alone.ranking[i], full.ranking[j], rtol=0, atol=1e-5)
        np.testing.assert_allclose(alone.retrieval[i], full.retrieval[j], rtol=0, atol=1e-5)
