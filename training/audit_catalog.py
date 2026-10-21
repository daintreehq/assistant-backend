"""Report catalog, corpus, encoder-cache and captured-app drift without training."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import yaml
from tokenizers import Tokenizer

from daintree_assistant_server.runbooks.catalog import load_catalog
from daintree_assistant_server.runbooks.local.documents import routing_revision, runbook_document
from daintree_assistant_server.runbooks.local.paths_local import selector_artifact_dir
from daintree_assistant_server.runbooks.local.setwise import (
    _load_table,
    document_digest,
    encoder_fingerprint,
)
from training.corpus_source import load_examples
from training.protocol import retired_inbox_reason

ROOT = Path(__file__).resolve().parents[1]


def audit(*, index: Path | None = None) -> dict[str, object]:
    catalog = load_catalog()
    artifact = selector_artifact_dir()
    meta = json.loads((artifact / "meta.json").read_text())
    labels = set(json.loads((artifact / "labels.json").read_text()))
    examples = load_examples()
    corpus_lock = json.loads((ROOT / "training/data/corpus-lock.json").read_text())
    expected = encoder_fingerprint(
        artifact, quantized=True, doc_max=meta["doc_max_length"], variant=meta["doc_variant"]
    )
    documents = {
        r.id: runbook_document(r.meta, variant=meta["doc_variant"])
        for r in catalog.runbooks
        if r.id in catalog.selectable_ids
    }
    digests = {sid: document_digest(doc) for sid, doc in documents.items()}
    cache_path = index or artifact / "vectors.npz"
    table = _load_table(cache_path) if cache_path.exists() else None
    token_counts: dict[str, dict[str, int]] = {}
    for stage in ("retriever", "ranking"):
        tokenizer = Tokenizer.from_file(str(artifact / stage / "tokenizer.json"))
        tokenizer.no_truncation()
        tokenizer.no_padding()
        token_counts[stage] = {
            sid: len(tokenizer.encode(doc).ids) for sid, doc in documents.items()
        }
    archive = ROOT / "archive/inbox-v0.44"
    captures = {
        p.stem: json.loads(p.read_text())
        for p in (ROOT / "tests/fixtures/daintree_mcp").glob("*.json")
    }
    return {
        "catalog": {
            "runbooks": len(catalog),
            "selectable": len(catalog.selectable_ids),
            "revision": catalog.revision,
            "routing_revision": routing_revision(catalog),
            "domains": dict(
                sorted(
                    Counter(
                        r.id.split(".")[1] if r.id.count(".") > 1 else "core"
                        for r in catalog.runbooks
                    ).items()
                )
            ),
        },
        "corpus": {
            "curated_rows": len(examples),
            "compiled_rows": corpus_lock["rows"],
            "compiled_sha256": corpus_lock["compiled_sha256"],
            "retired_protocol_rows": sum(
                retired_inbox_reason(e.query, e.history, (*e.expect, *e.active)) is not None
                for e in examples
            ),
        },
        "training": {
            "trained_label_count": len(labels),
            "untrained_ids": sorted(catalog.selectable_ids - labels),
            "retired_trained_ids": sorted(labels - catalog.selectable_ids),
            "routing_revision_at_train": meta.get("routing_revision_at_train"),
            "corpus_at_train": meta.get("training_corpus"),
            "retriever_corpus_at_train": meta.get("retriever_training_corpus"),
        },
        "index": {
            "path": str(cache_path),
            "expected_encoder_fingerprint": expected,
            "cached_encoder_fingerprint": table.encoder_fingerprint if table else None,
            "encoder_matches": table is not None and table.encoder_fingerprint == expected,
            "documents_match": table is not None and table.digests == digests,
            "rows": len(table.ids) if table else 0,
        },
        "document_budget": {
            "cap": meta["doc_max_length"],
            "maximum_by_stage": {
                stage: max(counts.values()) for stage, counts in token_counts.items()
            },
            "over_cap": {
                stage: {sid: n for sid, n in counts.items() if n > meta["doc_max_length"]}
                for stage, counts in token_counts.items()
            },
        },
        "app_capture": {
            "commits": sorted({data["capture"]["app_commit"] for data in captures.values()}),
            "versions": sorted({data["capture"]["app_version"] for data in captures.values()}),
            "tools_by_caller": {
                name: len(data["tools"]) for name, data in sorted(captures.items())
            },
        },
        "archive": {
            "inbox_overrides": len(list((archive / "runbooks").glob("*.md"))),
            "evaluation_cases": sum(
                len(yaml.safe_load(p.read_text())["cases"])
                for p in (archive / "cases").glob("*.yaml")
            ),
            "curated_rows": len((archive / "corpus-retired.jsonl").read_text().splitlines()),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--index", type=Path, help="inspect a rebuilt cache instead of the locked one"
    )
    args = parser.parse_args()
    print(json.dumps(audit(index=args.index), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
