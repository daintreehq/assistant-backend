"""Re-embed the current catalog with the shipped encoders, without training.

Write to a derived output by default. The immutable files in selector.lock are
left alone; installing a new cache there also requires publishing its new lock.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from daintree_assistant_server.runbooks.catalog import load_catalog
from daintree_assistant_server.runbooks.local.documents import routing_revision
from daintree_assistant_server.runbooks.local.paths_local import selector_artifact_dir
from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier, save_table


def refresh(artifact: Path, output: Path) -> dict[str, object]:
    catalog = load_catalog()
    classifier = SetwiseClassifier(artifact)
    table = classifier.sync(catalog)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        staged = Path(temporary) / "vectors.npz"
        save_table(staged, table)
        staged.replace(output)
    return {
        "catalog_revision": catalog.revision,
        "routing_revision": routing_revision(catalog),
        "runbooks": len(table.ids),
        "encoder_fingerprint": table.encoder_fingerprint,
        "output": str(output),
        "weights_changed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, default=selector_artifact_dir())
    parser.add_argument("--out", type=Path, default=Path(".tmp/runbooks/vectors.npz"))
    args = parser.parse_args()
    print(json.dumps(refresh(args.artifact, args.out), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
