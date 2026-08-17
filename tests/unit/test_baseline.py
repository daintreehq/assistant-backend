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
    """Model and privacy mode are release facts, not measurements.

    They are in the baseline so a diff shows a posture change explicitly — the
    kind of thing that must never move by accident.
    """
    from daintree_assistant_server.config import OPENROUTER_MODEL
    from daintree_assistant_server.providers.policy import DEFAULT_POLICY

    identity = _baseline()["identity"]
    assert identity["model"] == OPENROUTER_MODEL  # type: ignore[index]
    assert identity["privacy_mode"] == DEFAULT_POLICY.privacy.value  # type: ignore[index]
    # The sorts are asserted, not merely recorded. `compare()` only checks the
    # BANDED numeric metrics, so an identity string it captures but nobody asserts
    # drifts in silence — which is exactly what happened to `endpoint_sort` until
    # a second sort existed to notice it.
    assert identity["default_endpoint_sort"] == DEFAULT_POLICY.sort.value  # type: ignore[index]
    # There is no first-byte endpoint sort anymore: the call that sat on the
    # first-byte path (the upstream skill selector) was replaced by the LOCAL
    # classifier, whose release facts are asserted below instead.


def test_the_classifier_block_matches_the_shipped_artifact() -> None:
    """The baseline pins the LOCAL selector's provenance.

    A retrain that forgets the export, or a catalog change that forgets the
    retrain, shows up here as a failing push instead of a degraded-open
    selector discovered in production logs.
    """
    from daintree_assistant_server.skills.catalog import default_skills_dir, load_catalog
    from daintree_assistant_server.skills.local.documents import routing_revision

    clf = _baseline()["classifier"]
    catalog = load_catalog(default_skills_dir())
    assert clf["routing_revision_matches_catalog"] is True  # type: ignore[index]
    assert clf["routing_revision"] == routing_revision(catalog)  # type: ignore[index]
    assert clf["labels"] == len(catalog.selectable_ids)  # type: ignore[index]
