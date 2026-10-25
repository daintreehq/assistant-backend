"""Qualify a release candidate's catalog and selector before it can take traffic.

The release smoke (infra/gcp/smoke.sh) proves a candidate answers one real
search. It cannot prove the catalog routes: a routing document over its token
cap, a broken carve-out or a recall drop all serve a 200. This is the gate that
can, and cloudbuild.yaml runs it on every release:

    identity   run in ANY image holding the installed package (stdlib at the top,
               so `docker run -i IMAGE python - identity < this file` works):
               the catalog revision, the selector files' hashes and the
               installed distributions, as JSON on stdout.
    run        run in the Dockerfile's `qualify` stage, which is the builder's
               venv (the layer the runtime image copies) plus the test tools:
               checks the installed selector against selector.lock, runs the
               serving checks below, and writes the qualification record.
    verify     run by the build with nothing but python3: refuses a record that
               failed, skipped a check, or names another commit, lock, catalog,
               model or runtime than the candidate image it is meant to qualify.

What qualifies a serving candidate is QUALIFYING_TESTS (catalog loading and
validation, runbook hygiene and dependencies, document budgets, the Daintree MCP
surfaces runbooks may name, the MCP contract, search over the shipped model),
`bench coverage` and `bench local`. Training-corpus checks are not here: they
judge the next retrain's data, not what this build serves. A skipped test fails
the run, because a model-dependent check that skips for an absent artifact has
proved nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

SCHEMA = 1

#: The serving-relevant test files, relative to the qualification root.
QUALIFYING_TESTS = (
    "tests/unit/test_catalog.py",
    "tests/unit/test_catalogs.py",
    "tests/unit/test_runbooks_loader.py",
    "tests/unit/test_runbook_dependencies.py",
    "tests/unit/test_runbook_budgets.py",
    "tests/unit/test_runbook_catalog_hygiene.py",
    "tests/unit/test_runbook_examples.py",
    "tests/unit/test_runbook_host_execution.py",
    "tests/unit/test_runbook_migration.py",
    "tests/unit/test_runbook_scenarios.py",
    "tests/unit/test_runbook_search.py",
    "tests/unit/test_selector_serving_consistency.py",
    "tests/unit/test_selector_query_truncation.py",
    "tests/unit/test_local_selection.py",
    "tests/unit/test_host_state.py",
    "tests/unit/test_mcp_sessions.py",
    "tests/integration/test_runbook_mcp.py",
    "tests/integration/test_transport_edges.py",
    "tests/integration/test_health.py",
)

#: Every check a passing record must carry, in the order `run` performs them.
REQUIRED_CHECKS = ("runtime-unchanged", "selector-lock", "pytest", "bench-coverage", "bench-local")

#: What the evaluation read, digested so the record names its inputs.
_EVALUATION_INPUTS = ("tests", "benchmarks/runbook_selection", "selector.lock", "pyproject.toml")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _distributions() -> dict[str, str]:
    from importlib import metadata

    found: dict[str, str] = {}
    for dist in metadata.distributions():
        name = (dist.metadata["Name"] or "").lower().replace("_", "-")
        if name:
            found[name] = dist.version
    return dict(sorted(found.items()))


def identity() -> dict[str, Any]:
    """What this interpreter would serve: catalog, model files and runtime."""
    from daintree_assistant_server.runbooks.catalog import load_catalog
    from daintree_assistant_server.runbooks.local.paths_local import selector_artifact_dir

    catalog = load_catalog()
    root = selector_artifact_dir()
    selector = {
        path.relative_to(root).as_posix(): _sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    libc, libc_version = platform.libc_ver()
    return {
        "catalog_revision": catalog.revision,
        "runbooks": len(catalog.selectable_ids),
        "selector": selector,
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "system": platform.system(),
            "machine": platform.machine(),
            "libc": f"{libc} {libc_version}".strip(),
            "distributions": _distributions(),
        },
    }


def _lock_files(lock_path: Path) -> dict[str, str]:
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    return {name: entry["sha256"] for name, entry in sorted(lock["files"].items())}


def _lock_mismatches(lock: dict[str, str], selector: dict[str, str]) -> list[str]:
    """Lock entries the installed selector lacks or holds different bytes for.
    The lock names the model files; meta.json and labels.json ride in Git."""
    return sorted(name for name, sha in lock.items() if selector.get(name) != sha)


def _tree_digest(root: Path, entries: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        base = root / entry
        files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
        for path in files:
            rel = path.relative_to(root).as_posix()
            if "__pycache__" in path.parts or "/results/" in f"/{rel}":
                continue
            digest.update(f"{rel}\0{_sha256_file(path)}\n".encode())
    return digest.hexdigest()


def junit_outcome(xml_path: Path) -> dict[str, int]:
    """Counts from a pytest JUnit report; a skip counts against the run."""
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    if not xml_path.is_file():
        return counts
    for case in ET.parse(xml_path).getroot().iter("testcase"):
        counts["tests"] += 1
        for key, tag in (("failures", "failure"), ("errors", "error"), ("skipped", "skipped")):
            if case.find(tag) is not None:
                counts[key] += 1
    return counts


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}", flush=True)
    return {"name": name, "status": "pass" if ok else "fail", "detail": detail}


def _run(command: list[str], root: Path) -> int:
    print(f"$ {' '.join(command)}", flush=True)
    return subprocess.run(command, cwd=root, check=False).returncode  # noqa: S603


def run(root: Path, build_sha: str, identity_path: Path, out: Path) -> int:
    """Qualify what is installed in this interpreter; write the record either way."""
    before = json.loads(identity_path.read_text(encoding="utf-8"))
    now = identity()
    lock_path = root / "selector.lock"
    lock = _lock_files(lock_path)
    checks: list[dict[str, Any]] = []

    # The test tools were installed after `identity` read the runtime, into the
    # same venv the runtime image copies. Not one runtime package may have moved,
    # and nothing else about the interpreter may differ; added tools are allowed.
    moved = sorted(
        name
        for name, version in before["runtime"]["distributions"].items()
        if now["runtime"]["distributions"].get(name) != version
    ) + sorted(
        key
        for key, value in before["runtime"].items()
        if key != "distributions" and now["runtime"].get(key) != value
    )
    checks.append(
        _check(
            "runtime-unchanged",
            not moved
            and before["selector"] == now["selector"]
            and before["catalog_revision"] == now["catalog_revision"],
            "moved: " + ", ".join(moved) if moved else "runtime intact",
        )
    )

    mismatched = _lock_mismatches(lock, now["selector"])
    checks.append(
        _check(
            "selector-lock",
            not mismatched,
            "differs from selector.lock: " + ", ".join(mismatched)
            if mismatched
            else f"{len(lock)} installed files match selector.lock",
        )
    )

    junit = out.parent / "qualification-junit.xml"
    code = _run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-rs",
            f"--junitxml={junit}",
            *QUALIFYING_TESTS,
        ],
        root,
    )
    counts = junit_outcome(junit)
    checks.append(
        _check(
            "pytest",
            code == 0 and counts["tests"] > 0 and counts["skipped"] == 0,
            f"exit {code}; {counts['tests']} tests, {counts['failures']} failed, "
            f"{counts['errors']} errors, {counts['skipped']} skipped (a skip proves nothing)",
        )
    )

    bench = [sys.executable, "-m", "benchmarks.runbook_selection.bench"]
    code = _run([*bench, "coverage"], root)
    checks.append(_check("bench-coverage", code == 0, f"exit {code}"))
    code = _run([*bench, "local", "--no-save"], root)
    checks.append(_check("bench-local", code == 0, f"exit {code}"))

    passed = all(c["status"] == "pass" for c in checks)
    record = {
        "schema": SCHEMA,
        "build_sha": build_sha,
        "selector_lock_sha256": _sha256_file(lock_path),
        "catalog_revision": before["catalog_revision"],
        "runbooks": before["runbooks"],
        "selector": before["selector"],
        "runtime": before["runtime"],
        "evaluation": {
            "inputs_sha256": _tree_digest(root, _EVALUATION_INPUTS),
            "tests": list(QUALIFYING_TESTS),
            "checks": checks,
        },
        "verdict": "pass" if passed else "fail",
    }
    out.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"qualification {record['verdict']}: build {build_sha}, catalog "
        f"{record['catalog_revision']}, lock {record['selector_lock_sha256'][:12]}",
        flush=True,
    )
    return 0 if passed else 1


_IDENTITY_TYPES: dict[str, type] = {
    "catalog_revision": str,
    "runbooks": int,
    "selector": dict,
    "runtime": dict,
}


def verify(
    record: dict[str, Any],
    candidate: dict[str, Any],
    lock_path: Path,
    build_sha: str,
    root: Path,
) -> list[str]:
    """Every reason this record does not qualify this candidate; empty if it does.

    `root` is the build's checkout: the evaluation inputs the record names must
    be the ones this commit carries."""
    problems: list[str] = []
    for name, value in (("record", record), ("candidate", candidate)):
        if not isinstance(value, dict):
            return [f"the {name} is not a JSON object"]
        for key, kind in _IDENTITY_TYPES.items():
            if not isinstance(value.get(key), kind) or not value.get(key):
                problems.append(f"the {name} has no usable {key}")
    evaluation = record.get("evaluation")
    if not isinstance(evaluation, dict):
        return [*problems, "the record has no evaluation"]
    if problems:
        return problems
    if record.get("schema") != SCHEMA:
        problems.append(f"record schema is {record.get('schema')!r}, not {SCHEMA}")
    if record.get("verdict") != "pass":
        problems.append(f"the qualification verdict is {record.get('verdict')!r}")
    # Exactly the required checks, once each, every one passed: an extra entry or
    # a later duplicate must not be able to hide a failure.
    checks = evaluation.get("checks")
    rows = [c for c in checks if isinstance(c, dict)] if isinstance(checks, list) else []
    names = [c.get("name") for c in rows]
    statuses = {c.get("name"): c.get("status") for c in rows}
    for name in REQUIRED_CHECKS:
        if names.count(name) != 1 or statuses.get(name) != "pass":
            problems.append(f"check {name} is {statuses.get(name, 'missing')}")
    if (
        not isinstance(checks, list)
        or len(rows) != len(checks)
        or len(names) != len(REQUIRED_CHECKS)
    ):
        problems.append("the record's checks are not exactly the required ones")
    if evaluation.get("tests") != list(QUALIFYING_TESTS):
        problems.append("the record ran other tests than QUALIFYING_TESTS")
    if evaluation.get("inputs_sha256") != _tree_digest(root, _EVALUATION_INPUTS):
        problems.append("the record's evaluation inputs are not this checkout's")
    if record.get("build_sha") != build_sha:
        problems.append(f"qualified build {record.get('build_sha')!r}, releasing {build_sha!r}")
    if record.get("selector_lock_sha256") != _sha256_file(lock_path):
        problems.append("qualified a different selector.lock than this build fetched")
    if _lock_mismatches(_lock_files(lock_path), record["selector"]):
        problems.append("the qualified selector files are not the ones selector.lock names")
    for key in ("catalog_revision", "runbooks", "selector", "runtime"):
        if record.get(key) != candidate.get(key):
            problems.append(f"the candidate image's {key} differs from the qualified one")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("identity")
    p_run = sub.add_parser("run")
    p_run.add_argument("--root", type=Path, default=Path.cwd())
    p_run.add_argument("--build-sha", required=True)
    p_run.add_argument("--identity", type=Path, required=True)
    p_run.add_argument("--out", type=Path, required=True)
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--record", type=Path, required=True)
    p_verify.add_argument("--candidate", type=Path, required=True)
    p_verify.add_argument("--lock", type=Path, required=True)
    p_verify.add_argument("--build-sha", required=True)
    p_verify.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    if args.cmd == "identity":
        print(json.dumps(identity(), indent=1, sort_keys=True))
        return 0
    if args.cmd == "run":
        return run(args.root, args.build_sha, args.identity, args.out)
    try:
        record = json.loads(args.record.read_text(encoding="utf-8"))
        candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"ERROR: no usable qualification: {exc}", file=sys.stderr)
        return 1
    problems = verify(record, candidate, args.lock, args.build_sha, args.root)
    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    if problems:
        return 1
    # Three lines for the build to stamp: what was qualified, and the record itself.
    print(record["catalog_revision"])
    print(record["selector_lock_sha256"])
    print(hashlib.sha256(args.record.read_bytes()).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
