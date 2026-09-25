"""Size and house-style guards for runbooks.

Every search result carries a runbook's full body, so a body is paid for in the
calling agent's context on every hit. The ceilings are generous — they catch
drift toward bloat, not every incremental edit.
"""

from __future__ import annotations

from daintree_assistant_server.runbooks.catalog import load_catalog

# whenToUse is the classifier's primary routing signal and a search result field.
RUNBOOK_WHEN_TO_USE_CEILING = 2048
RUNBOOK_SUMMARY_CEILING = 512
RUNBOOK_BODY_HARD_CEILING = 24 * 1024
RUNBOOK_BODY_RECOMMENDED = 16 * 1024
# Runbooks allowed over the recommended budget, each with a justification.
OVERSIZE_ALLOWLIST: set[str] = set()


def test_runbook_when_to_use_under_ceiling() -> None:
    for runbook in load_catalog().runbooks:
        size = len(runbook.when_to_use.encode("utf-8"))
        assert size <= RUNBOOK_WHEN_TO_USE_CEILING, (
            f"{runbook.id} whenToUse is {size}B, over the {RUNBOOK_WHEN_TO_USE_CEILING}B "
            "ceiling; put procedure in the body"
        )


def test_runbook_summary_under_ceiling() -> None:
    for runbook in load_catalog().runbooks:
        size = len(runbook.summary.encode("utf-8"))
        assert size <= RUNBOOK_SUMMARY_CEILING, (
            f"{runbook.id} summary is {size}B, over the {RUNBOOK_SUMMARY_CEILING}B ceiling; "
            "summary is a one-line signal and routing detail belongs in whenToUse"
        )


def test_runbook_bodies_under_hard_ceiling() -> None:
    for runbook in load_catalog().runbooks:
        size = len(runbook.body.encode("utf-8"))
        assert size <= RUNBOOK_BODY_HARD_CEILING, f"{runbook.id} body {size}B exceeds hard ceiling"


def test_runbook_bodies_under_recommended_budget() -> None:
    for runbook in load_catalog().runbooks:
        if runbook.id in OVERSIZE_ALLOWLIST:
            continue
        size = len(runbook.body.encode("utf-8"))
        assert size <= RUNBOOK_BODY_RECOMMENDED, (
            f"{runbook.id} body {size}B exceeds the {RUNBOOK_BODY_RECOMMENDED}B recommended "
            "budget; split or trim it, or add it to OVERSIZE_ALLOWLIST with justification"
        )


def test_oversize_allowlist_is_not_stale() -> None:
    catalog = load_catalog()
    for runbook_id in OVERSIZE_ALLOWLIST:
        runbook = catalog.get(runbook_id)
        assert runbook is not None, f"allowlisted runbook {runbook_id} no longer exists"
        size = len(runbook.body.encode("utf-8"))
        assert size > RUNBOOK_BODY_RECOMMENDED, (
            f"{runbook_id} is now {size}B (under budget); remove it from OVERSIZE_ALLOWLIST"
        )


def test_runbooks_have_no_pipe_tables() -> None:
    for runbook in load_catalog().runbooks:
        assert "|---" not in runbook.body, f"{runbook.id} contains a pipe table"


def test_runbooks_end_with_report_back() -> None:
    for runbook in load_catalog().runbooks:
        assert "report back" in runbook.body.lower(), f"{runbook.id} has no Report-back template"
