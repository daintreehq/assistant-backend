"""Deployment instructions, checked against the code they deploy.

`tests/unit/test_docs_drift.py` guards the PROSE surfaces — README, CLAUDE.md,
the docs/ tree. It has never looked at `infra/`, and that is where the most
dangerous staleness was found: the Terraform config still described the
caller-funded OpenRouter contract that `auth.py` stopped implementing, told an
operator `/v1/daintree/capabilities` answers 401 when the door is open, and — the
executable half — omitted `TOGETHER_API_KEY` from the secrets it mounts, so a
deployment built from it could never pass `/readyz`.

Prose that is merely out of date is a nuisance. Prose an operator FOLLOWS during
an incident is a defect, and a `.tf` file is not prose at all. These tests derive
their expectations from `config.py` so they ratchet with the code rather than
with someone remembering.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from daintree_assistant_server.config import SERVING_KEY_ENV, SERVING_ORIGIN, Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA = REPO_ROOT / "infra"
TF_VARIABLES = INFRA / "terraform" / "variables.tf"
TF_README = INFRA / "terraform" / "README.md"

#: Every operator-facing file under infra/ that carries instructions. Terraform
#: `.tf` files are included because a wrong default is not documentation — it
#: deploys.
#: Enumerated by GLOB, not by hand. A hand-written tuple is a list of the files
#: someone remembered, and the first version of it missed `rollback.sh` and
#: `outputs.tf` — a new operator script would join them silently.
INFRA_FILES = tuple(
    sorted(
        path
        for path in INFRA.rglob("*")
        if path.is_file() and path.suffix in {".tf", ".sh", ".md"}
    )
)


def _read(path: Path) -> str:
    assert path.exists(), f"{path.relative_to(REPO_ROOT)} is missing"
    return path.read_text(encoding="utf-8")


def _required_secret_env_names() -> set[str]:
    """Every env var a PRODUCTION deployment must supply a real value for.

    The union of two checks, not just `missing_secrets()`. `STATE_SALT` is absent
    from that method — an unset salt falls back to a packaged default rather than
    being empty — and yet `weak_secrets()` rejects that default in a hardened
    environment, so a deployment omitting it fails readiness exactly as hard.
    Deriving from one method alone would let the salt be dropped from the
    Terraform without this test noticing, which is the same defect the Together
    key had.
    """
    empty = Settings(  # type: ignore[call-arg]
        TOGETHER_API_KEY="", STATE_SIGNING_KEY_CURRENT="", ENVIRONMENT="production"
    )
    names = set(empty.missing_secrets())
    # `weak_secrets` reports prose, so the env var names are recovered from it by
    # matching against the settings' own aliases rather than by parsing English.
    aliases = {
        str(field.alias)
        for field in Settings.model_fields.values()
        if field.alias
    }
    for problem in empty.weak_secrets():
        names.update(alias for alias in aliases if alias in problem)
    # The serving base URL is pinned by origin, not supplied as a secret — it is
    # a plain env var with a working default, so it is not this map's business.
    names.discard("TOGETHER_BASE_URL")
    return names


def test_terraform_mounts_every_secret_that_gates_readiness() -> None:
    """The executable defect, and the reason this file exists.

    `secret_ids` is the complete set of secrets the Cloud Run service mounts.
    A required secret missing from it deploys a revision that answers /readyz
    with 503 forever — and nothing else in the repo would notice, because the
    Terraform parses fine and the container starts.
    """
    text = _read(TF_VARIABLES)
    block = re.search(r'variable\s+"secret_ids".*?\n}\n', text, re.DOTALL)
    assert block, "the secret_ids variable block could not be located"
    for name in _required_secret_env_names():
        assert name in block.group(0), (
            f"{name} fails production readiness when unset or left at its default "
            f"(Settings.missing_secrets / weak_secrets) but is not in the Terraform "
            f"secret_ids default. A deployment built from this config can never "
            f"serve a request."
        )


def test_the_service_actually_mounts_the_secret_map() -> None:
    """The test above proves the map CONTAINS the right keys. It would still pass
    if `main.tf` stopped reading the map at all, which is the other half of the
    same defect — a complete list nobody mounts."""
    text = _read(INFRA / "terraform" / "main.tf")
    # Scoped to the container's `dynamic "env"` block specifically. A bare search
    # for `for_each = var.secret_ids` finds the IAM binding higher up the file and
    # passes even when the env block has stopped reading the map — which is the
    # exact mutation this test was written to catch, and which it missed on the
    # first attempt.
    env_block = re.search(
        r'dynamic\s+"env"\s*\{(.*?)\n      \}', text, re.DOTALL
    )
    assert env_block, 'the container\'s dynamic "env" block could not be located'
    assert re.search(r"for_each\s*=\s*var\.secret_ids", env_block.group(1)), (
        'main.tf\'s dynamic "env" block no longer iterates var.secret_ids, so the '
        "secret list has stopped being mounted as environment variables — a "
        "complete map nobody reads"
    )


@pytest.mark.parametrize("path", INFRA_FILES, ids=lambda p: p.name)
def test_no_infra_file_claims_a_caller_supplied_credential(path: Path) -> None:
    """`auth.py` ignores the Authorization header entirely: there is no caller
    key, and no bearer authenticates anything.

    An operator who believes otherwise draws two wrong conclusions that matter —
    that the endpoint is protected, and that the deployment needs no upstream
    key of its own. Both were written down in `infra/` after they stopped being
    true.
    """
    text = _read(path).lower()
    # PRESENT-TENSE claims only. `bootstrap.sh` narrates the same history
    # correctly — "it carried the caller's own key ... but the server now holds
    # the credential" — and a gate that cannot tell a superseded architecture
    # from a live instruction would force that explanation to be deleted, which
    # is the opposite of what this file is for. Each phrase below asserts the old
    # model in the present, so none of them can be history.
    forbidden = (
        "is the caller's own key",
        "bearer key is the gate",
        "your own openrouter key",
        "no server-side api key",
        "the deployment holds no api key",
        # The claim also travels as "auth exists, and it is what protects this".
        # Every one of these was live in `infra/terraform/` while the phrases
        # above were not — the wrong belief does not have a canonical wording,
        # so the list grows whenever a new phrasing of it is found.
        "app-level bearer auth is the gate",
        "the app's auth, not google iam, is the gate",
        "reachable with just the caller's bearer",
        "reachable with just the app bearer",
        "app's auth is the gate",
    )
    for claim in forbidden:
        assert claim not in text, (
            f"{path.relative_to(REPO_ROOT)} still describes the caller-funded "
            f"credential model ({claim!r}). The server holds "
            f"{SERVING_KEY_ENV} and funds every turn; see docs/KEY_HANDLING.md."
        )


@pytest.mark.parametrize("path", INFRA_FILES, ids=lambda p: p.name)
def test_no_infra_file_promises_a_401_on_an_open_route(path: Path) -> None:
    """The most actively misleading line found: a smoke step asserting
    `/v1/daintree/capabilities` returns 401 without auth.

    Following it teaches an operator that bearer auth is protecting the service.
    It is not — the route answers 200 to anyone — so the check either fails
    confusingly or, worse, is remembered as having once passed.
    """
    # Line-scoped and case-insensitive, and it recognises the honest way to
    # MENTION the old behaviour ("used to return 401") so a history note is not
    # forced out. It cannot see a claim split across two lines — a tripwire, not
    # a semantic gate, and worth saying so rather than trusting it further than
    # it goes.
    unauthorized = re.compile(r"\b(401|unauthorized)\b", re.IGNORECASE)
    historical = re.compile(
        r"\b(used to|previously|no longer|formerly|once returned|superseded)\b",
        re.IGNORECASE,
    )
    for number, line in enumerate(_read(path).splitlines(), start=1):
        if "capabilities" not in line.lower():
            continue
        if unauthorized.search(line) and not historical.search(line):
            pytest.fail(
                f"{path.relative_to(REPO_ROOT)}:{number} claims /capabilities is "
                f"auth-gated:\n  {line.strip()}\n"
                "The door is open (auth.py ignores the header); it answers 200."
            )


def test_the_origin_readiness_check_names_the_host_it_actually_pins() -> None:
    """`weak_secrets` pins the SERVING origin, and the README explains why: the
    server's key is forwarded there. Naming a different vendor's origin makes the
    explanation false and the instruction unfollowable."""
    text = _read(TF_README)
    assert SERVING_ORIGIN in text, (
        f"the Terraform README should name {SERVING_ORIGIN}, the origin "
        "Settings.weak_secrets actually pins in production"
    )
    assert "openrouter.ai` origin" not in text, (
        "the README still pins the OpenRouter origin, which no readiness check "
        "has enforced since the serving host moved"
    )
