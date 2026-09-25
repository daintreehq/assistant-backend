"""Deployment instructions, checked against the service they deploy.

`infra/` is where the most dangerous staleness in this repo has been found
before: a Terraform module still describing a credential contract `auth.py` had
stopped implementing, and omitting a secret readiness required. Prose an
operator FOLLOWS during an incident is a defect rather than a nuisance.

The service is now a stateless runbook search with no credential, no account
model and no upstream, so what is left to check is small: the operator surfaces
do not send anybody after machinery that no longer exists, and the environment
the build deploys is made of settings the server actually reads.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from daintree_assistant_server.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA = REPO_ROOT / "infra"
CLOUDBUILD = REPO_ROOT / "cloudbuild.yaml"

#: Every operator-facing file: the scripts and docs under infra/, plus the build
#: that deploys. Enumerated by GLOB, so a new script joins without anybody
#: remembering to add it.
OPERATOR_FILES = (
    *sorted(
        path
        for path in INFRA.rglob("*")
        if path.is_file() and path.suffix in {".tf", ".sh", ".md"}
    ),
    CLOUDBUILD,
)

#: Names from the removed model-orchestration server. Each was a secret, a mode
#: or a route an operator could be told to set, mount or probe; none exists now.
REMOVED_NAMES = (
    "BASETEN_API_KEY",
    "CEREBRAS_API_KEY",
    "OPENROUTER_API_KEY",
    "STATE_SIGNING_KEY",
    "AUTH_MODE",
    "ENTITLEMENT_MODE",
    "DAINTREE_ENTITLEMENT_URL",
    "STAGING_ALLOWED_SUBJECT_DIGESTS",
    "SUPABASE_",
    "subject gate",
    "subject_gate",
    "/v1/daintree/respond",
    "/v1/daintree/tasks",
    "/v1/daintree/account",
    "/v1/daintree/auth",
)


def _read(path: Path) -> str:
    assert path.exists(), f"{path.relative_to(REPO_ROOT)} is missing"
    return path.read_text(encoding="utf-8")


def test_there_are_operator_files_to_check() -> None:
    names = {path.name for path in OPERATOR_FILES}
    assert {"bootstrap.sh", "smoke.sh", "rollback.sh", "README.md", "cloudbuild.yaml"} <= names


def test_terraform_is_gone() -> None:
    """The Terraform path was deleted rather than kept in step with the build.

    A second, unmaintained definition of the deployment is the drift this module
    was first written to catch, so nothing may point an operator back at one.
    """
    assert not (INFRA / "terraform").exists(), "infra/terraform/ is back"
    assert not list(REPO_ROOT.glob("infra/**/*.tf")), "a .tf file is back under infra/"
    for path in OPERATOR_FILES:
        assert "infra/terraform" not in _read(path), (
            f"{path.relative_to(REPO_ROOT)} refers an operator to the deleted Terraform"
        )


@pytest.mark.parametrize(
    "path", OPERATOR_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_no_operator_file_names_removed_machinery(path: Path) -> None:
    """A secret to mount, a mode to set or a route to probe that no longer exists
    is an instruction that fails — during a deploy or an incident, when somebody
    is following it rather than reading it."""
    text = _read(path).lower()
    found = [name for name in REMOVED_NAMES if name.lower() in text]
    assert not found, f"{path.relative_to(REPO_ROOT)} still names {found}"


def _deployed_env_names() -> tuple[str, ...]:
    """The names the deploy step writes into the revision's environment."""
    steps = yaml.safe_load(_read(CLOUDBUILD))["steps"]
    deploy = next(step for step in steps if step.get("id") == "deploy-rc")
    script = "\n".join(deploy["args"])
    match = re.search(r"NAMES = \((.*?)\)", script, re.S)
    assert match is not None, "the NAMES tuple moved; this gate is now blind"
    names = tuple(re.findall(r'"([A-Z_][A-Z0-9_]*)"', match.group(1)))
    assert names, "the NAMES tuple parsed empty"
    return names


def test_the_build_deploys_only_settings_the_server_reads() -> None:
    """`Settings` ignores unknown variables (`extra="ignore"`), so a name the
    server does not read deploys cleanly and changes nothing — the worst way for
    a setting to fail, because it looks like it worked."""
    aliases = {
        str(field.alias) for field in Settings.model_fields.values() if field.alias
    }
    unread = sorted(set(_deployed_env_names()) - aliases)
    assert not unread, f"cloudbuild.yaml deploys variables Settings never reads: {unread}"


def test_the_smoke_probes_the_route_the_server_serves() -> None:
    """The pre-promotion gate and the public URL in the README both name the MCP
    route; a rename on one side would fail every release, or send users to 404."""
    from daintree_assistant_server.api import mcp

    paths = {getattr(route, "path", None) for route in mcp.router.routes}
    assert "/v1/daintree/mcp" in paths
    assert "/v1/daintree/mcp" in _read(INFRA / "gcp" / "smoke.sh")
    assert "/v1/daintree/mcp" in _read(INFRA / "gcp" / "README.md")
