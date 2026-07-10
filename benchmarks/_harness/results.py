"""Per-suite JSON results documents — the tracked record for every benchmark.

Each suite writes ``<suite_dir>/results/<timestamp>-<label>.json`` (results dirs
are gitignored). The document always carries a ``meta`` block (benchmark name,
model/mode, timestamps) and a ``summary`` block; the aggregator dashboard reads
these back.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def results_dir(suite_file: str) -> Path:
    """The ``results/`` dir next to a suite's module file."""
    d = Path(suite_file).resolve().parent / "results"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_doc(doc: dict[str, Any], suite_file: str, label: str, out: Path | None = None) -> Path:
    if out is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = results_dir(suite_file) / f"{stamp}-{label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return out


def load_doc(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def latest(suite_file: str, label: str | None = None) -> Path | None:
    """Most recent results file for a suite (optionally filtered by label)."""
    d = Path(suite_file).resolve().parent / "results"
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.json"))
    if label:
        files = [f for f in files if f.stem.endswith(f"-{label}")]
    return files[-1] if files else None
