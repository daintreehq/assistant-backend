"""Load and validate skill markdown files.

Each skill is ``<id>.md`` with a YAML frontmatter block followed by a Markdown
body. Loading is strict: a malformed skill raises :class:`SkillLoadError`,
which the startup path turns into a readiness failure.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from .models import Skill, SkillMeta

MAX_META_BYTES = 16 * 1024
MAX_BODY_BYTES = 256 * 1024


class SkillLoadError(Exception):
    """Raised when a skill file is missing required structure or fails validation."""


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(yaml_text, body)`` or raise if the structure is wrong."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise SkillLoadError("missing opening frontmatter delimiter '---'")
    closing: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            closing = i
            break
    if closing is None:
        raise SkillLoadError("missing closing frontmatter delimiter '---'")
    yaml_text = "\n".join(lines[1:closing])
    body = "\n".join(lines[closing + 1 :]).strip("\n")
    return yaml_text, body


def parse_skill(text: str, *, expected_id: str | None = None) -> Skill:
    """Parse normalized skill text into a validated :class:`Skill`."""
    text = _normalize_newlines(text)
    yaml_text, body = _split_frontmatter(text)

    if len(yaml_text.encode("utf-8")) > MAX_META_BYTES:
        raise SkillLoadError("frontmatter exceeds size limit")
    if not body.strip():
        raise SkillLoadError("skill body is empty")
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise SkillLoadError("skill body exceeds size limit")

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise SkillLoadError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillLoadError("frontmatter must be a YAML mapping")

    try:
        meta = SkillMeta.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise SkillLoadError(f"invalid skill metadata: {exc}") from exc

    if expected_id is not None and meta.id != expected_id:
        raise SkillLoadError(
            f"skill id {meta.id!r} does not match filename id {expected_id!r}"
        )

    content_hash = _content_hash(meta, body)
    return Skill(meta=meta, body=body, content_hash=content_hash)


def _content_hash(meta: SkillMeta, normalized_body: str) -> str:
    canonical = json.dumps(
        meta.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(
        (canonical + "\n---\n" + normalized_body).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def load_skill_file(path: Path) -> Skill:
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise SkillLoadError(f"{path.name}: contains NUL bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillLoadError(f"{path.name}: invalid UTF-8") from exc
    try:
        return parse_skill(text, expected_id=path.stem)
    except SkillLoadError as exc:
        raise SkillLoadError(f"{path.name}: {exc}") from exc


def load_skills_dir(directory: Path) -> list[Skill]:
    """Load every ``*.md`` skill in a directory, sorted by id."""
    if not directory.is_dir():
        raise SkillLoadError(f"skills directory not found: {directory}")
    skills: list[Skill] = []
    for path in sorted(directory.glob("*.md")):
        skills.append(load_skill_file(path))
    return sorted(skills, key=lambda s: s.id)
