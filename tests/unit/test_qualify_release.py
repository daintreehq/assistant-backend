"""The release qualification runner (benchmarks/qualify_release.py), offline.

The build-side refusals are proved in test_deployment_wiring.py by running the
`qualify-record` step itself; these pin what the runner checks and records.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from benchmarks import qualify_release as Q
from daintree_assistant_server.runbooks.catalog import load_catalog

_ROOT = Path(__file__).resolve().parents[2]


def test_every_qualifying_test_exists_and_none_judges_training_data() -> None:
    for name in Q.QUALIFYING_TESTS:
        assert (_ROOT / name).is_file(), f"{name} is named but does not exist"
        assert "training" not in Path(name).name, (
            f"{name} judges the next retrain's data, not what this build serves"
        )
    # The document budgets and the MCP contract are the serving checks most
    # likely to be dropped by a rename; name them.
    for needed in (
        "tests/unit/test_runbook_budgets.py",
        "tests/unit/test_selector_query_truncation.py",
        "tests/integration/test_runbook_mcp.py",
    ):
        assert needed in Q.QUALIFYING_TESTS


def test_identity_names_the_installed_catalog_and_runtime() -> None:
    found = Q.identity()
    catalog = load_catalog()
    assert found["catalog_revision"] == catalog.revision
    assert found["runbooks"] == len(catalog.selectable_ids)
    assert "meta.json" in found["selector"]
    runtime = found["runtime"]
    assert runtime["python"] and runtime["machine"]
    assert "onnxruntime" in runtime["distributions"]


def _junit(path: Path, *outcomes: str) -> None:
    cases = "".join(
        f'<testcase name="t{i}">' + (f"<{tag}/>" if tag != "pass" else "") + "</testcase>"
        for i, tag in enumerate(outcomes)
    )
    path.write_text(f'<testsuites><testsuite name="s">{cases}</testsuite></testsuites>')


def test_junit_outcome_counts_skips_against_the_run(tmp_path: Path) -> None:
    report = tmp_path / "junit.xml"
    _junit(report, "pass", "skipped", "failure", "error")
    assert Q.junit_outcome(report) == {"tests": 4, "failures": 1, "errors": 1, "skipped": 1}
    assert Q.junit_outcome(tmp_path / "absent.xml")["tests"] == 0


def _fake_run(
    monkeypatch: pytest.MonkeyPatch, outcomes: tuple[str, ...], exits: dict[str, int]
) -> list[list[str]]:
    """Every subprocess `run` would start, answered without starting it."""
    calls: list[list[str]] = []

    def fake(command: list[str], root: Path) -> int:
        calls.append(command)
        for arg in command:
            if arg.startswith("--junitxml="):
                _junit(Path(arg.split("=", 1)[1]), *outcomes)
                return exits.get("pytest", 0)
        return exits.get(command[-1] if command[-1] != "--no-save" else "local", 0)

    monkeypatch.setattr(Q, "_run", fake)
    return calls


def _qualify(tmp_path: Path) -> tuple[int, dict[str, Any]]:
    root = tmp_path / "root"
    root.mkdir()
    selector = Q.identity()["selector"]
    lock = {
        "files": {name: {"sha256": sha} for name, sha in selector.items() if name.endswith(".onnx")}
    }
    (root / "selector.lock").write_text(json.dumps(lock), encoding="utf-8")
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps(Q.identity()), encoding="utf-8")
    out = tmp_path / "out" / "qualification.json"
    out.parent.mkdir()
    code = Q.run(root, "c0ffee", identity, out)
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_a_clean_run_records_every_required_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_run(monkeypatch, ("pass", "pass"), {})
    code, record = _qualify(tmp_path)
    assert code == 0
    assert record["verdict"] == "pass"
    assert [c["name"] for c in record["evaluation"]["checks"]] == list(Q.REQUIRED_CHECKS)
    assert record["build_sha"] == "c0ffee"
    assert record["evaluation"]["inputs_sha256"]
    assert record["evaluation"]["tests"] == list(Q.QUALIFYING_TESTS)
    assert calls[0][-len(Q.QUALIFYING_TESTS) :] == list(Q.QUALIFYING_TESTS)
    assert [c[-2:] for c in calls[1:]] == [
        ["benchmarks.runbook_selection.bench", "coverage"],
        ["local", "--no-save"],
    ]


def test_a_skipped_test_fails_the_qualification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model-dependent tests skip when the artifact is absent; a skip there
    must not read as a pass."""
    _fake_run(monkeypatch, ("pass", "skipped"), {})
    code, record = _qualify(tmp_path)
    assert code == 1
    assert record["verdict"] == "fail"
    pytest_check = next(c for c in record["evaluation"]["checks"] if c["name"] == "pytest")
    assert pytest_check["status"] == "fail" and "1 skipped" in pytest_check["detail"]


@pytest.mark.parametrize("failing", ["pytest", "coverage", "local"])
def test_a_failing_gate_fails_the_qualification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    _fake_run(monkeypatch, ("pass",), {failing: 1})
    code, record = _qualify(tmp_path)
    assert code == 1 and record["verdict"] == "fail"


def _identity_with(**runtime: Any) -> dict[str, Any]:
    found = Q.identity()
    found["runtime"].update(runtime)
    return found


@pytest.mark.parametrize(
    "change",
    [
        {"python": "3.11.0"},
        {"libc": "musl 1.2"},
        {"distributions": {**Q.identity()["runtime"]["distributions"], "onnxruntime": "0.1"}},
    ],
)
def test_a_runtime_that_moved_under_the_test_tools_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    """`identity` read before the tools went in; now reads something else."""
    _fake_run(monkeypatch, ("pass",), {})
    before = _identity_with(**change)
    real = Q.identity
    monkeypatch.setattr(Q, "identity", lambda: real())
    root = tmp_path / "root"
    root.mkdir()
    lock = {
        "files": {n: {"sha256": h} for n, h in before["selector"].items() if n.endswith(".onnx")}
    }
    (root / "selector.lock").write_text(json.dumps(lock), encoding="utf-8")
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps(before), encoding="utf-8")
    out = tmp_path / "qualification.json"
    assert Q.run(root, "c0ffee", identity, out) == 1
    record = json.loads(out.read_text(encoding="utf-8"))
    check = next(c for c in record["evaluation"]["checks"] if c["name"] == "runtime-unchanged")
    assert check["status"] == "fail"
    assert (
        next(c for c in record["evaluation"]["checks"] if c["name"] == "selector-lock")["status"]
        == "pass"
    )


def test_an_installed_selector_the_lock_does_not_name_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_run(monkeypatch, ("pass",), {})
    root = tmp_path / "root"
    root.mkdir()
    lock = {"files": {"head.onnx": {"sha256": "0" * 64}}}
    (root / "selector.lock").write_text(json.dumps(lock), encoding="utf-8")
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps(Q.identity()), encoding="utf-8")
    out = tmp_path / "qualification.json"
    assert Q.run(root, "c0ffee", identity, out) == 1
    record = json.loads(out.read_text(encoding="utf-8"))
    checks = {c["name"]: c["status"] for c in record["evaluation"]["checks"]}
    assert checks["selector-lock"] == "fail" and checks["runtime-unchanged"] == "pass"
