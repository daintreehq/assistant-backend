"""Load and validate runbook markdown files.

Each runbook is ``<id>.md`` with a YAML frontmatter block followed by a Markdown
body. Loading is strict: a malformed runbook raises :class:`RunbookLoadError`,
which the startup path turns into a readiness failure.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .models import Runbook, RunbookMeta

MAX_META_BYTES = 16 * 1024
MAX_BODY_BYTES = 256 * 1024


class RunbookLoadError(Exception):
    """Raised when a runbook file is missing required structure or fails validation."""


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(yaml_text, body)`` or raise if the structure is wrong."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise RunbookLoadError("missing opening frontmatter delimiter '---'")
    closing: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            closing = i
            break
    if closing is None:
        raise RunbookLoadError("missing closing frontmatter delimiter '---'")
    yaml_text = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1 :]).strip("\n")
    return yaml_text, body


def parse_runbook(text: str, *, expected_id: str | None = None) -> Runbook:
    """Parse normalized runbook text into a validated :class:`Runbook`."""
    text = _normalize_newlines(text)
    yaml_text, body = _split_frontmatter(text)

    if len(yaml_text.encode("utf-8")) > MAX_META_BYTES:
        raise RunbookLoadError("frontmatter exceeds size limit")
    if not body.strip():
        raise RunbookLoadError("runbook body is empty")
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise RunbookLoadError("runbook body exceeds size limit")

    try:
        data = yaml.safe_load(yaml_text)
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        # NOT just YAMLError: PyYAML raises straight out of its constructors for
        # some inputs — `title: 2025-02-29` is a bare ValueError, deep nesting a
        # RecursionError. Either one escaping means an ordinary authoring typo
        # reads as a server fault instead of "your frontmatter is wrong", which
        # is exactly the promise this module's strictness is supposed to make.
        raise RunbookLoadError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise RunbookLoadError("frontmatter must be a YAML mapping")

    try:
        meta = RunbookMeta.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise RunbookLoadError(f"invalid runbook metadata: {exc}") from exc

    if expected_id is not None and meta.id != expected_id:
        raise RunbookLoadError(f"runbook id {meta.id!r} does not match filename id {expected_id!r}")

    # The hash is derived inside Runbook.__post_init__ — see models.py. It used to
    # be computed here and passed in, which left `dataclasses.replace` able to
    # produce a runbook whose stored hash described a body it no longer had.
    return Runbook(meta=meta, body=body)


def load_runbook_file(path: Path) -> Runbook:
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise RunbookLoadError(f"{path.name}: contains NUL bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RunbookLoadError(f"{path.name}: invalid UTF-8") from exc
    try:
        return parse_runbook(text, expected_id=path.stem)
    except RunbookLoadError as exc:
        raise RunbookLoadError(f"{path.name}: {exc}") from exc


def load_runbooks_dir(directory: Path) -> list[Runbook]:
    """Load a catalog tree, sorted by id, independent of domain directories.

    Only runbook files belong under this root. Keeping archived generations
    outside it prevents a recursive load from reviving retired procedures.
    """
    if not directory.is_dir():
        raise RunbookLoadError(f"runbooks directory not found: {directory}")
    runbooks: list[Runbook] = []
    for path in sorted(directory.rglob("*.md")):
        runbooks.append(load_runbook_file(path))
    if not runbooks:
        raise RunbookLoadError(f"runbooks directory is empty: {directory}")
    return sorted(runbooks, key=lambda s: s.id)
