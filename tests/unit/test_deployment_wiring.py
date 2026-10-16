"""The Cloud Build release pipeline, checked offline.

`cloudbuild.yaml` is the deployment: every push to `main` runs it, and nothing
else raises a revision. Most of what it guarantees lives in shell and Python
embedded in YAML, which is exactly where a rule can be present and wrong —
`case` patterns, quoting and `$$` escaping all fail silently. So wherever a
block decides something, it is EXTRACTED AND EXECUTED here, against a fake
`gcloud` on PATH, rather than grepped. Each extracted block has a paired test
proving the build actually reaches it, because extraction lifts a block out of
its control flow.

Cloud Build expands `$$` to a literal `$` and `${_NAME}`/`$BUILTIN` to their
values before bash parses a step, so `_render` applies the same substitution.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_CLOUDBUILD = _ROOT / "cloudbuild.yaml"
_SMOKE = _ROOT / "infra" / "gcp" / "smoke.sh"

#: What Cloud Build would substitute, in the values this repo deploys with.
_SUBSTITUTED = {
    "${_SERVICE}": "daintree-assistant",
    "${_REGION}": "us-central1",
    "${_AR_REPO}": "daintree",
    "${_RUNTIME_SA}": "daintree-assistant-run",
}
_BUILTINS = {
    "PROJECT_ID": "daintree-research",
    "COMMIT_SHA": "0123456789abcdef0123456789abcdef01234567",
    "SHORT_SHA": "0123456",
}


@pytest.fixture(scope="module")
def cloudbuild() -> dict[str, Any]:
    parsed: dict[str, Any] = yaml.safe_load(_CLOUDBUILD.read_text(encoding="utf-8"))
    return parsed


def _step(step_id: str) -> dict[str, Any]:
    """One step of the build, by id. Asserts it exists — a `next()` over a
    renamed id would otherwise raise StopIteration and read as a broken test
    rather than as a missing step."""
    steps = yaml.safe_load(_CLOUDBUILD.read_text(encoding="utf-8"))["steps"]
    found = [s for s in steps if s.get("id") == step_id]
    assert len(found) == 1, f"expected exactly one {step_id!r} step, found {len(found)}"
    step: dict[str, Any] = found[0]
    return step


@pytest.fixture(scope="module")
def deploy_step() -> dict[str, Any]:
    return _step("deploy-rc")


@pytest.fixture(scope="module")
def smoke_step() -> dict[str, Any]:
    return _step("smoke")


def _code(text: str) -> str:
    """Shell source with comment lines dropped. Several steps EXPLAIN a rule by
    naming what it forbids, and a scan over raw text would be tripped by the
    comment that documents it."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _render(text: str, *, release: Path | None = None, workspace: Path | None = None) -> str:
    """Step source as bash would receive it, with the state dirs relocated."""
    for literal, value in _SUBSTITUTED.items():
        text = text.replace(literal, value)
    for name, value in _BUILTINS.items():
        text = re.sub(rf"(?<!\$)\${name}\b", value, text)
    text = text.replace("$$", "$")
    if release is not None:
        text = text.replace("/release", str(release))
    if workspace is not None:
        text = text.replace("/workspace", str(workspace))
    return text


def _between(step_id: str, start_marker: str, end_marker: str) -> str:
    """The raw source of one step from `start_marker` up to (not including) the
    line holding `end_marker`."""
    body: str = _step(step_id)["args"][-1]
    start = body.index(start_marker)
    start = body.rfind("\n", 0, start) + 1
    end = body.index(end_marker, start)
    end = body.rfind("\n", 0, end) + 1
    block = body[start:end]
    assert block.strip(), f"{start_marker!r} extracted empty"
    return block


def _bash(script: str, env: dict[str, str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - a script this repo owns, on fixed input
        ["bash", "-c", script],
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _fake_bin(tmp_path: Path, **scripts: str) -> str:
    """A PATH whose first directory holds the given fake commands."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, body in scripts.items():
        stub = bin_dir / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
    return f"{bin_dir}:{os.environ['PATH']}"


def _write_stamped(release: Path, name: str, build_id: str, *values: str) -> None:
    """A release-state file in the shape the build writes them."""
    body = "".join(f"{line}\n" for line in (build_id, *values))
    (release / name).write_text(body, encoding="utf-8")


def _read_stamped(release: Path, name: str, build_id: str) -> str:
    """The value half of a release-state file, asserting the stamp is the
    build's own — the assertion is the point, not the value."""
    lines = (release / name).read_text(encoding="utf-8").splitlines()
    assert lines, f"{name} was written empty"
    assert lines[0] == build_id, f"{name} is stamped {lines[0]!r}, not {build_id!r}"
    return "\n".join(lines[1:]).strip()


# --- the substitutions -----------------------------------------------------------


def test_the_substitutions_are_the_ones_the_pipeline_uses(cloudbuild: dict[str, Any]) -> None:
    """Six operator values and nothing else. The server holds no credential and
    has no account posture, so a substitution beyond these is a setting nobody
    reads — or one that came back with the machinery it belonged to."""
    assert set(cloudbuild["substitutions"]) == {
        "_SERVICE",
        "_REGION",
        "_AR_REPO",
        "_RUNTIME_SA",
        "_ENVIRONMENT",
        "_INGRESS",
    }


def test_the_shipped_defaults_are_the_production_release(cloudbuild: dict[str, Any]) -> None:
    """A file that deploys on every push to main must default to what
    production is: the public MCP endpoint, hardened."""
    subs = cloudbuild["substitutions"]
    assert subs["_SERVICE"] == "daintree-assistant"
    assert subs["_ENVIRONMENT"] == "production"
    assert subs["_INGRESS"] == "all"


# --- an interpolated substitution is a name, or the build stops ---------------
#
# Four substitutions are expanded into SHELL SOURCE rather than passed as
# environment variables, because they identify resources inside gcloud arguments
# across four steps. Cloud Build expands them before bash parses, so each one is a
# fragment of the program: a `_RUNTIME_SA` of `x"); SERVICE_STATE=absent; REST=("`
# closes the flag array and runs whatever follows. The deploy-shape refusal and
# the environment checks are ordinary shell BELOW that point, so this guard is
# the precondition they stand on rather than a rule beside them.

#: The four names the build interpolates, and the only four it may.
_INTERPOLATED = frozenset({"_SERVICE", "_REGION", "_AR_REPO", "_RUNTIME_SA"})

#: Cloud Build's own substitutions that reach a script as SOURCE. `COMMIT_SHA`
#: and `SHORT_SHA` look platform-supplied and are overridable on a manual build,
#: so they are guarded like the four above. `PROJECT_ID` is not overridable, and
#: `BUILD_ID` reaches bash as environment data rather than as source.
_GUARDED_BUILTINS = frozenset({"COMMIT_SHA", "SHORT_SHA"})
_UNGUARDED_BUILTINS = frozenset({"PROJECT_ID"})


def test_only_the_guarded_names_are_ever_interpolated_into_a_script(
    cloudbuild: dict[str, Any],
) -> None:
    """The drift gate, in both directions: a name that appears in a script must
    be guarded, and a guarded name no script interpolates has stopped earning its
    check."""
    interpolated: set[str] = set()
    for step in cloudbuild["steps"]:
        body = "\n".join(str(arg) for arg in step.get("args", []))
        interpolated.update(re.findall(r"\$\{(_[A-Za-z0-9_]+)\}", body))
    assert interpolated, "no step interpolates anything — this scan found nothing to check"
    assert interpolated == set(_INTERPOLATED), (
        "the set of substitutions expanded into shell source has changed: "
        f"{sorted(interpolated)} against the guarded {sorted(_INTERPOLATED)}. "
        "Either guard the new one in substitution-guard, or pass it as an "
        "environment variable like every other operator value."
    )
    guard_env = "\n".join(_step("substitution-guard")["env"])
    for name in _INTERPOLATED:
        assert "${" + name + "}" in guard_env, (
            f"{name} is interpolated into a script and never checked"
        )

    builtins: set[str] = set()
    for step in cloudbuild["steps"]:
        body = "\n".join(str(arg) for arg in step.get("args", []))
        builtins.update(re.findall(r"(?<!\$)\$([A-Z][A-Z0-9_]*)", body))
    assert builtins, "no step interpolates a built-in — this scan found nothing"
    assert builtins <= (_GUARDED_BUILTINS | _UNGUARDED_BUILTINS), (
        "a Cloud Build substitution reaches a script as source and nothing checks "
        f"it: {sorted(builtins - _GUARDED_BUILTINS - _UNGUARDED_BUILTINS)}"
    )
    for name in _GUARDED_BUILTINS & builtins:
        assert f"{name}=${name}" in guard_env, (
            f"{name} is interpolated into a script and never checked"
        )


def test_the_guard_runs_before_anything_can_parse_a_substitution(
    cloudbuild: dict[str, Any],
) -> None:
    """A check inside `deploy-rc` could not do this: the interpolation happens
    before bash reads that step's first line. And the guard must be able to stop
    the build, which `allowFailure` would take away."""
    ids = [s["id"] for s in cloudbuild["steps"]]
    assert ids[0] == "substitution-guard", (
        "a step that parses an unchecked substitution runs before the guard"
    )
    guard = cloudbuild["steps"][0]
    assert guard.get("allowFailure") is not True, "the guard cannot fail the build"
    # NO step declares `waitFor`. The whole file relies on file order, and a
    # single `waitFor: ['-']` makes that step start in parallel with the guard.
    for step in cloudbuild["steps"]:
        assert "waitFor" not in step, (
            f"{step.get('id')} declares waitFor, which detaches it from file order"
        )
    assert "${_" not in "\n".join(guard["args"]), (
        "the guard interpolates a substitution into its own script"
    )


def _run_substitution_guard(**values: str) -> subprocess.CompletedProcess[str]:
    env = {
        "SERVICE": "daintree-assistant",
        "REGION": "us-central1",
        "AR_REPO": "daintree",
        "RUNTIME_SA": "daintree-assistant-run",
        "COMMIT_SHA": "0123456789abcdef0123456789abcdef01234567",
        "SHORT_SHA": "0123456",
    }
    env.update(values)
    body: str = _step("substitution-guard")["args"][-1]
    return _bash("set -euo pipefail\n" + body.replace("$$", "$"), env)


def test_the_guard_passes_the_values_this_repo_actually_deploys_with(
    cloudbuild: dict[str, Any],
) -> None:
    """The positive control. Every refusal below is worthless beside a check that
    refuses everything, and the defaults are what `main` releases with."""
    subs = cloudbuild["substitutions"]
    result = _run_substitution_guard(
        SERVICE=subs["_SERVICE"],
        REGION=subs["_REGION"],
        AR_REPO=subs["_AR_REPO"],
        RUNTIME_SA=subs["_RUNTIME_SA"],
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("field", "value", "id_"),
    [
        pytest.param(
            "RUNTIME_SA", 'x"); SERVICE_STATE=absent; REST=("', "runtime_sa_injection"
        ),
        pytest.param("SERVICE", "daintree-assistant$(id)", "command_substitution"),
        pytest.param("SERVICE", "daintree-assistant`id`", "backtick"),
        pytest.param("SERVICE", "daintree-assistant; rm -rf /", "semicolon"),
        pytest.param("AR_REPO", "daintree && curl evil.example", "and_and"),
        pytest.param("REGION", "us-central1 --project=elsewhere", "flag_injection"),
        # grep matches per LINE, so a first line that looks like a name and a
        # payload after it is the shape a `grep -Eq` check waves through.
        pytest.param("SERVICE", "daintree-assistant\n; id", "newline_smuggle"),
        pytest.param("SERVICE", "", "empty"),
        pytest.param("SERVICE", "9-not-a-name", "leading_digit"),
        pytest.param("SERVICE", "Daintree-Assistant", "uppercase"),
        pytest.param("RUNTIME_SA", "a" * 64, "too_long"),
        pytest.param(
            "SHORT_SHA", "x); SERVICE_STATE=absent; REST=(", "short_sha_injection"
        ),
        pytest.param("COMMIT_SHA", "abc1234$(id)", "commit_sha_substitution"),
        # What `gcloud builds submit` produces, which tags the image with nothing.
        pytest.param("SHORT_SHA", "", "sha_absent"),
        pytest.param("COMMIT_SHA", "ABC1234", "sha_uppercase"),
        pytest.param("SHORT_SHA", "abc12", "sha_too_short"),
    ],
)
def test_the_guard_refuses_a_substitution_that_is_not_a_name(
    field: str, value: str, id_: str
) -> None:
    result = _run_substitution_guard(**{field: value})
    assert result.returncode != 0, f"{id_} was accepted as a resource name"
    assert "must be a resource NAME" in result.stderr or (
        "must be a git object name" in result.stderr
    )


def test_the_guard_never_echoes_the_value_it_refused() -> None:
    """It has just decided the value may be a program, and a build log outlives
    the build. The rule that broke is the whole diagnostic."""
    marker = "zzunlikelyzz"
    result = _run_substitution_guard(SERVICE=f'x"); {marker}; ("')
    assert result.returncode != 0
    assert marker not in result.stdout + result.stderr


# --- release state cannot be forged by the checkout --------------------------
#
# `allowFailure` made the stage markers the VERDICT: a failed step no longer
# aborts the build, so what a later step believes about an earlier one is
# whatever it finds on disk. /workspace IS the repository checkout, so state
# lives on `/release`, a build-scoped volume nothing in the tree can write, and
# every file under it opens with the id of the build that wrote it. The volume
# alone would still trust a file; the stamp alone would still be a file the repo
# could carry, stamp and all.

#: Every step that touches release state.
_RELEASE_STATE_STEPS = ("deploy-rc", "smoke", "promote", "release-gate")


def _helper_block(step_id: str) -> str:
    """The `stamp`/`stamped` pair and the empty-BUILD_ID refusal, verbatim."""
    body: str = _step(step_id)["args"][-1]
    start = body.index("stamp() {")
    start = body.rfind("\n", 0, start) + 1
    check = body.index("BUILD_ID is empty", start)
    end = body.index("\n", body.index("fi\n", check)) + 1
    block = body[start:end]
    assert block.strip(), f"the release-state helpers extracted empty from {step_id}"
    return block


def test_the_release_state_lives_outside_the_repository_checkout(
    cloudbuild: dict[str, Any],
) -> None:
    """/workspace IS the checkout. A marker there is a file the repo can ship."""
    steps = {s["id"]: s for s in cloudbuild["steps"]}
    for step_id in ("release-state-init", *_RELEASE_STATE_STEPS):
        volumes = steps[step_id].get("volumes") or []
        assert {"name": "release-state", "path": "/release"} in volumes, (
            f"{step_id} does not mount the release-state volume, so whatever it "
            "reads or writes is in the repository checkout"
        )
    for name in (
        "stage_deploy_ok",
        "stage_smoke_ok",
        "stage_promote_ok",
        "skip_smoke",
        "rc_tag",
        "rc_url",
        "rc_revision",
    ):
        for step in cloudbuild["steps"]:
            script = "\n".join(step.get("args", []))
            assert f"/workspace/{name}" not in script, (
                f"{step.get('id')} keeps {name} in the checkout"
            )


def test_the_release_state_is_emptied_and_bound_before_anything_writes_it(
    cloudbuild: dict[str, Any],
) -> None:
    """A volume is per-build today; this step is what makes that a property of
    this file rather than of the platform's current behaviour."""
    ids = [s["id"] for s in cloudbuild["steps"]]
    init = ids.index("release-state-init")
    for step_id in _RELEASE_STATE_STEPS:
        assert init < ids.index(step_id), (
            f"release state is initialised after {step_id} has already used it"
        )
    script = "\n".join(cloudbuild["steps"][init]["args"])
    assert "find /release -mindepth 1 -delete" in script
    assert "/release/build_id" in script


def _run_init(tmp_path: Path, *, build_id: str) -> subprocess.CompletedProcess[str]:
    release = tmp_path / "release"
    release.mkdir(parents=True)
    # What a checkout-shaped forgery looks like once it has been copied in: the
    # exact filenames the verdict is assembled from.
    for name in ("stage_deploy_ok", "stage_smoke_ok", "stage_promote_ok"):
        (release / name).write_text("", encoding="utf-8")
    body: str = _step("release-state-init")["args"][-1]
    return _bash("set -euo pipefail\n" + _render(body, release=release), {"BUILD_ID": build_id})


def test_a_build_with_no_id_cannot_stamp_release_state(tmp_path: Path) -> None:
    """A marker stamped with the empty string matches a check for the empty
    string, so every reader below would be verifying nothing while looking as
    though it verified something."""
    result = _run_init(tmp_path, build_id="")
    assert result.returncode != 0
    assert "BUILD_ID is empty" in result.stderr


def test_the_init_step_clears_state_it_did_not_write(tmp_path: Path) -> None:
    result = _run_init(tmp_path, build_id="build-1")
    assert result.returncode == 0, result.stderr
    release = tmp_path / "release"
    assert sorted(p.name for p in release.iterdir()) == ["build_id"]
    assert release.joinpath("build_id").read_text(encoding="utf-8").strip() == "build-1"


def test_every_step_reads_release_state_through_the_same_check() -> None:
    """Four copies of a trust check that drift apart are worse than one.

    They are copies because Cloud Build gives a step no way to share a shell
    function with another, and sourcing one from the volume would put the trust
    check itself in a file — the shape this mechanism exists to distrust.
    """
    blocks = {step: _helper_block(step) for step in _RELEASE_STATE_STEPS}
    reference = blocks["deploy-rc"]
    assert "stamped()" in reference and "stamp()" in reference
    assert '"$$STAMP" = "$$BUILD_ID"' in reference, (
        "the check no longer compares the file's stamp against this build"
    )
    for step_id, block in blocks.items():
        assert block == reference, (
            f"{step_id}'s copy of the release-state check has drifted from deploy-rc's"
        )


def test_the_three_fallible_steps_record_rather_than_abort(
    cloudbuild: dict[str, Any],
) -> None:
    """`allowFailure` here is what makes the cleanup reachable: without it the
    build stops at the failing step with the candidate tag still attached."""
    steps = {s["id"]: s for s in cloudbuild["steps"]}
    for name in ("deploy-rc", "smoke", "promote"):
        assert steps[name].get("allowFailure") is True, (
            f"{name} aborts the build on failure, so the cleanup below it never runs"
        )
    assert steps["release-gate"].get("allowFailure") is not True, (
        "the step that renders the verdict must be able to fail the build"
    )
    assert cloudbuild["steps"][-1]["id"] == "release-gate", (
        "the cleanup must be last, or a step after it would run with the "
        "candidate already retired"
    )


@pytest.mark.parametrize(
    ("step_id", "marker"),
    [("smoke", "stage_deploy_ok"), ("promote", "stage_smoke_ok")],
)
def test_a_step_declines_without_its_predecessors_marker(step_id: str, marker: str) -> None:
    """`allowFailure` means a step now runs after its predecessor failed, so each
    one has to decline for itself — and ask through `stamped`, which answers
    "did THIS build write it"."""
    script = "\n".join(_step(step_id)["args"])
    assert f"stamped {marker}" in script, f"{step_id} runs whatever happened before it"
    assert f"/workspace/{marker}" not in script


# --- deploy-rc: the environment and ingress checks, run rather than read -------

_VALIDATION_START = 'case "$$ENVIRONMENT" in'
_VALIDATION_END = "# The WHOLE environment, replaced on every release"


def test_the_validation_actually_runs_before_the_deploy() -> None:
    """The half executing the block cannot prove: that the build reaches it,
    ahead of the deploy and not behind an early successful exit."""
    body: str = _step("deploy-rc")["args"][-1]
    start = body.index(_VALIDATION_START)
    assert "gcloud run deploy" in body[start:], "the validation runs after the deploy"
    assert "exit 0" not in body[:start], "an early successful exit would skip it"
    for guard in ("if false", "if [ 1 -eq 0 ]", "if /bin/false"):
        assert guard not in body, f"a guard that never runs: {guard}"


def _validate(**values: str) -> subprocess.CompletedProcess[str]:
    env = {"ENVIRONMENT": "production", "INGRESS": "all"}
    env.update(values)
    block = _render(_between("deploy-rc", _VALIDATION_START, _VALIDATION_END))
    return _bash("set -euo pipefail\n" + block, env)


@pytest.mark.parametrize("value", ["development", "test", "staging", "production"])
def test_the_build_accepts_every_environment_the_server_parses(value: str) -> None:
    result = _validate(ENVIRONMENT=value)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("value", ["", "prod", "Production", "staging "])
def test_the_build_refuses_an_environment_the_server_would_not_parse(value: str) -> None:
    """`Settings.environment` is a closed set, so a typo would come up as an
    unready revision after the image was built and pushed. Refused here it costs
    seconds and names the mistake."""
    result = _validate(ENVIRONMENT=value)
    assert result.returncode != 0
    assert "_ENVIRONMENT must be" in result.stderr


@pytest.mark.parametrize("value", ["all", "internal", "internal-and-cloud-load-balancing"])
def test_the_build_accepts_every_ingress_cloud_run_accepts(value: str) -> None:
    result = _validate(INGRESS=value)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("value", ["", "public", "ALL", "internal-and-load-balancing"])
def test_the_build_refuses_an_ingress_cloud_run_would_reject(value: str) -> None:
    """A typo here fails AFTER the image is built and pushed. The empty string is
    in the list on purpose: `gcloud run deploy --ingress=` is an error, not an
    omission."""
    result = _validate(INGRESS=value)
    assert result.returncode != 0
    assert "_INGRESS must be" in result.stderr


def test_the_deploy_step_takes_operator_values_as_environment(
    deploy_step: dict[str, Any],
) -> None:
    """`_ENVIRONMENT` and `_INGRESS` are not guarded names, so they must reach
    bash as DATA. An inline `${_ENVIRONMENT}` would be source code nothing checks."""
    assert set(deploy_step["env"]) == {
        "BUILD_ID=$BUILD_ID",
        "SERVICE=${_SERVICE}",
        "ENVIRONMENT=${_ENVIRONMENT}",
        "INGRESS=${_INGRESS}",
        "LOG_LEVEL=INFO",
    }
    script = "\n".join(deploy_step["args"])
    assert "${_ENVIRONMENT}" not in script and "${_INGRESS}" not in script


def test_ingress_comes_from_the_substitution(deploy_step: dict[str, Any]) -> None:
    script = "\n".join(deploy_step["args"])
    assert "--ingress=all" not in script, "ingress is hardcoded"
    assert '--ingress="$$INGRESS"' in script


# --- the revision's environment is exactly what the build generates -----------


def _env_file_program() -> str:
    body: str = _step("deploy-rc")["args"][-1]
    opener = body.index("<<'PYEOF' > /workspace/env_vars.json")
    start = body.index("\n", opener) + 1
    end = body.index("\nPYEOF", start)
    program = body[start:end]
    assert program.strip(), "the env-file program extracted empty"
    return program


def _generate_env_file(env: dict[str, str]) -> dict[str, str]:
    result = subprocess.run(  # noqa: S603 - a program this repo owns, on fixed input
        ["python3", "-c", _env_file_program()],
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    generated: dict[str, str] = json.loads(result.stdout)
    return generated


def test_the_env_file_holds_exactly_environment_and_log_level() -> None:
    """`--env-vars-file` REPLACES the service's whole environment, so this file is
    the complete list of what a revision is configured with. Anything else in the
    build's environment — including a stale provider key an operator exported —
    must not ride along."""
    generated = _generate_env_file(
        {
            "ENVIRONMENT": "production",
            "LOG_LEVEL": "INFO",
            "INGRESS": "all",
            "BUILD_ID": "b-1",
            "AUTH_MODE": "enforce",
            "BASETEN_API_KEY": "sk-not-for-the-revision",
        }
    )
    assert generated == {"ENVIRONMENT": "production", "LOG_LEVEL": "INFO"}


def test_a_blank_value_is_left_to_the_server_default() -> None:
    generated = _generate_env_file({"ENVIRONMENT": "staging", "LOG_LEVEL": "  "})
    assert generated == {"ENVIRONMENT": "staging"}


def test_the_env_file_names_are_a_closed_tuple(deploy_step: dict[str, Any]) -> None:
    script = "\n".join(deploy_step["args"])
    names = re.search(r"NAMES = \((.*?)\)", script, re.S)
    assert names is not None, "the NAMES tuple moved; this gate is now blind"
    assert re.findall(r'"([A-Z_]+)"', names.group(1)) == ["ENVIRONMENT", "LOG_LEVEL"]


def test_the_deploy_actually_consumes_the_generated_env_file(
    deploy_step: dict[str, Any],
) -> None:
    script = _code("\n".join(deploy_step["args"]))
    assert "--env-vars-file=/workspace/env_vars.json" in script
    for flag in ("--set-env-vars", "--update-env-vars"):
        assert flag not in script, f"{flag} bypasses the generated file"


# --- nothing is mounted from Secret Manager -----------------------------------


def test_the_deploy_clears_secrets_and_never_sets_one(deploy_step: dict[str, Any]) -> None:
    """The server holds no credential. `--clear-secrets` is what drops the
    provider keys earlier revisions carried; without it a new revision inherits
    every mount the last one had."""
    script = _code("\n".join(deploy_step["args"]))
    flags = script[script.index("FLAGS=(") : script.index(")", script.index("FLAGS=("))]
    assert "--clear-secrets" in flags, "the flag array both deploy shapes use lost it"
    for step in yaml.safe_load(_CLOUDBUILD.read_text(encoding="utf-8"))["steps"]:
        code = _code("\n".join(str(arg) for arg in step.get("args", [])))
        for flag in ("--set-secrets", "--update-secrets"):
            assert flag not in code, f"{step.get('id')} mounts a secret with {flag}"


def test_the_build_reads_no_secret_at_all(cloudbuild: dict[str, Any]) -> None:
    """The model comes from a bucket the build service account can read, not from a
    GitHub token: no secret is declared and no step is handed one."""
    assert "availableSecrets" not in cloudbuild
    assert [s["id"] for s in cloudbuild["steps"] if s.get("secretEnv")] == []


def test_the_selector_is_fetched_by_lock_before_the_image_is_built(
    cloudbuild: dict[str, Any],
) -> None:
    """`fetch-selector` runs `selector_store.py fetch`, which checks every file against
    selector.lock, and it runs before `build` copies the checkout into the image."""
    ids = [s["id"] for s in cloudbuild["steps"]]
    assert ids.index("fetch-selector") < ids.index("build")
    step = cloudbuild["steps"][ids.index("fetch-selector")]
    assert "training/selector_store.py fetch" in "\n".join(step["args"])
    assert "allowFailure" not in step, "a model that did not arrive must stop the build"


def test_the_build_never_reads_a_secret_value() -> None:
    """`versions access` returns a payload, and a build log outlives the build."""
    for path in (_CLOUDBUILD, _ROOT / "infra" / "gcp" / "bootstrap.sh", _SMOKE):
        offending = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if "versions access" in line and not line.lstrip().startswith("#")
        ]
        assert not offending, f"{path.name} reads a secret's payload: {offending}"


# --- the deploy shape: absent and unreadable are different answers ------------

_SHAPE_START = ": > /workspace/service_err"
_SHAPE_END = 'echo "Deploy shape:'


def test_the_deploy_shape_is_decided_before_the_deploy_and_consumed_by_it() -> None:
    body: str = _step("deploy-rc")["args"][-1]
    end = body.index(_SHAPE_END)
    assert "gcloud run deploy" not in body[:end], "a deploy runs before the shape is known"
    assert 'if [ "$$DEPLOY_SHAPE" = "rc" ]; then' in body[end:], (
        "nothing branches on the shape this block decided"
    )
    # One describe, whose stderr is read. A boolean `if gcloud ... describe` is
    # how a denied permission and a missing service became the same answer.
    assert "if gcloud run services describe" not in _code(body)


_DESCRIBE_STUB = """#!/bin/bash
case "$DESCRIBE" in
  exists) echo https://daintree-assistant-xyz.a.run.app; exit 0 ;;
  missing) echo "ERROR: (gcloud.run.services.describe) Cannot find service [daintree-assistant]" >&2; exit 1 ;;
  not_found) echo "ERROR: NOT_FOUND: Resource not found" >&2; exit 1 ;;
  denied) echo "ERROR: PERMISSION_DENIED: Permission 'run.services.get' denied" >&2; exit 1 ;;
  transient) echo "ERROR: (gcloud.run.services.describe) HTTPError 503" >&2; exit 1 ;;
  silent) exit 1 ;;
esac
"""


def _run_deploy_shape(tmp_path: Path, describe: str) -> subprocess.CompletedProcess[str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    path = _fake_bin(tmp_path, gcloud=_DESCRIBE_STUB)
    block = _render(_between("deploy-rc", _SHAPE_START, _SHAPE_END), workspace=workspace)
    return _bash(
        "set -euo pipefail\n" + block + 'echo "SHAPE=$DEPLOY_SHAPE"\n',
        {"PATH": path, "DESCRIBE": describe},
    )


@pytest.mark.parametrize(
    ("describe", "shape"),
    [("exists", "rc"), ("missing", "create"), ("not_found", "create")],
)
def test_the_two_known_states_choose_the_two_real_shapes(
    tmp_path: Path, describe: str, shape: str
) -> None:
    """The positive control. `missing` is the wording `gcloud run services
    describe` really uses — "Cannot find service" — and it has to reach the
    create path, or a fresh project can never receive its first release."""
    result = _run_deploy_shape(tmp_path, describe)
    assert result.returncode == 0, result.stderr
    assert f"SHAPE={shape}" in result.stdout


@pytest.mark.parametrize("describe", ["denied", "transient", "silent"])
def test_an_unreadable_service_never_reaches_the_full_traffic_path(
    tmp_path: Path, describe: str
) -> None:
    """The create path deploys at 100% behind a /readyz check alone — no
    readback, no smoke. Reading a denied permission or a flake as "absent" would
    put an existing service onto an unsmoked revision and report green."""
    result = _run_deploy_shape(tmp_path, describe)
    assert result.returncode != 0
    assert "refusing to guess" in result.stderr
    assert "SHAPE=" not in result.stdout


# --- one candidate tag per build, proved by RUNNING the recording -------------


def _rc_recording_script() -> str:
    """The tag derivation and metadata recording, as runnable bash.

    The property under test is a race between two builds, and no substring
    assertion can settle it: the old code contained `--tag=candidate` and
    `rc_revision`, everything a grep could ask for, and still recorded whichever
    revision held the shared tag when the describe ran. `FLAGS` is supplied here;
    the release-state helpers come from the SAME step rather than a local
    imitation of them.
    """
    body: str = _step("deploy-rc")["args"][-1]
    start = body.index('RC_TAG="rc-')
    start = body.rfind("\n", 0, start) + 1
    end = body.index("\n", body.index('echo "Release candidate:', start))
    return _helper_block("deploy-rc") + "FLAGS=(--quiet)\n" + body[start:end] + "\n"


#: A `gcloud` that races like a real overlapping build: `describe` lands a
#: SECOND build's revision between our deploy and our describe, and emits it
#: FIRST, so a recording reaching for `traffic[0]` would find the wrong row.
_RACING_GCLOUD = """#!/bin/bash
set -euo pipefail
STATE="$TRAFFIC_STATE"
row() { printf '{"tag": "%s", "url": "https://%s---svc.run.app", "revisionName": "svc-%s"}' "$1" "$1" "$1"; }
case "$*" in
  *"run deploy"*)
    for arg in "$@"; do
      case "$arg" in
        --tag=*) MINE="${arg#--tag=}" ;;
      esac
    done
    # A tag names ONE revision: deploying under a tag another revision holds
    # MOVES it rather than adding a second row.
    if [ -f "$STATE" ]; then
      grep -v "\\"tag\\": \\"$MINE\\"" "$STATE" > "$STATE.tmp" || true
      mv "$STATE.tmp" "$STATE"
    fi
    row "$MINE" >> "$STATE"
    echo >> "$STATE"
    ;;
  *"run services describe"*"--format=json"*)
    printf '{"status": {"traffic": ['
    { row "$INTERLOPER"; echo; cat "$STATE"; } | grep -v '^$' | paste -sd, -
    printf ']}}'
    ;;
esac
"""


def _run_rc_recording(
    *, build_id: str, interloper: str, tmp_path: Path
) -> tuple[subprocess.CompletedProcess[str], Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    release = tmp_path / "release"
    release.mkdir(parents=True)
    path = _fake_bin(tmp_path, gcloud=_RACING_GCLOUD)
    script = _render(_rc_recording_script(), release=release, workspace=workspace)
    result = _bash(
        "set -euo pipefail\n" + script,
        {
            "PATH": path,
            "BUILD_ID": build_id,
            "INTERLOPER": interloper,
            "TRAFFIC_STATE": str(tmp_path / "traffic"),
        },
    )
    return result, release


def test_a_concurrent_build_cannot_make_us_record_its_revision(tmp_path: Path) -> None:
    """With a tag only this build can name, the interloper's row is not ours."""
    build_id = "11111111-2222-3333-4444-555555555555"
    result, release = _run_rc_recording(
        build_id=build_id, interloper="rc-99999999aaaa", tmp_path=tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert _read_stamped(release, "rc_tag", build_id) == "rc-111111112222"
    revision = _read_stamped(release, "rc_revision", build_id)
    url = _read_stamped(release, "rc_url", build_id)
    assert revision == "svc-rc-111111112222", f"recorded a concurrent build's {revision!r}"
    assert "99999999" not in url


def test_two_builds_derive_two_different_tags(tmp_path: Path) -> None:
    """Derived from BUILD_ID, never from the commit: a re-run of a failed release
    shares a commit sha, and is exactly the overlapping pair this separates."""
    tags = set()
    for i, build_id in enumerate(
        ("aaaaaaaa-1111-2222-3333-444444444444", "bbbbbbbb-1111-2222-3333-444444444444")
    ):
        result, release = _run_rc_recording(
            build_id=build_id, interloper="rc-none", tmp_path=tmp_path / f"run{i}"
        )
        assert result.returncode == 0, result.stderr
        tags.add(_read_stamped(release, "rc_tag", build_id))
    assert len(tags) == 2, f"two builds shared one tag: {tags}"


def test_the_derived_tag_is_a_legal_cloud_run_tag(tmp_path: Path) -> None:
    """Cloud Run rejects a tag that is not a DNS label, and one that fails does
    so AFTER the image has been built and pushed."""
    build_id = "AAAA-BBBB-cccc-dddd-eeee-ffff-0000-1111"
    result, release = _run_rc_recording(build_id=build_id, interloper="rc-none", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    tag = _read_stamped(release, "rc_tag", build_id)
    assert re.fullmatch(r"[a-z][a-z0-9-]{2,62}", tag), tag
    assert len(tag) <= 15, f"tag {tag!r} is longer than the derivation bounds it to"


def test_a_build_with_no_usable_id_refuses_rather_than_sharing_a_name(
    tmp_path: Path,
) -> None:
    result, _ = _run_rc_recording(build_id="!!!!", interloper="rc-none", tmp_path=tmp_path)
    assert result.returncode != 0
    assert "refusing to share a tag" in result.stderr


def test_no_step_still_names_the_shared_candidate_tag(cloudbuild: dict[str, Any]) -> None:
    """A single leftover `--tag=candidate` puts the whole race back."""
    for step in cloudbuild["steps"]:
        script = _code("\n".join(step.get("args", [])))
        assert "--tag=candidate" not in script, step.get("id")
        assert '"candidate"' not in script, step.get("id")


# --- the environment readback, run rather than read --------------------------


def test_the_deploy_reads_the_recorded_revision_back() -> None:
    """From the revision this build recorded, not the service — which would
    describe whatever is serving — and against the file the build generated."""
    body: str = _step("deploy-rc")["args"][-1]
    assert 'gcloud run revisions describe "$$(stamped rc_revision)"' in body
    readback = body[body.index("gcloud run revisions describe") :]
    assert "/workspace/env_vars.json" in readback
    assert "|| exit 1" in readback.splitlines()[2], "the readback's verdict is ignored"


def _readback_program() -> str:
    body: str = _step("deploy-rc")["args"][-1]
    segment = body[body.index("gcloud run revisions describe") :]
    start = segment.index("\n", segment.index("<<'PYEOF'")) + 1
    end = segment.index("\nPYEOF", start)
    program = segment[start:end]
    assert program.strip(), "the readback program extracted empty"
    return program


def _run_readback(
    tmp_path: Path, *, wanted: dict[str, str], revision_env: list[dict[str, Any]]
) -> subprocess.CompletedProcess[str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "env_vars.json").write_text(json.dumps(wanted), encoding="utf-8")
    (workspace / "revision_readback_json").write_text(
        json.dumps({"spec": {"containers": [{"env": revision_env}]}}), encoding="utf-8"
    )
    return subprocess.run(  # noqa: S603 - a program this repo owns, on fixed input
        ["python3", "-c", _render(_readback_program(), workspace=workspace)],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=60,
    )


_WANTED = {"ENVIRONMENT": "production", "LOG_LEVEL": "INFO"}


def _generated_revision() -> list[dict[str, Any]]:
    """The revision this build asks for, plus what the platform injects."""
    return [
        {"name": "ENVIRONMENT", "value": "production"},
        {"name": "LOG_LEVEL", "value": "INFO"},
        {"name": "PORT", "value": "8080"},
        {"name": "K_SERVICE", "value": "daintree-assistant"},
        {"name": "CLOUD_RUN_JOB", "value": ""},
    ]


def _secret(name: str) -> dict[str, Any]:
    return {"name": name, "valueFrom": {"secretKeyRef": {"name": name.lower(), "key": "latest"}}}


def test_the_readback_accepts_the_revision_the_build_generated(tmp_path: Path) -> None:
    """Platform names ride along and must not fail a release."""
    result = _run_readback(tmp_path, wanted=_WANTED, revision_env=_generated_revision())
    assert result.returncode == 0, result.stderr
    assert "no secrets" in result.stdout


def test_the_readback_refuses_an_application_variable_nobody_deployed(
    tmp_path: Path,
) -> None:
    """`--env-vars-file` replaces the whole environment, so a variable this build
    did not generate got there some other way — a manual `--update-env-vars` a
    release was meant to wipe, or a deploy that did not take."""
    env = [*_generated_revision(), {"name": "AUTH_MODE", "value": "open"}]
    result = _run_readback(tmp_path, wanted=_WANTED, revision_env=env)
    assert result.returncode != 0
    assert "AUTH_MODE" in result.stderr


@pytest.mark.parametrize(
    "name", ["BASETEN_API_KEY", "STATE_SIGNING_KEY_CURRENT", "ANYTHING_AT_ALL"]
)
def test_the_readback_refuses_any_secret_reference(tmp_path: Path, name: str) -> None:
    """The server has no use for a secret, so ANY mount is wrong — the provider
    keys `--clear-secrets` should have dropped, or one nobody has heard of."""
    env = [*_generated_revision(), _secret(name)]
    result = _run_readback(tmp_path, wanted=_WANTED, revision_env=env)
    assert result.returncode != 0
    assert "mounts secrets" in result.stderr and name in result.stderr


def test_the_readback_refuses_a_missing_or_different_value(tmp_path: Path) -> None:
    env = [e for e in _generated_revision() if e["name"] != "LOG_LEVEL"]
    missing = _run_readback(tmp_path / "missing", wanted=_WANTED, revision_env=env)
    assert missing.returncode != 0
    assert "is missing: LOG_LEVEL" in missing.stderr

    env = [
        {**e, "value": "staging"} if e["name"] == "ENVIRONMENT" else e
        for e in _generated_revision()
    ]
    differing = _run_readback(tmp_path / "differing", wanted=_WANTED, revision_env=env)
    assert differing.returncode != 0
    assert "different value for: ENVIRONMENT" in differing.stderr


def test_the_readback_never_prints_a_value(tmp_path: Path) -> None:
    """Diagnostics name KEYS only. A `value` beside a `valueFrom` would be a
    payload, and a value an operator mis-set could be a pasted credential; a
    build log outlives the build."""
    env = [
        *_generated_revision(),
        {**_secret("SOME_KEY"), "value": "sk-live-payload"},
        {"name": "STRAY_SETTING", "value": "sk-stray-value"},
    ]
    result = _run_readback(tmp_path, wanted=_WANTED, revision_env=env)
    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "sk-live-payload" not in output and "sk-stray-value" not in output


# --- the whole deploy-rc step, run end to end ---------------------------------
#
# The blocks above are proved one at a time. This runs the step as Cloud Build
# would, against a `gcloud` that records every deploy argument, so the
# guarantees that only exist in the COMBINATION — the flags a deploy is actually
# given, the environment actually written, the marker actually stamped — are
# checked where they happen.

_DEPLOY_GCLOUD = """#!/bin/bash
set -uo pipefail
case "$*" in
  *"run deploy"*)
    printf '%s\\n' "$@" > "$DEPLOY_LOG"
    touch "$STATE_DIR/created"
    for arg in "$@"; do
      case "$arg" in --tag=*) echo "${arg#--tag=}" > "$STATE_DIR/tag" ;; esac
    done
    ;;
  *"run services describe"*"--format=json"*)
    TAG="$(cat "$STATE_DIR/tag")"
    printf '{"status": {"traffic": [{"tag": "%s", "url": "https://%s---svc.run.app", "revisionName": "svc-00002-new"}]}}' "$TAG" "$TAG"
    ;;
  *"run services describe"*)
    if [ "$SERVICE_EXISTS" = "yes" ] || [ -f "$STATE_DIR/created" ]; then
      echo https://daintree-assistant-xyz.a.run.app
    else
      echo "ERROR: (gcloud.run.services.describe) Cannot find service [daintree-assistant]" >&2
      exit 1
    fi
    ;;
  *"run revisions describe"*)
    cat "$REVISION_JSON"
    ;;
  *"auth print-identity-token"*)
    echo identity-token
    ;;
esac
"""

#: The first-deploy readiness check curls the new service; this answers 200.
_READY_CURL = """#!/bin/bash
echo 200
"""

_DEPLOY_BUILD_ID = "11111111-2222-3333-4444-555555555555"


def _run_deploy_rc(
    tmp_path: Path,
    *,
    service_exists: bool = True,
    revision_env: list[dict[str, Any]] | None = None,
    environment: str = "production",
) -> tuple[subprocess.CompletedProcess[str], Path, Path, list[str]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    release = tmp_path / "release"
    release.mkdir(parents=True)
    state = tmp_path / "gcloud-state"
    state.mkdir()
    revision_json = tmp_path / "revision.json"
    revision_json.write_text(
        json.dumps({"spec": {"containers": [{"env": revision_env or _generated_revision()}]}}),
        encoding="utf-8",
    )
    deploy_log = tmp_path / "deploy-args"
    path = _fake_bin(tmp_path, gcloud=_DEPLOY_GCLOUD, curl=_READY_CURL)
    body: str = _step("deploy-rc")["args"][-1]
    result = _bash(
        "set -euo pipefail\n" + _render(body, release=release, workspace=workspace),
        {
            "PATH": path,
            "BUILD_ID": _DEPLOY_BUILD_ID,
            "SERVICE": "daintree-assistant",
            "ENVIRONMENT": environment,
            "INGRESS": "all",
            "LOG_LEVEL": "INFO",
            # What an operator's shell might still be carrying. None of it may
            # reach the revision.
            "AUTH_MODE": "enforce",
            "BASETEN_API_KEY": "sk-stale",
            "SERVICE_EXISTS": "yes" if service_exists else "no",
            "STATE_DIR": str(state),
            "REVISION_JSON": str(revision_json),
            "DEPLOY_LOG": str(deploy_log),
        },
    )
    args = deploy_log.read_text(encoding="utf-8").splitlines() if deploy_log.exists() else []
    return result, release, workspace, args


def test_a_release_candidate_is_deployed_with_no_secrets_and_the_generated_env(
    tmp_path: Path,
) -> None:
    result, release, workspace, args = _run_deploy_rc(tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads((workspace / "env_vars.json").read_text(encoding="utf-8")) == _WANTED
    assert "--clear-secrets" in args
    assert not [a for a in args if a.startswith(("--set-secrets", "--update-secrets"))]
    assert f"--env-vars-file={workspace}/env_vars.json" in args
    assert "--no-traffic" in args
    assert "--tag=rc-111111112222" in args
    assert "--ingress=all" in args
    assert "sk-stale" not in "\n".join(args)
    assert _read_stamped(release, "rc_revision", _DEPLOY_BUILD_ID) == "svc-00002-new"
    assert _read_stamped(release, "stage_deploy_ok", _DEPLOY_BUILD_ID) == ""
    assert not (release / "skip_smoke").exists()


def test_a_candidate_whose_readback_fails_is_not_marked_deployed(tmp_path: Path) -> None:
    """The readback's refusal has to stop the step, or `stage_deploy_ok` is
    written anyway and the smoke and promotion proceed on a revision that still
    mounts a provider key."""
    result, release, _, _ = _run_deploy_rc(
        tmp_path, revision_env=[*_generated_revision(), _secret("BASETEN_API_KEY")]
    )
    assert result.returncode != 0
    assert "BASETEN_API_KEY" in result.stderr
    assert not (release / "stage_deploy_ok").exists()
    # The tag was recorded BEFORE the deploy, so release-gate can still retire it.
    assert _read_stamped(release, "rc_tag", _DEPLOY_BUILD_ID) == "rc-111111112222"


def test_the_first_deploy_hands_the_smoke_a_skip_marker(tmp_path: Path) -> None:
    """A new service is IAM-private, so readiness is checked with an identity
    token here and there is no anonymous URL for the smoke step to use."""
    result, release, _, args = _run_deploy_rc(tmp_path, service_exists=False)
    assert result.returncode == 0, result.stderr
    assert "--no-traffic" not in args and not [a for a in args if a.startswith("--tag")]
    assert "--clear-secrets" in args
    assert _read_stamped(release, "skip_smoke", _DEPLOY_BUILD_ID) == ""
    assert _read_stamped(release, "stage_deploy_ok", _DEPLOY_BUILD_ID) == ""
    assert not (release / "rc_tag").exists()


def test_an_invalid_environment_stops_the_step_before_any_deploy(tmp_path: Path) -> None:
    result, release, _, args = _run_deploy_rc(tmp_path, environment="prod")
    assert result.returncode != 0
    assert args == [], "a deploy ran with an environment the server cannot parse"
    assert not (release / "stage_deploy_ok").exists()


# --- the pre-promotion smoke step ---------------------------------------------


def test_the_smoke_step_runs_the_shared_script_rather_than_its_own_curls(
    smoke_step: dict[str, Any],
) -> None:
    """ONE definition of what a working deployment is. Two definitions drift, in
    the direction of the copy nobody runs by hand — the copy gating production."""
    script = "\n".join(smoke_step["args"])
    assert re.search(r"^\s*bash\s+infra/gcp/smoke\.sh\b", script, re.MULTILINE), (
        "the pre-promotion gate no longer executes infra/gcp/smoke.sh"
    )
    # `curl` survives only in the readiness warm-up, which is this step's own.
    assertions = [
        line for line in _code(script).splitlines() if "curl" in line and "readyz" not in line
    ]
    assert not assertions, f"the gate is inlining its own checks again: {assertions}"


def test_the_smoke_step_states_the_release_sha(smoke_step: dict[str, Any]) -> None:
    """The script ADAPTS to a host it is told nothing about. Here the whole
    question is whether the candidate is THIS commit, so the sha is stated — and
    taken as environment, because Cloud Build substitutes before bash parses."""
    assert "RELEASE_SHA=$COMMIT_SHA" in smoke_step["env"]
    invocation = next(
        line for line in _code("\n".join(smoke_step["args"])).splitlines()
        if "infra/gcp/smoke.sh" in line
    )
    assert '--expect-build-sha "$$RELEASE_SHA"' in invocation
    assert '--url "$$URL"' in invocation


def test_every_flag_the_build_passes_is_one_the_script_accepts(
    smoke_step: dict[str, Any],
) -> None:
    """A flag the parser does not know is an exit 2, which would fail every
    release with a usage error rather than a verdict."""
    invocation = next(
        line for line in _code("\n".join(smoke_step["args"])).splitlines()
        if "infra/gcp/smoke.sh" in line
    )
    parser = _SMOKE.read_text(encoding="utf-8")
    for flag in re.findall(r"(--[a-z-]+)", invocation):
        assert re.search(rf"^\s*{re.escape(flag)}\)", parser, re.MULTILINE), (
            f"the build passes {flag}, which smoke.sh does not accept"
        )


def test_the_smoke_step_keeps_the_readiness_warm_up_out_of_the_assertions(
    smoke_step: dict[str, Any],
) -> None:
    """The retry belongs to the step, not the script: an assertion suite that
    silently retried could not report a route that is intermittently reachable."""
    assert "readyz not answering yet" in "\n".join(smoke_step["args"])


def test_the_smoke_step_still_bails_out_on_the_first_deploy(
    smoke_step: dict[str, Any],
) -> None:
    """The candidate URL needs an invoker binding a new service does not have."""
    script = "\n".join(smoke_step["args"])
    assert "stamped skip_smoke" in script
    assert "/workspace/skip_smoke" not in script
    assert "stamp skip_smoke" in "\n".join(_step("deploy-rc")["args"])


def test_the_smoke_step_runs_before_the_promotion(cloudbuild: dict[str, Any]) -> None:
    ids = [s.get("id") for s in cloudbuild["steps"]]
    assert ids.index("deploy-rc") < ids.index("smoke") < ids.index("promote")


_SMOKE_DECLINE_START = "if ! stamped stage_deploy_ok"
_SMOKE_DECLINE_END = "for attempt in 1 2 3 4 5; do"


def test_the_smoke_declines_before_it_reaches_the_assertion_suite() -> None:
    """A refusal that fired after `bash infra/gcp/smoke.sh` would be a report,
    not a gate."""
    body: str = _step("smoke")["args"][-1]
    start = body.index(_SMOKE_DECLINE_START)
    assert body.index(_SMOKE_DECLINE_END) > start
    assert "bash infra/gcp/smoke.sh" in body[body.index(_SMOKE_DECLINE_END) :]
    assert "exit 0" not in body[:start], "an early successful exit would skip the marker check"


_SMOKE_BUILD_ID = "build-under-test"


def _run_smoke_decline(
    tmp_path: Path,
    *,
    release_state: dict[str, tuple[str, ...]],
    state_build_id: str = _SMOKE_BUILD_ID,
    seed_checkout: tuple[str, ...] = (),
    foreign: dict[str, tuple[str, ...]] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    release = tmp_path / "release"
    release.mkdir(parents=True)
    for name, values in release_state.items():
        _write_stamped(release, name, state_build_id, *values)
    for name, values in (foreign or {}).items():
        _write_stamped(release, name, "some-other-build", *values)
    for name in seed_checkout:
        (workspace / name).write_text("", encoding="utf-8")
    script = (
        _helper_block("smoke")
        + _between("smoke", _SMOKE_DECLINE_START, _SMOKE_DECLINE_END)
        + 'echo "URL=$$URL"\n'
    )
    result = _bash(
        "set -euo pipefail\n" + _render(script, release=release, workspace=workspace),
        {"BUILD_ID": _SMOKE_BUILD_ID},
    )
    return result, release


def test_this_builds_own_marker_starts_the_smoke(tmp_path: Path) -> None:
    """The positive control: without it every refusal below could be a script
    that refuses everything."""
    result, _ = _run_smoke_decline(
        tmp_path,
        release_state={"stage_deploy_ok": (), "rc_url": ("https://rc-1---svc.run.app",)},
    )
    assert result.returncode == 0, result.stderr
    assert "URL=https://rc-1---svc.run.app" in result.stdout


def test_a_marker_the_repository_carried_does_not_start_a_smoke(tmp_path: Path) -> None:
    """THE forgery: a checkout holding `stage_deploy_ok` and `skip_smoke`."""
    result, _ = _run_smoke_decline(
        tmp_path, release_state={}, seed_checkout=("stage_deploy_ok", "skip_smoke", "rc_url")
    )
    assert result.returncode != 0
    assert "nothing to smoke" in result.stderr


def test_a_marker_another_build_wrote_does_not_start_a_smoke(tmp_path: Path) -> None:
    result, _ = _run_smoke_decline(
        tmp_path,
        release_state={"stage_deploy_ok": (), "rc_url": ("https://someone-else",)},
        state_build_id="some-other-build",
    )
    assert result.returncode != 0
    assert "nothing to smoke" in result.stderr


def test_a_foreign_first_deploy_hand_off_does_not_skip_the_smoke(tmp_path: Path) -> None:
    """`skip_smoke` skips the entire assertion suite, so it is the most valuable
    marker to forge. An unrecognised one is simply not there."""
    result, release = _run_smoke_decline(
        tmp_path,
        release_state={"stage_deploy_ok": (), "rc_url": ("https://ours",)},
        foreign={"skip_smoke": ()},
    )
    assert result.returncode == 0, result.stderr
    assert "URL=https://ours" in result.stdout, "the foreign hand-off skipped the smoke"
    assert not (release / "stage_smoke_ok").exists()


def test_this_builds_first_deploy_hand_off_skips_and_records(tmp_path: Path) -> None:
    result, release = _run_smoke_decline(
        tmp_path, release_state={"stage_deploy_ok": (), "skip_smoke": ()}
    )
    assert result.returncode == 0, result.stderr
    assert "URL=" not in result.stdout
    assert _read_stamped(release, "stage_smoke_ok", _SMOKE_BUILD_ID) == ""


# --- the promotion ----------------------------------------------------------------


def test_the_promotion_names_the_revision_the_smoke_proved() -> None:
    """`--to-latest` re-resolves at promotion time; an overlapping build that
    landed its own revision in between would be promoted unsmoked."""
    script = _code("\n".join(_step("promote")["args"]))
    assert "--to-latest" not in script
    assert '--to-revisions="$$REVISION=100"' in script
    assert 'REVISION="$$(stamped rc_revision)"' in script


def test_the_promotion_removes_only_this_builds_tag() -> None:
    """Cleanup used to be `--remove-tags=candidate`, which every concurrent build
    also carried, so promoting A deleted the tag B was about to describe."""
    script = _code("\n".join(_step("promote")["args"]))
    assert '--remove-tags="$$RC_TAG"' in script
    assert 'RC_TAG="$$(stamped rc_tag)"' in script


# --- candidate isolation: the tag is retired on EVERY path -------------------
#
# A candidate deployed `--no-traffic --tag=rc-<build>` has a URL of its own, and
# a Cloud Run invoker binding is per-SERVICE, so on a public service that URL is
# public. The tag used to be removed only when the promotion succeeded. The
# final step now runs on every path, removes this build's own tag, and only then
# decides whether the build is red.

#: A `gcloud` for the cleanup step: it reports whichever traffic rows the test
#: sets up, and records every tag anybody asks it to remove.
_CLEANUP_GCLOUD = """#!/bin/bash
set -uo pipefail
case "$*" in
  *"run services describe"*"--format=json"*)
    [ "${DESCRIBE_OK:-yes}" = "yes" ] || exit 1
    cat "$TRAFFIC_JSON"
    ;;
  *"run services update-traffic"*)
    for arg in "$@"; do
      case "$arg" in
        --remove-tags=*) echo "${arg#--remove-tags=}" >> "$REMOVED_LOG" ;;
        --to-revisions=*|--to-latest) echo "TRAFFIC-MOVED:$arg" >> "$REMOVED_LOG" ;;
      esac
    done
    ;;
esac
exit 0
"""

_CLEANUP_BUILD_ID = "build-under-test"


def _run_cleanup(
    tmp_path: Path,
    *,
    rc_tag: str | None,
    attached_tags: list[str],
    markers: tuple[str, ...] = ("stage_deploy_ok", "stage_smoke_ok", "stage_promote_ok"),
    describe_ok: str = "yes",
    state_build_id: str = _CLEANUP_BUILD_ID,
    seed_checkout: tuple[str, ...] = (),
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """The verdict step, run against a release directory this harness writes.

    `state_build_id` is what the state is STAMPED with, a different thing from
    what the step is told its own id is — that difference is the forgery the
    stamps exist to refuse. `seed_checkout` puts files in the fake /workspace.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    release = tmp_path / "release"
    release.mkdir(parents=True)
    if rc_tag is not None:
        _write_stamped(release, "rc_tag", state_build_id, rc_tag)
    for marker in markers:
        _write_stamped(release, marker, state_build_id)
    for name in seed_checkout:
        (workspace / name).write_text("", encoding="utf-8")
    traffic = tmp_path / "traffic.json"
    traffic.write_text(
        json.dumps(
            {
                "status": {
                    "traffic": [
                        {"tag": tag, "revisionName": f"svc-{tag}", "url": f"https://{tag}"}
                        for tag in attached_tags
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    removed_log = tmp_path / "removed"
    removed_log.write_text("", encoding="utf-8")
    body: str = _step("release-gate")["args"][-1]
    result = _bash(
        # No `set -e`: the step itself runs with `-uo pipefail` only.
        "set -uo pipefail\n" + _render(body, release=release, workspace=workspace),
        {
            "PATH": _fake_bin(tmp_path, gcloud=_CLEANUP_GCLOUD),
            "BUILD_ID": _CLEANUP_BUILD_ID,
            "TRAFFIC_JSON": str(traffic),
            "REMOVED_LOG": str(removed_log),
            "DESCRIBE_OK": describe_ok,
        },
    )
    removed = [
        line for line in removed_log.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    return result, removed


def test_the_verdict_step_cannot_abort_before_its_last_line() -> None:
    """`-e` would let the first failed command skip the cleanup it exists for."""
    args = _step("release-gate")["args"]
    assert args[0] == "-uo", "release-gate runs with -e"


def test_a_failed_release_retires_its_own_candidate_tag(tmp_path: Path) -> None:
    """The leak, closed. The build still goes red — but not with a live URL on a
    revision that failed its own smoke."""
    result, removed = _run_cleanup(
        tmp_path,
        rc_tag="rc-111111112222",
        attached_tags=["rc-111111112222"],
        markers=("stage_deploy_ok",),
    )
    assert result.returncode != 0, "a failed release must still fail the build"
    assert removed == ["rc-111111112222"]
    assert "smoke" in result.stderr and "promote" in result.stderr


def test_the_cleanup_cannot_touch_a_concurrent_builds_tag(tmp_path: Path) -> None:
    result, removed = _run_cleanup(
        tmp_path,
        rc_tag="rc-111111112222",
        attached_tags=["rc-99999999aaaa"],
        markers=("stage_deploy_ok",),
    )
    assert result.returncode != 0
    assert removed == [], f"the cleanup removed a tag that is not ours: {removed}"


def test_the_cleanup_never_moves_traffic(tmp_path: Path) -> None:
    """`--remove-tags` alone says nothing about the split; a cleanup reaching for
    `--to-revisions` would be promoting or rolling back on the failure path."""
    _, removed = _run_cleanup(
        tmp_path, rc_tag="rc-111111112222", attached_tags=["rc-111111112222"]
    )
    assert not any(entry.startswith("TRAFFIC-MOVED") for entry in removed), removed


def test_a_successful_release_leaves_the_promotions_own_cleanup_alone(
    tmp_path: Path,
) -> None:
    """The promotion removed the tag inside its traffic call, and a second
    removal would be an error on the one run that went perfectly."""
    result, removed = _run_cleanup(tmp_path, rc_tag="rc-111111112222", attached_tags=[])
    assert result.returncode == 0, result.stderr
    assert removed == []
    assert "Release complete" in result.stdout


def test_a_build_that_never_tagged_anything_guesses_at_no_name(tmp_path: Path) -> None:
    result, removed = _run_cleanup(
        tmp_path, rc_tag=None, attached_tags=["rc-99999999aaaa"], markers=()
    )
    assert result.returncode != 0
    assert removed == []


def test_a_cleanup_that_cannot_confirm_the_tag_fails_the_build(tmp_path: Path) -> None:
    """A successful release whose tag might still be attached is not a success."""
    result, _ = _run_cleanup(
        tmp_path,
        rc_tag="rc-111111112222",
        attached_tags=["rc-111111112222"],
        describe_ok="no",
    )
    assert result.returncode != 0
    assert "could not determine" in result.stderr


def test_a_verdict_cannot_be_assembled_from_the_checkout(tmp_path: Path) -> None:
    """Three empty files in the repository used to be enough to say yes."""
    result, removed = _run_cleanup(
        tmp_path,
        rc_tag=None,
        attached_tags=[],
        markers=(),
        seed_checkout=("stage_deploy_ok", "stage_smoke_ok", "stage_promote_ok"),
    )
    assert result.returncode != 0
    for stage in ("deploy-rc", "smoke", "promote"):
        assert stage in result.stderr
    assert removed == []


def test_a_candidate_tag_this_build_did_not_write_is_not_removed(tmp_path: Path) -> None:
    """A pre-seeded `rc_tag` used to aim the cleanup wherever it pointed."""
    result, removed = _run_cleanup(
        tmp_path,
        rc_tag="rc-99999999aaaa",
        attached_tags=["rc-99999999aaaa"],
        state_build_id="some-other-build",
    )
    assert removed == [], f"the cleanup acted on a tag it did not write: {removed}"
    assert result.returncode != 0
    assert "this build did not write it" in result.stderr
