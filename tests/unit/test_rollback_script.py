"""`infra/gcp/rollback.sh` — revision selection and refusal, tested offline.

A rollback script is run for the first time during an incident, which is the worst
possible moment to discover it picks the wrong revision. The real thing cannot be
rehearsed cheaply (it needs a deployed Cloud Run service and moves production
traffic), so the parts that can be wrong on their own — which revision it decides
on, and which situations it refuses outright — are exercised here against a fake
`gcloud` on PATH.

`--dry-run` and `--list` are what make this possible: they stop before any
mutation, so these tests drive the ACTUAL script rather than a reimplementation of
its logic in Python.

The fake `gcloud` is deliberately strict. It asserts the flags the real command
needs (`--service`, `--region`, `--project`, `--format=json`) and exits non-zero
on anything else, because a fake that answers any invocation lets a missing
`--service` — which in production returns every revision in the region, including
other services' — stay green forever.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "infra" / "gcp" / "rollback.sh"
SERVICE = "daintree-assistant"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash is required to run the rollback script"
)


def _revision(name: str, sha: str, created: str, *, ready: bool = True) -> dict[str, object]:
    return {
        "metadata": {
            "name": name,
            "labels": {"commit-sha": sha},
            "creationTimestamp": created,
        },
        "status": {
            "conditions": [
                # A revision that failed to start still reports OTHER conditions as
                # True, which is why readiness is read from the Ready condition
                # specifically rather than from "has a Ready condition, and has
                # something True".
                {"type": "ContainerHealthy", "status": "True"},
                {"type": "Ready", "status": "True" if ready else "False"},
            ]
        },
    }


def _service(traffic: list[tuple[str, int]], url: str = "https://svc.example") -> dict[str, object]:
    return {
        "status": {
            "url": url,
            "traffic": [{"revisionName": n, "percent": p} for n, p in traffic],
        }
    }


def _fake_gcloud(tmp_path: Path, service: dict[str, object], revisions: list[dict[str, object]]):
    """A `gcloud` that answers the two reads ONLY when correctly invoked."""
    (tmp_path / "service.json").write_text(json.dumps(service))
    (tmp_path / "revisions.json").write_text(json.dumps(revisions))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(
        f"""#!/usr/bin/env bash
# Record every invocation so a test can assert on it.
echo "$@" >> "{tmp_path}/calls.log"

args=" $* "
have() {{ case "$args" in *" $1"*) return 0 ;; *) return 1 ;; esac }}

# Flags every read must carry. A fake that skipped these would let the script
# lose --service (which returns other services' revisions in production) or
# --region (wrong region entirely) without any test noticing.
for required in --region= --project= --format=json; do
  have "$required" || {{ echo "fake gcloud: missing $required in: $*" >&2; exit 90; }}
done

case "$args" in
  *" services describe "*) cat "{tmp_path}/service.json"; exit 0 ;;
  *" revisions list "*)
    have --service= || {{ echo "fake gcloud: revisions list without --service: $*" >&2; exit 91; }}
    cat "{tmp_path}/revisions.json"; exit 0 ;;
esac
echo "fake gcloud: unexpected call: $*" >&2
exit 99
"""
    )
    gcloud.chmod(0o755)
    return bin_dir


def _run(tmp_path: Path, service, revisions, *args: str) -> subprocess.CompletedProcess[str]:
    bin_dir = _fake_gcloud(tmp_path, service, revisions)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PROJECT": "example-project",
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True, env=env, cwd=REPO_ROOT, timeout=60,
    )


def _chosen(result: subprocess.CompletedProcess[str]) -> str:
    """The revision the script decided on, read from the plan it printed.

    Asserting on the plan line specifically, not "the name appears in stdout" —
    every candidate appears in the revision table above it, so a substring check
    passes no matter which one was selected.
    """
    for line in result.stdout.splitlines():
        if line.strip().startswith("to    "):
            return line.strip()[len("to    "):].strip()
    return ""


NEWEST = _revision(f"{SERVICE}-00003-ccc", "ccc1111", "2026-08-15T10:00:00Z")
MIDDLE = _revision(f"{SERVICE}-00002-bbb", "bbb2222", "2026-08-14T10:00:00Z")
OLDEST = _revision(f"{SERVICE}-00001-aaa", "aaa3333", "2026-08-13T10:00:00Z")
# `gcloud revisions list --sort-by='~creationTimestamp'` returns newest first.
ALL = [NEWEST, MIDDLE, OLDEST]
ON_NEWEST = _service([(f"{SERVICE}-00003-ccc", 100)])


def test_the_script_is_executable_and_parses() -> None:
    assert SCRIPT.exists(), "infra/gcp/rollback.sh is missing"
    assert os.access(SCRIPT, os.X_OK), "rollback.sh must be executable to be usable in an incident"
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0


# ---------------------------------------------------------------------------
# Selection.
# ---------------------------------------------------------------------------


def test_it_targets_the_revision_behind_the_one_serving(tmp_path: Path) -> None:
    r = _run(tmp_path, ON_NEWEST, ALL, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert _chosen(r) == f"{SERVICE}-00002-bbb"


def test_rolling_back_twice_goes_further_back_not_forward(tmp_path: Path) -> None:
    """The bug a naive "pick the newest that is not serving" implementation ships.

    After one rollback the NEWEST revision is the bad one we just moved away from.
    Taking it would roll straight forward into it — during an incident, while
    reporting success.
    """
    r = _run(tmp_path, _service([(f"{SERVICE}-00002-bbb", 100)]), ALL, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert _chosen(r) == f"{SERVICE}-00001-aaa"


def test_a_revision_that_never_became_ready_is_skipped(tmp_path: Path) -> None:
    """Behavioural, not a grep for the filter string.

    The middle revision failed to start. Offering it would move all traffic to a
    revision that cannot serve — an incident made strictly worse by the tool
    meant to end it.
    """
    revisions = [NEWEST, _revision(f"{SERVICE}-00002-bbb", "bbb2222",
                                   "2026-08-14T10:00:00Z", ready=False), OLDEST]
    r = _run(tmp_path, ON_NEWEST, revisions, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert _chosen(r) == f"{SERVICE}-00001-aaa"
    assert f"{SERVICE}-00002-bbb" not in r.stdout


def test_an_unready_revision_cannot_be_named_explicitly_either(tmp_path: Path) -> None:
    revisions = [NEWEST, _revision(f"{SERVICE}-00002-bbb", "bbb2222",
                                   "2026-08-14T10:00:00Z", ready=False), OLDEST]
    r = _run(tmp_path, ON_NEWEST, revisions, "--dry-run", "--to", f"{SERVICE}-00002-bbb")
    assert r.returncode == 2
    assert "not a ready revision" in r.stderr


def test_rolling_forward_is_the_same_command_with_an_explicit_target(tmp_path: Path) -> None:
    r = _run(tmp_path, _service([(f"{SERVICE}-00002-bbb", 100)]), ALL,
             "--dry-run", "--to", f"{SERVICE}-00003-ccc")
    assert r.returncode == 0, r.stderr
    assert _chosen(r) == f"{SERVICE}-00003-ccc"


# ---------------------------------------------------------------------------
# Refusals. Each is a case where guessing would be worse than stopping.
# ---------------------------------------------------------------------------


def test_a_split_traffic_service_is_refused(tmp_path: Path) -> None:
    """There is no single revision to roll back FROM under a split, and taking
    the largest share is a guess: on a 60/40, "the one behind current" means
    something different depending on which side you read from."""
    split = _service([(f"{SERVICE}-00003-ccc", 60), (f"{SERVICE}-00002-bbb", 40)])
    r = _run(tmp_path, split, ALL, "--dry-run")
    assert r.returncode == 2
    assert "split across revisions" in r.stderr
    assert "--to" in r.stderr, "a refusal must say how to proceed"


def test_a_single_revision_service_refuses_rather_than_guessing(tmp_path: Path) -> None:
    r = _run(tmp_path, _service([(f"{SERVICE}-00001-aaa", 100)]), [OLDEST], "--dry-run")
    assert r.returncode == 2
    assert "no previous ready revision" in r.stderr


def test_an_unknown_target_is_refused(tmp_path: Path) -> None:
    r = _run(tmp_path, ON_NEWEST, ALL, "--dry-run", "--to", f"{SERVICE}-00099-zzz")
    assert r.returncode == 2
    assert "not a ready revision" in r.stderr


def test_a_partial_revision_name_does_not_match(tmp_path: Path) -> None:
    """Membership is an exact comparison, not a grep. A substring or a regex
    metacharacter must not resolve to a revision the operator did not name."""
    r = _run(tmp_path, ON_NEWEST, ALL, "--dry-run", "--to", f"{SERVICE}-00001")
    assert r.returncode == 2
    assert "not a ready revision" in r.stderr


def test_targeting_the_currently_serving_revision_is_refused(tmp_path: Path) -> None:
    r = _run(tmp_path, ON_NEWEST, ALL, "--dry-run", "--to", f"{SERVICE}-00003-ccc")
    assert r.returncode == 2
    assert "already serving" in r.stderr


def test_a_trailing_to_flag_with_no_value_exits_rather_than_looping(tmp_path: Path) -> None:
    """`shift 2` on a flag with no value shifts once and leaves the flag in
    place; without `set -e` that spins forever. During an incident it is a hung
    terminal and no rollback. The 60s subprocess timeout is the real assertion."""
    r = _run(tmp_path, ON_NEWEST, ALL, "--to")
    assert r.returncode == 2
    assert "requires a value" in r.stderr


def test_an_unreadable_revision_list_refuses_rather_than_guessing(tmp_path: Path) -> None:
    """`set -uo pipefail` without `-e` means a failed read does not stop the
    script by itself — an empty list would otherwise flow into "no previous ready
    revision", which reads as a fact about the service rather than a failed call.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gcloud = bin_dir / "gcloud"
    (tmp_path / "service.json").write_text(json.dumps(ON_NEWEST))
    gcloud.write_text(
        f'#!/usr/bin/env bash\n'
        f'case " $* " in\n'
        f'  *" services describe "*) cat "{tmp_path}/service.json"; exit 0 ;;\n'
        f'esac\n'
        f'echo "boom" >&2; exit 1\n'
    )
    gcloud.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PROJECT": "example-project",
    }
    r = subprocess.run(["bash", str(SCRIPT), "--dry-run"], capture_output=True,
                       text=True, env=env, cwd=REPO_ROOT, timeout=60)
    assert r.returncode == 2
    assert "cannot list revisions" in r.stderr


def test_a_service_with_no_traffic_at_all_is_refused(tmp_path: Path) -> None:
    r = _run(tmp_path, _service([]), ALL, "--dry-run")
    assert r.returncode == 2
    assert "no revision is currently serving" in r.stderr


# ---------------------------------------------------------------------------
# The read-only modes stay read-only.
# ---------------------------------------------------------------------------


def test_a_dry_run_changes_nothing(tmp_path: Path) -> None:
    """The fake exits 99 on any call but the two reads, so a `--dry-run` that
    reached `update-traffic` surfaces as a non-zero exit."""
    r = _run(tmp_path, ON_NEWEST, ALL, "--dry-run")
    assert r.returncode == 0
    assert "unexpected call" not in r.stderr
    assert "Dry run — nothing changed" in r.stdout
    calls = (tmp_path / "calls.log").read_text()
    assert "update-traffic" not in calls


def test_list_mode_reports_and_stops(tmp_path: Path) -> None:
    r = _run(tmp_path, ON_NEWEST, ALL, "--list")
    assert r.returncode == 0, r.stderr
    for name in ("00003-ccc", "00002-bbb", "00001-aaa"):
        assert name in r.stdout
    assert "Plan:" not in r.stdout
    assert "update-traffic" not in (tmp_path / "calls.log").read_text()


def test_the_plan_shows_which_revision_is_serving(tmp_path: Path) -> None:
    """An operator picking a target under pressure needs the current one marked;
    the revision names differ only by a serial number and a hash."""
    r = _run(tmp_path, _service([(f"{SERVICE}-00002-bbb", 100)]), ALL, "--list")
    serving_line = next(ln for ln in r.stdout.splitlines() if "00002-bbb" in ln)
    assert "serving now" in serving_line


def test_the_reads_carry_the_flags_that_scope_them(tmp_path: Path) -> None:
    """The fake enforces this per-call, but assert it explicitly too: `revisions
    list` without `--service` returns every revision in the region, so "the one
    behind current" could belong to another service entirely."""
    r = _run(tmp_path, ON_NEWEST, ALL, "--list")
    assert r.returncode == 0, r.stderr
    calls = (tmp_path / "calls.log").read_text().splitlines()
    listing = next(c for c in calls if "revisions list" in c)
    assert f"--service={SERVICE}" in listing
    assert "--region=" in listing and "--project=" in listing
