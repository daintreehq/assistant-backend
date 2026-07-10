"""Generic ``cases/*.yaml`` loader shared by the suites.

Each suite validates the raw case dicts against its own schema; this only reads
the files, enforces a top-level ``cases:`` list, and rejects duplicate ids.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class CaseError(Exception):
    """A malformed benchmark case file."""


def load_yaml_cases(cases_dir: Path, id_filter: str | None = None) -> list[dict[str, Any]]:
    """Load every ``cases/*.yaml`` file's ``cases:`` list into raw dicts.

    Validation of individual cases is the caller's job (each suite knows its own
    fields); here we only guarantee well-formed files and unique string ids.
    """
    import yaml

    files = sorted(cases_dir.glob("*.yaml"))
    if not files:
        raise CaseError(f"no case files under {cases_dir}")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in files:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
            raise CaseError(f"{path.name}: expected a top-level 'cases' list")
        for raw in data["cases"]:
            if not isinstance(raw, dict):
                raise CaseError(f"{path.name}: each case must be a mapping")
            cid = raw.get("id")
            if not cid or not isinstance(cid, str):
                raise CaseError(f"{path.name}: a case is missing a string 'id'")
            if cid in seen:
                raise CaseError(f"duplicate case id {cid!r} in {path.name}")
            seen.add(cid)
            raw.setdefault("_source", path.name)
            out.append(raw)
    if id_filter:
        needle = id_filter.lower()
        out = [c for c in out if needle in c["id"].lower()
               or needle in str(c.get("category", "")).lower()]
        if not out:
            raise CaseError(f"filter {id_filter!r} matched no cases")
    return out
