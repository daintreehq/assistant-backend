"""Deterministic linting of the main model's final message.

Output-shape rules the prompt can only *ask* for — pipe-table width, visible
``<think>`` leakage, an oversized prompt-recitation dump — are checked HERE, in code.
On the non-stream path the orchestrator lints the final assistant message and LOGS any
violation (observability by default); optional, off-by-default repair deterministically
converts an over-budget pipe table to a per-entity list. Streaming is never touched —
its content is already on the wire — so this is a non-stream-only backstop.

Width is display-width aware: CJK/wide glyphs count double, combining marks zero. Emoji
are approximated as wide (Unicode East Asian Width leaves most of them Neutral, yet
terminals render them double-width), so a table padded with emoji is still caught.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# A fenced code block boundary (``` or ~~~). Lines inside a fence are exempt.
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# A GFM table delimiter row: ``|---|:--:|`` — at least two columns (one interior pipe),
# each cell ≥ 2 dashes so a lone ``- item`` list bullet is never mistaken for one.
_DELIM_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)+\|?\s*$")
_THINK_RE = re.compile(r"</?think\s*>", re.IGNORECASE)


def _is_wide(ch: str) -> bool:
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return True
    o = ord(ch)
    # Emoji / pictographic blocks terminals render double-width but Unicode marks Neutral.
    return 0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF


def display_width(line: str) -> int:
    """Terminal display width of one line (combining marks = 0, wide glyphs = 2)."""
    width = 0
    for ch in line:
        if unicodedata.combining(ch):
            continue
        width += 2 if _is_wide(ch) else 1
    return width


def _split_cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


@dataclass(frozen=True)
class PipeTable:
    """A located GFM pipe table (line indices into the split text)."""

    start: int  # header line index (0-based)
    end: int  # exclusive — one past the last body line
    columns: int
    rows: int  # body rows (excludes header + delimiter)
    width: int  # max display width across all the table's lines


def find_pipe_tables(text: str) -> list[PipeTable]:
    """Locate every pipe table outside fenced code."""
    lines = text.split("\n")
    tables: list[PipeTable] = []
    in_fence = False
    i = 0
    n = len(lines)
    while i < n:
        if _FENCE_RE.match(lines[i]):
            in_fence = not in_fence
            i += 1
            continue
        if not in_fence and i > 0 and _DELIM_RE.match(lines[i]) and "|" in lines[i - 1]:
            header = i - 1
            columns = len(_split_cells(lines[i]))
            j = i + 1
            while j < n and lines[j].strip() and "|" in lines[j] and not _FENCE_RE.match(lines[j]):
                j += 1
            width = max(display_width(lines[k]) for k in range(header, j))
            tables.append(
                PipeTable(start=header, end=j, columns=columns, rows=j - (i + 1), width=width)
            )
            i = j
            continue
        i += 1
    return tables


@dataclass(frozen=True)
class TableFinding:
    table: PipeTable
    over_columns: bool
    over_rows: bool
    over_width: bool

    @property
    def violates(self) -> bool:
        return self.over_columns or self.over_rows or self.over_width


@dataclass(frozen=True)
class OutputFindings:
    char_count: int
    think_leak: bool
    oversized: bool
    tables: tuple[TableFinding, ...]

    @property
    def violating_tables(self) -> tuple[TableFinding, ...]:
        return tuple(t for t in self.tables if t.violates)

    @property
    def has_violation(self) -> bool:
        return self.think_leak or self.oversized or bool(self.violating_tables)

    def violation_types(self) -> list[str]:
        out: list[str] = []
        if self.violating_tables:
            out.append("pipe_table")
        if self.think_leak:
            out.append("think_leak")
        if self.oversized:
            out.append("oversized")
        return out


def lint_output(
    text: str,
    *,
    terminal_width: int,
    warn_chars: int,
    max_columns: int = 3,
    max_rows: int = 5,
) -> OutputFindings:
    """Lint a final assistant message against the terminal output contract."""
    tables = tuple(
        TableFinding(
            table=t,
            over_columns=t.columns > max_columns,
            over_rows=t.rows > max_rows,
            over_width=t.width > terminal_width,
        )
        for t in find_pipe_tables(text)
    )
    return OutputFindings(
        char_count=len(text),
        think_leak=bool(_THINK_RE.search(text)),
        oversized=len(text) > warn_chars,
        tables=tables,
    )


def _table_to_list(lines: list[str], table: PipeTable) -> list[str]:
    """Render one pipe table as per-entity list lines: `**first** — rest · rest`."""
    out: list[str] = []
    for r in range(table.start + 2, table.end):  # skip header + delimiter rows
        cells = [c for c in _split_cells(lines[r]) if c and c != "—"]
        if not cells:
            continue
        name, rest = cells[0], cells[1:]
        out.append(f"- **{name}** — " + " · ".join(rest) if rest else f"- **{name}**")
    return out


def repair_tables(text: str, findings: OutputFindings) -> str:
    """Replace each VIOLATING pipe table with a per-entity list.

    Compliant tables and fenced code are left untouched. Returns the text unchanged
    when nothing violates (so a caller can cheaply detect a no-op with ``is``/``==``).
    """
    violating = findings.violating_tables
    if not violating:
        return text
    lines = text.split("\n")
    # Replace bottom-up so earlier line indices stay valid as we splice.
    for finding in sorted(violating, key=lambda f: f.table.start, reverse=True):
        t = finding.table
        replacement = _table_to_list(lines, t)
        if replacement:  # never blank out a table we couldn't parse into rows
            lines[t.start : t.end] = replacement
    return "\n".join(lines)
