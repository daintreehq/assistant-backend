"""Each consequential scenario has a runbook that states the rule for it.

`tests/fixtures/runbook_scenarios.yaml` lists the situations the migration guide
names (section 12.4, plus the distinctive failure mode of each workflow): the
starting state, the caller's surface, what must happen and what must not. Every
scenario names the runbooks that carry its rule and the phrases that state it.

This is a PROSE-CONTRACT check, and it is claimed narrowly. It proves the
instructions still say the thing; a later edit that drops a safety rule turns it
red. It does not prove a model follows the rule, and it does not prove Daintree
delivers a notice. That takes a live trial in a Daintree pane, which no offline test
can stand in for.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from daintree_assistant_server.runbooks.catalog import load_catalog

SCENARIOS = Path(__file__).resolve().parents[1] / "fixtures" / "runbook_scenarios.yaml"


def _scenarios() -> list[dict[str, Any]]:
    data = yaml.safe_load(SCENARIOS.read_text(encoding="utf-8"))
    scenarios: list[dict[str, Any]] = data["scenarios"]
    return scenarios


def test_scenarios_are_well_formed_and_unique() -> None:
    scenarios = _scenarios()
    ids = [s["id"] for s in scenarios]
    assert len(ids) == len(set(ids)), "duplicate scenario ids"
    known = load_catalog().known_ids
    for s in scenarios:
        assert set(s) >= {"id", "situation", "must", "must_not", "runbooks", "states"}, s["id"]
        unknown = [r for r in s["runbooks"] if r not in known]
        assert not unknown, f"{s['id']}: unknown runbooks {unknown}"
        assert s["states"], f"{s['id']}: no phrases to check"


@pytest.mark.parametrize("scenario", _scenarios(), ids=[s["id"] for s in _scenarios()])
def test_the_named_runbooks_state_the_rule(scenario: dict[str, Any]) -> None:
    catalog = load_catalog()
    text = "\n".join(catalog.get(r).body for r in scenario["runbooks"])  # type: ignore[union-attr]
    missing = [p for p in scenario["states"] if not re.search(p, text, re.I | re.S)]
    assert not missing, (
        f"{scenario['id']}: {scenario['runbooks']} no longer state the rule for "
        f"'{scenario['situation']}' — missing {missing}"
    )
