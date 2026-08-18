"""Rules moved OUT of the permanent base prompt must still exist somewhere reachable.

The base prompt shrank from ~30.6 KB to ~23.5 KB by relocating rules rather than
deleting them: operational mechanics into ``daintree.foundation`` (measured active on
153 of the 153 routing-benchmark cases that load any skill), and narrower rules into
the runbook that owns them. Every relocated rule stopped being an always-on guarantee
at that moment.

Nothing else would notice if one went missing. The byte ceilings in
``test_prompt_budgets.py`` only get HAPPIER as text disappears; the golden snapshots
render the base prompt and a pinned fixture catalog, not the live skills; and the
routing benchmark scores which skill is picked, never what its body says. So a later
edit tidying the foundation could silently delete a rule that used to hold on every
turn, and the whole suite would stay green.

This is the ratchet that stops that. Each entry is a behavioural claim the base prompt
used to make, a probe string, and the file that now owns it. The probes are deliberately
short and distinctive — they pin the CLAIM's continued existence, not its wording, so a
rewrite of the surrounding prose is free while a deletion is not.

If you are here because a probe failed: either restore the rule, or move it somewhere
else and update its entry. Do not delete an entry to make the test pass — that is the
exact silent regression this file exists to catch.
"""

from __future__ import annotations

import pytest

from daintree_assistant_server.skills.catalog import load_catalog

#: ``(claim, probe, owning skill id)``.
RELOCATED_RULES: list[tuple[str, str, str]] = [
    # --- from 10-hard-rules.md, deleted outright ---
    ("spawn title is the action alone", "task-descriptive title", "daintree.foundation"),
    ("title at spawn, do not rename after", "name it correctly AT SPAWN", "daintree.foundation"),
    ("agentId must be a configured agent", "REAL configured Daintree agent", "daintree.foundation"),
    ("a spoken agent name is only a hint", "antiravity", "daintree.foundation"),
    # --- from 20-tool-discipline.md, deleted outright ---
    ("the terminal roster is a fresh snapshot", "FRESH per-request snapshot", "daintree.foundation"),
    ("a storeId is the only handle back", "the ONLY handle back", "daintree.foundation"),
    ("drop a scratch store when done", "scratch.drop it when done", "daintree.foundation"),
    ("an async result means accepted", "ACCEPTED, not finished", "daintree.foundation"),
    ("async.cancel leaves the terminal running", "the terminal keeps running", "daintree.foundation"),
    ("capable model judges, cheap model reads", "capable model on judgement", "daintree.foundation"),
    ("a small-model read result is a leaf", "is a LEAF", "daintree.foundation"),
    ("agent state vocabulary", "directing | completed | exited", "daintree.foundation"),
    ('"completed" is transient', "transient flash", "daintree.foundation"),
    ("waitingReason question means blocked", 'waitingReason "question"', "daintree.foundation"),
    ("calls dispatch in emission order", "dispatch in emission order", "daintree.foundation"),
    ("small-model reads dispatch concurrently", "dispatches CONCURRENTLY", "daintree.foundation"),
    ("an id minted this reply waits a turn", "waits for the NEXT turn", "daintree.foundation"),
    # --- from 20-tool-discipline.md, compressed in base, mechanics relocated ---
    ("one extract per terminal, not per cohort", "PER TERMINAL", "daintree.foundation"),
    ("an id array merges rather than fans out", "MERGE the whole cohort", "daintree.foundation"),
    ("awaitAll returns partial results early", "interruptedByUser", "daintree.foundation"),
    ("a bounded extract wait for one agent", "wait for ONE agent", "daintree.foundation"),
    ("re-spawn a crashed agent at most once", "re-spawn AT MOST once", "daintree.foundation"),
    (
        "watcher.cancel is refused on a finished watch",
        "expect watcher.cancel on it to be refused",
        "daintree.foundation",
    ),
    ("some id families are natively short", "never pad", "daintree.foundation"),
    # --- relocated to the runbook that owns the trigger ---
    (
        "compaction preserves the full transcript",
        "FULL discarded transcript",
        "daintree.orchestration.status-and-resume",
    ),
    ("the PR watcher stops itself", "STOPS ON ITS OWN", "daintree.orchestration.watch-outcome"),
]


@pytest.mark.parametrize(
    ("claim", "probe", "skill_id"),
    RELOCATED_RULES,
    ids=[c for c, _, _ in RELOCATED_RULES],
)
def test_a_relocated_rule_still_has_a_home(claim: str, probe: str, skill_id: str) -> None:
    catalog = load_catalog()
    skill = catalog.get(skill_id)
    assert skill is not None, f"{skill_id} owns a relocated rule but is not in the catalog"
    assert probe in skill.body, (
        f"the base prompt no longer states {claim!r} and {skill_id} no longer carries it "
        f"either (probe {probe!r}). Restore it, or relocate it and update RELOCATED_RULES."
    )


def test_the_foundation_is_still_a_domain_foundation() -> None:
    """The whole relocation rests on `daintree.foundation` being auto-paired with every
    selected `daintree.*` specific. If it ever stopped being a foundation, two dozen
    rules above would quietly become opt-in."""
    catalog = load_catalog()
    foundation = catalog.get("daintree.foundation")
    assert foundation is not None
    assert foundation.meta.foundation is True
    assert catalog.domain_foundation("daintree.terminals.rename") == "daintree.foundation"
