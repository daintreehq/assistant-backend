"""The learn-from-transcript skill's digest: search extraction and redaction.

The script lives with its skill (`.claude/skills/learn-from-transcript/scripts/`),
so it is loaded by path. What is pinned is what the skill leans on: every
`search_runbooks` call is recovered with its query and ranking from both
transcript shapes, and nothing secret survives into the digest.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".claude/skills/learn-from-transcript/scripts/transcript_digest.py"
)

_RESULT = {
    "catalog_revision": "sha256:abc123",
    "runbooks": [
        {
            "id": "daintree.terminals.rename",
            "required_tools": ["context.snapshot", "terminal.extract.json", "terminal.rename"],
            "score": 0.9998,
            "selected": True,
            "instructions": "1. Rename.",
        },
        {"id": "daintree.foundation", "required_tools": [], "score": 0.01, "selected": False},
    ],
}


@pytest.fixture(scope="module")
def td() -> ModuleType:
    spec = importlib.util.spec_from_file_location("transcript_digest", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["transcript_digest"] = module
    spec.loader.exec_module(module)
    return module


def _write(tmp_path: Path, rows: list[dict[str, Any]]) -> str:
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return str(path)


def _claude(role: str, content: list[dict[str, Any]] | str) -> dict[str, Any]:
    return {"type": role, "timestamp": "2026-09-25T10:00:00Z", "message": {"content": content}}


def _codex(item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "response_item", "timestamp": "2026-09-25T10:00:00Z", "payload": item}


def _digest(td: ModuleType, path: str) -> str:
    return str(td.digest(path, max_output=1500, max_input=1200, include_injected=False))


def test_a_claude_search_is_recovered_with_its_ranking_and_follow_through(
    td: ModuleType, tmp_path: Path
) -> None:
    path = _write(
        tmp_path,
        [
            _claude("user", "rename my terminals"),
            _claude("assistant", [{
                "type": "tool_use", "id": "s1", "name": "mcp__daintree-runbooks__search_runbooks",
                "input": {"query": "rename the terminal tabs", "max_results": 2},
            }]),
            _claude("user", [{
                "type": "tool_result", "tool_use_id": "s1",
                "content": [{"type": "text", "text": json.dumps(_RESULT)}],
            }]),
            _claude("assistant", [{
                "type": "tool_use", "id": "r1", "name": "mcp__daintree__terminal_rename",
                "input": {"terminalId": "t1", "name": "build"},
            }]),
        ],
    )
    out = _digest(td, path)
    assert "rename the terminal tabs" in out
    assert "max_results 2" in out
    assert "| 1 | `daintree.terminals.rename` | 0.9998 | **yes** |" in out
    assert "called after this search: terminal.rename" in out
    assert "never called: context.snapshot, terminal.extract.json" in out
    assert "`sha256:abc123`" in out


def test_a_codex_function_call_search_is_recovered_from_the_tool_result(
    td: ModuleType, tmp_path: Path
) -> None:
    wrapped = {"content": [{"type": "text", "text": json.dumps(_RESULT)}], "isError": False}
    path = _write(
        tmp_path,
        [
            _codex({"type": "message", "role": "user", "content": "rename them"}),
            _codex({
                "type": "function_call", "call_id": "c1",
                "name": "mcp__daintree_runbooks__search_runbooks",
                "arguments": json.dumps({"query": "retitle my agents"}),
            }),
            _codex({"type": "function_call_output", "call_id": "c1", "output": json.dumps(wrapped)}),
        ],
    )
    [search] = _search_list(td, path)
    assert search.query == "retitle my agents"
    assert search.results[0]["runbooks"][0]["id"] == "daintree.terminals.rename"
    assert search.error is None


def test_a_search_inside_a_codex_exec_cell_is_recovered(td: ModuleType, tmp_path: Path) -> None:
    cell = "const r = await tools.mcp__daintree_runbooks__search_runbooks("
    cell += "{query: \"give each agent a proper title\", max_results: 3}); text(r);"
    printed = json.dumps({"output": json.dumps({"structuredContent": _RESULT})})
    path = _write(
        tmp_path,
        [
            _codex({"type": "message", "role": "user", "content": "title them"}),
            _codex({"type": "custom_tool_call", "call_id": "e1", "name": "exec", "input": cell}),
            _codex({"type": "custom_tool_call_output", "call_id": "e1", "output": printed}),
        ],
    )
    [search] = _search_list(td, path)
    assert search.query == "give each agent a proper title"
    assert search.max_results == 3
    assert search.results and search.results[0]["catalog_revision"] == "sha256:abc123"


def test_a_failed_search_reports_its_error(td: ModuleType, tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        [
            _claude("assistant", [{
                "type": "tool_use", "id": "s1", "name": "mcp__runbooks__search_runbooks",
                "input": {"query": "x"},
            }]),
            _claude("user", [{
                "type": "tool_result", "tool_use_id": "s1", "is_error": True,
                "content": "Runbook search is temporarily unavailable. Try again shortly.",
            }]),
        ],
    )
    assert "**error:** Runbook search is temporarily unavailable" in _digest(td, path)


def test_a_session_without_a_search_says_so(td: ModuleType, tmp_path: Path) -> None:
    path = _write(tmp_path, [_claude("user", "close the idle terminals")])
    assert "No `search_runbooks` call in this session" in _digest(td, path)


def test_secrets_are_redacted_before_anything_is_clipped(td: ModuleType, tmp_path: Path) -> None:
    key = "sk-ant-" + "A1b2C3d4" * 6
    path = _write(
        tmp_path,
        [
            _claude("user", "mail someone@example.com about it"),
            _claude("assistant", [{
                "type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "env"},
            }]),
            _claude("user", [{
                "type": "tool_result", "tool_use_id": "b1",
                "content": f'{{"api_key": "{key}"}} ' + "x" * 40,
            }]),
        ],
    )
    out = str(td.digest(path, max_output=30, max_input=1200, include_injected=False))
    assert key not in out and key[:20] not in out
    assert "someone@example.com" not in out and "<email>" in out


def test_mcp_tool_names_map_to_the_dotted_form_runbooks_write(td: ModuleType) -> None:
    assert td.runbook_tool("mcp__daintree__terminal_sendCommand") == "terminal.sendCommand"
    assert td.runbook_tool("terminal__extract__json") == "terminal.extract.json"
    assert td.runbook_tool("Bash") == "Bash"


def _search_list(td: ModuleType, path: str) -> list[Any]:
    rows, _ = td.load_jsonl(path)
    _, events = td.parse_codex(rows)
    td.number_steps(events)
    return list(td.find_searches(events))


# ── regressions: each reproduces a failure the first version had ────────────


def _real_body() -> str:
    body = Path(__file__).resolve().parents[2] / (
        "src/daintree_assistant_server/runbooks/files/daintree.terminals.rename.md"
    )
    return body.read_text()


def test_a_wrapped_result_with_a_real_runbook_body_is_recovered(
    td: ModuleType, tmp_path: Path
) -> None:
    """Escaped newlines and quotes inside `instructions` survive decoding."""
    result = json.loads(json.dumps(_RESULT))
    result["runbooks"][0]["instructions"] = _real_body()
    cell = 'await tools.mcp__runbooks__search_runbooks({"query": "rename \\"these\\" tabs"})'
    printed = json.dumps({"output": json.dumps(result)})
    path = _write(
        tmp_path,
        [
            _codex({"type": "custom_tool_call", "call_id": "e1", "name": "exec", "input": cell}),
            _codex({"type": "custom_tool_call_output", "call_id": "e1", "output": printed}),
        ],
    )
    [search] = _search_list(td, path)
    assert search.query == 'rename "these" tabs'
    assert search.results[0]["runbooks"][0]["instructions"] == _real_body()


def test_a_cell_whose_results_cannot_be_paired_pairs_nothing(
    td: ModuleType, tmp_path: Path
) -> None:
    """Two searches, one printed result: guessing would hand one query the other's answer."""
    cell = (
        "try { await tools.mcp__r__search_runbooks({query: 'first'}) } catch (e) {}\n"
        "text(await tools.mcp__r__search_runbooks({query: 'second'}));"
    )
    path = _write(
        tmp_path,
        [
            _codex({"type": "custom_tool_call", "call_id": "e1", "name": "exec", "input": cell}),
            _codex({
                "type": "custom_tool_call_output", "call_id": "e1", "output": json.dumps(_RESULT),
            }),
        ],
    )
    first, second = _search_list(td, path)
    assert (first.query, second.query) == ("first", "second")
    assert first.results == [] and second.results == []
    assert first.note and "unknown" in first.note


def test_quoted_secret_values_are_redacted_whole(td: ModuleType) -> None:
    for text in (
        '{"password": "my secret passphrase"}',
        '{"db_password": "abcdef,ghijkl"}',
        '{\\"api_key\\": \\"short one\\"}',
        '{"my_app_api_key": "several words here"}',
        '{\\"password\\": \\"a \\\\\\"quoted\\\\\\" tail\\"}',
    ):
        out = td.redact(text)
        for fragment in ("secret passphrase", "ghijkl", "short one", "words here", "tail"):
            assert fragment not in out, (text, out)


def test_claude_injected_text_is_kept_for_include_injected(
    td: ModuleType, tmp_path: Path
) -> None:
    reminder = "<system-reminder>deferred tools: mcp__runbooks__search_runbooks</system-reminder>"
    path = _write(tmp_path, [_claude("user", reminder + " rename my tabs")])
    out = str(td.digest(path, max_output=1500, max_input=1200, include_injected=True))
    assert "deferred tools: mcp__runbooks__search_runbooks" in out


def test_malformed_records_do_not_crash_the_digest(td: ModuleType, tmp_path: Path) -> None:
    odd = dict(_RESULT, catalog_revision=["not", "a", "string"])
    path = _write(
        tmp_path,
        [
            _claude("user", [{"type": "text", "text": 5}, {"type": "text", "text": "hi"}]),
            {"type": "assistant", "message": {"id": {"not": "hashable"}, "usage": {},
                                               "content": [{"type": "text", "text": "ok"}]}},
            _claude("assistant", [
                {"type": "text", "text": {"nested": True}},
                {"type": "tool_use", "id": "s1", "name": "mcp__r__search_runbooks",
                 "input": {"query": "x", "max_results": True}},
            ]),
            _claude("user", [{
                "type": "tool_result", "tool_use_id": "s1", "content": json.dumps(odd),
            }]),
        ],
    )
    out = _digest(td, path)
    assert "Served from catalog_revision: unknown" in out


def test_a_call_tool_result_counts_once_whatever_wraps_its_document(td: ModuleType) -> None:
    wrapped = {"output": _RESULT}
    result = {
        "structuredContent": wrapped,
        "content": [{"type": "text", "text": json.dumps(wrapped)}],
    }
    assert len(td._search_docs(result)) == 1


def test_js_hex_escapes_decode(td: ModuleType) -> None:
    assert td._js_string("'", r"rename\x20tabs") == "rename tabs"
