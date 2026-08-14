"""Tests for the deterministic output linter and its orchestrator wiring."""

from __future__ import annotations

from daintree_assistant_server.config import Settings
from daintree_assistant_server.providers.base import ProviderChatResult, ProviderUsage
from daintree_assistant_server.services.chat_orchestrator import ChatOrchestrator
from daintree_assistant_server.services.output_policy import (
    display_width,
    find_pipe_tables,
    lint_output,
    repair_tables,
)

WIDE_TABLE = (
    "Here are the terminals:\n\n"
    "| Terminal | Agent | State | Location |\n"
    "|----------|-------|-------|----------|\n"
    "| Claude   | claude | directing | grid |\n"
    "| Codex    | codex  | waiting   | grid |\n"
)


# ---- display_width ----------------------------------------------------------


def test_display_width_ascii() -> None:
    assert display_width("hello") == 5


def test_display_width_wide_cjk() -> None:
    assert display_width("日本") == 4


def test_display_width_emoji_is_wide() -> None:
    # 🏆 renders double-width even though Unicode East Asian Width leaves it Neutral.
    assert display_width("🏆") == 2


def test_display_width_combining_is_zero() -> None:
    assert display_width("é") == 1  # e + combining acute


# ---- find_pipe_tables -------------------------------------------------------


def test_find_table_basic_shape() -> None:
    tables = find_pipe_tables(WIDE_TABLE)
    assert len(tables) == 1
    t = tables[0]
    assert t.columns == 4
    assert t.rows == 2
    assert t.width > 30


def test_list_bullets_are_not_tables() -> None:
    text = "- **Claude** — directing · grid\n- **Codex** — waiting · grid\n"
    assert find_pipe_tables(text) == []


def test_table_inside_code_fence_is_ignored() -> None:
    text = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```\n"
    assert find_pipe_tables(text) == []


def test_two_tables_located_separately() -> None:
    text = WIDE_TABLE + "\nand\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"
    tables = find_pipe_tables(text)
    assert len(tables) == 2


# ---- lint_output ------------------------------------------------------------


def test_lint_flags_over_columns() -> None:
    findings = lint_output(WIDE_TABLE, terminal_width=56, warn_chars=8000)
    assert findings.has_violation
    assert "pipe_table" in findings.violation_types()
    assert findings.violating_tables[0].over_columns  # 4 > 3


def test_lint_flags_over_width() -> None:
    text = (
        "| Path | Status | Note |\n"
        "|------|--------|------|\n"
        "| /very/long/path/to/some/file.py | modified | needs a rebase before merge |\n"
    )
    findings = lint_output(text, terminal_width=56, warn_chars=8000)
    tf = findings.violating_tables[0]
    assert tf.over_width
    assert not tf.over_columns  # 3 columns is within the cap; width is the problem


def test_small_compliant_table_passes() -> None:
    text = "**Tally**\n\n| Agent | Pts |\n|-------|-----|\n| Codex | 4 |\n| Grok | 2 |\n"
    findings = lint_output(text, terminal_width=56, warn_chars=8000)
    assert not findings.violating_tables
    assert not findings.has_violation


def test_lint_flags_think_leak() -> None:
    findings = lint_output("done</think> ok", terminal_width=56, warn_chars=8000)
    assert findings.think_leak
    assert findings.has_violation


def test_lint_flags_oversized() -> None:
    findings = lint_output("x" * 100, terminal_width=56, warn_chars=50)
    assert findings.oversized
    assert "oversized" in findings.violation_types()


def test_lint_flags_over_rows() -> None:
    rows = "".join(f"| r{i} | v{i} |\n" for i in range(8))
    text = "| A | B |\n|---|---|\n" + rows
    findings = lint_output(text, terminal_width=200, warn_chars=8000)
    assert findings.violating_tables[0].over_rows


# ---- repair_tables ----------------------------------------------------------


def test_repair_converts_violating_table_to_list() -> None:
    findings = lint_output(WIDE_TABLE, terminal_width=56, warn_chars=8000)
    out = repair_tables(WIDE_TABLE, findings)
    assert "|---" not in out
    assert "- **Claude** — claude · directing · grid" in out
    assert "- **Codex** — codex · waiting · grid" in out
    assert "Here are the terminals:" in out  # surrounding prose preserved


def test_repair_leaves_compliant_table() -> None:
    text = "**Tally**\n\n| Agent | Pts |\n|-------|-----|\n| Codex | 4 |\n"
    findings = lint_output(text, terminal_width=56, warn_chars=8000)
    assert repair_tables(text, findings) == text


def test_repair_leaves_fenced_table() -> None:
    text = "```\n| a | b | c | d |\n|---|---|---|---|\n| 1 | 2 | 3 | 4 |\n```\n"
    findings = lint_output(text, terminal_width=56, warn_chars=8000)
    assert repair_tables(text, findings) == text


# ---- orchestrator wiring ----------------------------------------------------


def _result(text: str, *, finish_reason: str = "stop") -> ProviderChatResult:
    return ProviderChatResult(
        id="x",
        created=0,
        model="m",
        choices=[{"message": {"role": "assistant", "content": text}}],
        usage=ProviderUsage(),
        finish_reason=finish_reason,
    )


def _orch_with(settings: Settings, **fixtures: object) -> ChatOrchestrator:
    return ChatOrchestrator(
        settings=settings,
        catalog_manager=fixtures["catalog_manager"],  # type: ignore[arg-type]
        selector=fixtures["selector"],  # type: ignore[arg-type]
        state_codec=fixtures["state_codec"],  # type: ignore[arg-type]
        assembler=fixtures["assembler"],  # type: ignore[arg-type]
        provider=fixtures["fake_provider"],  # type: ignore[arg-type]
    )


def test_lint_wiring_repairs_when_enabled(
    settings, catalog_manager, selector, state_codec, assembler, fake_provider
) -> None:
    repair_settings = settings.model_copy(update={"main_output_repair_tables": True})
    orch = _orch_with(
        repair_settings,
        catalog_manager=catalog_manager,
        selector=selector,
        state_codec=state_codec,
        assembler=assembler,
        fake_provider=fake_provider,
    )
    res = _result(WIDE_TABLE)
    orch._lint_final_output(res, request_id="r", had_tool_calls=False)
    assert "|---" not in res.text
    assert "- **Claude**" in res.text


def test_lint_wiring_default_does_not_mutate(orchestrator: ChatOrchestrator) -> None:
    # Default settings: lint (observability) on, repair off → content unchanged.
    res = _result(WIDE_TABLE)
    orchestrator._lint_final_output(res, request_id="r", had_tool_calls=False)
    assert res.text == WIDE_TABLE


def test_lint_wiring_skips_tool_call_round(
    settings, catalog_manager, selector, state_codec, assembler, fake_provider
) -> None:
    repair_settings = settings.model_copy(update={"main_output_repair_tables": True})
    orch = _orch_with(
        repair_settings,
        catalog_manager=catalog_manager,
        selector=selector,
        state_codec=state_codec,
        assembler=assembler,
        fake_provider=fake_provider,
    )
    res = _result(WIDE_TABLE, finish_reason="tool_calls")
    orch._lint_final_output(res, request_id="r", had_tool_calls=True)
    assert res.text == WIDE_TABLE  # a continuation round is never linted/repaired


def test_a_partial_table_repair_does_not_suppress_the_warning() -> None:
    """`repair_tables` converts what it can and leaves the rest.

    A table with no usable body rows is deliberately untouched. Treating "the
    text changed" as "every table is now compliant" would clear the warning for
    a table that still violates the contract — the client is told the reply is
    clean while rendering something that is not.
    """
    text = (
        "| a | b | c | d |\n"
        "|---|---|---|---|\n"
        "| 1 | 2 | 3 | 4 |\n"
        "| 5 | 6 | 7 | 8 |\n"
        "\n"
        "| w | x | y | z |\n"
        "|---|---|---|---|\n"  # header + delimiter only: nothing to convert
    )
    findings = lint_output(text, terminal_width=56, warn_chars=8000)
    assert findings.violating_tables

    repaired = repair_tables(text, findings)
    assert repaired != text, "the convertible table should have been rewritten"

    # Re-lint what the client would actually receive.
    after = lint_output(repaired, terminal_width=56, warn_chars=8000)
    assert after.violating_tables, (
        "the unconvertible table still violates the contract, so the warning "
        "must survive the repair"
    )
