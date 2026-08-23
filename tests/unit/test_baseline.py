"""The committed release baseline stays within its declared bands.

Gated through pytest rather than a separate CI step so it runs on every PR
without anyone remembering to wire it, and so the failure arrives next to the
change that caused it.

What this catches that the per-file budget tests do not: those assert CEILINGS —
absolute limits that a 20% growth can sit comfortably under. This asserts
MOVEMENT. A base prompt that quietly grows 4% a month passes every ceiling for a
year and is 60% larger at the end of it.
"""

from __future__ import annotations

import json

import pytest

from tools.baseline import BANDS, BASELINE_PATH, capture, compare


def _baseline() -> dict[str, object]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def test_the_baseline_file_exists_and_parses() -> None:
    assert BASELINE_PATH.exists(), (
        "benchmarks/baseline.json is the committed record of what the server "
        "sends. Without it there is nothing to compare a release against."
    )
    assert _baseline()


def test_current_measurements_are_within_their_bands() -> None:
    problems = compare(capture(), _baseline())
    assert not problems, "\n".join(problems)


def test_every_banded_metric_is_actually_in_the_baseline() -> None:
    """A band naming a metric the file does not carry enforces nothing."""
    flat = {k for section in _baseline().values() for k in section}  # type: ignore[union-attr]
    missing = sorted(set(BANDS) - flat)
    assert not missing, f"bands reference metrics absent from the baseline: {missing}"


def test_the_band_check_actually_fails_on_a_regression() -> None:
    """A test for the test.

    A comparison that cannot fail is worse than none — it reports "within bands"
    forever and everyone believes it. Shrink the recorded base prompt so the
    current one reads as a large growth, and confirm the checker objects.
    """
    baseline = _baseline()
    shrunk = json.loads(json.dumps(baseline))
    shrunk["prompt"]["base_prompt_bytes"] = int(  # type: ignore[index]
        baseline["prompt"]["base_prompt_bytes"] * 0.5  # type: ignore[index,operator]
    )
    problems = compare(capture(), shrunk)
    assert any("base_prompt_bytes" in p for p in problems), problems


def test_the_identity_block_records_the_pinned_posture() -> None:
    """The model and the upstream are release facts, not measurements.

    They are in the baseline so a diff shows a posture change explicitly — the
    kind of thing that must never move by accident. Asserted, not merely
    captured: ``compare()`` only checks the BANDED numeric metrics, so an
    identity string nobody asserts drifts in silence.
    """
    from daintree_assistant_server.config import SERVING_MODEL

    identity = _baseline()["identity"]
    assert identity["model"] == SERVING_MODEL  # type: ignore[index]
    # The upstream itself. Together is a model host rather than a router, so
    # there is no endpoint-selection posture to record beside it — privacy mode,
    # sort axis and denylist have no subject. Nor is there a first-byte endpoint
    # sort: the call that sat on that path (the upstream runbook selector) was
    # replaced by the LOCAL two-stage selector, whose release facts are asserted
    # below instead.
    assert identity["provider"] == "together"  # type: ignore[index]


def test_the_selector_block_describes_the_shipped_artifact() -> None:
    """The baseline pins the LOCAL selector's shape.

    Deliberately NOT a provenance gate any more. The set-wise head has no
    per-runbook weights, so a catalog that moved since training is served rather
    than degraded — ``routing_revision_matches_catalog`` is captured as a
    drift signal for a human, and asserting it would re-impose exactly the
    coupling this architecture removed. What IS asserted is the shape the
    serving code depends on.
    """
    from daintree_assistant_server.runbooks.local.setwise import FEATURE_NAMES

    sel = _baseline()["selector"]
    assert sel["selector_onnx_bytes"] > 1_000_000  # type: ignore[index]
    assert sel["candidates"] >= 8  # type: ignore[index]
    assert sel["ordering"] in {"equivariant", "positional", "blind"}  # type: ignore[index]
    assert len(FEATURE_NAMES) == 3


def test_the_shipped_table_covers_the_live_catalog() -> None:
    """Every selectable runbook must have a vector, or it can never be shortlisted.

    This is the check that replaced the routing-revision gate: not "was the head
    trained on this catalog" (it need not have been) but "can every runbook in the
    catalog be reached at all".
    """
    from daintree_assistant_server.runbooks.catalog import default_runbooks_dir, load_catalog
    from daintree_assistant_server.runbooks.local.paths_local import selector_artifact_dir
    from daintree_assistant_server.runbooks.local.setwise import SetwiseClassifier

    artifact = selector_artifact_dir()
    if not (artifact / "head.onnx").is_file():
        pytest.skip("selector artifact not present (LFS)")
    catalog = load_catalog(default_runbooks_dir())
    table = SetwiseClassifier(artifact).sync(catalog)
    assert set(table.ids) == set(catalog.selectable_ids)
