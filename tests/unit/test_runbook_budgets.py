"""Size and house-style guards for runbooks.

Every search result carries a runbook's full body, so a body is paid for in the
calling agent's context on every hit. The ceilings are generous — they catch
drift toward bloat, not every incremental edit.

Every current runbook and its supporting bundle is checked, including the
additional hosts foundation attached for a connected caller.
"""

from __future__ import annotations

import pytest

from daintree_assistant_server.runbooks.catalog import (
    HOSTS_FOUNDATION_ID,
    load_catalog,
    view_for,
)
from daintree_assistant_server.runbooks.host_state import HostState
from daintree_assistant_server.runbooks.models import Runbook

# whenToUse is the classifier's primary routing signal and a search result field.
RUNBOOK_WHEN_TO_USE_CEILING = 2048
RUNBOOK_SUMMARY_CEILING = 512
RUNBOOK_BODY_HARD_CEILING = 8 * 1024
# Cut to 5KB on 2026-09-26, when the largest body (spawn-visible-agent) was 4.2KB.
RUNBOOK_BODY_RECOMMENDED = 5 * 1024
# Runbooks allowed over the recommended budget, each with a justification.
OVERSIZE_ALLOWLIST: set[str] = {
    # Bounded by its own, stricter FOUNDATION_BODY_CEILING below.
    "daintree.foundation",
}


def _every_runbook() -> list[Runbook]:
    return list(load_catalog().runbooks)


def test_runbook_when_to_use_under_ceiling() -> None:
    for runbook in _every_runbook():
        size = len(runbook.when_to_use.encode("utf-8"))
        assert size <= RUNBOOK_WHEN_TO_USE_CEILING, (
            f"{runbook.id} whenToUse is {size}B, over the {RUNBOOK_WHEN_TO_USE_CEILING}B "
            "ceiling; put procedure in the body"
        )


def test_runbook_summary_under_ceiling() -> None:
    for runbook in _every_runbook():
        size = len(runbook.summary.encode("utf-8"))
        assert size <= RUNBOOK_SUMMARY_CEILING, (
            f"{runbook.id} summary is {size}B, over the {RUNBOOK_SUMMARY_CEILING}B ceiling; "
            "summary is a one-line signal and routing detail belongs in whenToUse"
        )


def test_runbook_bodies_under_hard_ceiling() -> None:
    for runbook in _every_runbook():
        size = len(runbook.body.encode("utf-8"))
        assert size <= RUNBOOK_BODY_HARD_CEILING, f"{runbook.id} body {size}B exceeds hard ceiling"


def test_runbook_bodies_under_recommended_budget() -> None:
    for runbook in _every_runbook():
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
    for runbook in _every_runbook():
        assert "|---" not in runbook.body, f"{runbook.id} contains a pipe table"


def test_runbooks_end_with_report_back() -> None:
    for runbook in _every_runbook():
        assert "report back" in runbook.body.lower(), f"{runbook.id} has no Report-back template"


# The foundation rides along with every search and every retrieval, as a supporting
# runbook, so it is paid for on EVERY response rather than only on a hit. Measured at
# the migration to the app MCP (2026-09-25): 14.0KB; cut the same day to 5.7KB of
# cross-cutting judgment, with workflow rules moved into the runbooks that need them.
# Cut again on 2026-09-26 to 5.0KB, leaning on the app's batch tools and waitForReply.
# Raised the same day to 5400B for the pending-reply recheck rule (a live session stopped
# on a vote Daintree had already received). 5400B is the most RUNBOOK_BUNDLE_CEILING
# leaves beside the largest runbook (relocate-agents, 4.8KB): cut before raising again.
FOUNDATION_BODY_CEILING = 5400
# What following one runbook costs: its body plus everything `supporting_for` sends
# with it. Measured at the same point: 28.1KB at the largest (fan-out-work); 14.9KB
# after the foundation cut (edits.spawn-visible-agent); 9.1KB after the 2026-09-26 cut.
RUNBOOK_BUNDLE_CEILING = 10 * 1024
# The control catalog's hosts foundation (issue #159) comes with EVERY runbook a caller
# with a host connected is served, not only the hosts ones, and there is no room for it
# inside RUNBOOK_BUNDLE_CEILING (most bundles were within 20B of it). So it is bounded
# on its own, and everything else in a bundle that carries it still fits the ordinary
# ceiling. Written at 3.0KB.
HOSTS_FOUNDATION_BODY_CEILING = 3 * 1024


def test_foundations_stay_compact() -> None:
    for runbook in _every_runbook():
        if not runbook.meta.foundation:
            continue
        size = len(runbook.body.encode("utf-8"))
        assert size <= FOUNDATION_BODY_CEILING, (
            f"{runbook.id} body is {size}B, over {FOUNDATION_BODY_CEILING}B; it is sent "
            "with every response, so move task-specific rules into the runbooks that need them"
        )


def test_the_hosts_foundation_stays_within_its_own_ceiling() -> None:
    hosts_foundation = load_catalog().get(HOSTS_FOUNDATION_ID)
    assert hosts_foundation is not None
    size = len(hosts_foundation.body.encode("utf-8"))
    assert size <= HOSTS_FOUNDATION_BODY_CEILING, (
        f"{HOSTS_FOUNDATION_ID} body is {size}B, over {HOSTS_FOUNDATION_BODY_CEILING}B; it "
        "is sent with every runbook while a host is connected"
    )


#: Every kind of caller each catalog serves, by what changes its bundles.
CALLERS = [None, HostState("connected")]


@pytest.mark.parametrize("caller", CALLERS)
def test_a_runbook_with_its_supporting_runbooks_stays_bounded(caller: HostState | None) -> None:
    view = view_for(load_catalog(), caller)
    catalog = view.catalog
    for runbook in catalog.runbooks:
        if runbook.id not in catalog.selectable_ids:
            continue
        bundle = [runbook, *catalog.supporting_for([runbook.id], attach=view.attach)]
        # The control hosts foundation is bounded on its own (above); everything else in
        # the bundle still fits the ordinary ceiling.
        rest = [r for r in bundle if r.id != HOSTS_FOUNDATION_ID]
        size = sum(len(r.body.encode("utf-8")) for r in rest)
        assert size <= RUNBOOK_BUNDLE_CEILING, (
            f"{runbook.id} with its supporting runbooks is {size}B for a {caller} caller "
            f"(the control hosts foundation aside), over {RUNBOOK_BUNDLE_CEILING}B: "
            f"{[r.id for r in bundle]}"
        )
