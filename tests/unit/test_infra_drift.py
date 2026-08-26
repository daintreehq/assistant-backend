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

from daintree_assistant_server.config import EXECUTOR_KEY_ENV, EXECUTOR_ORIGIN, Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
INFRA = REPO_ROOT / "infra"
TF_VARIABLES = INFRA / "terraform" / "variables.tf"
TF_MAIN = INFRA / "terraform" / "main.tf"
TF_README = INFRA / "terraform" / "README.md"

#: The variable an operator must set true before this module will plan at all.
ACKNOWLEDGEMENT = "acknowledge_unsupported_terraform_path"

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
    """There is no caller key: the server holds the upstream credential, and an
    account is an entitlement to spend it rather than a mapping to one of the
    caller's. (`auth.py` does read the header — it dispatches on `AUTH_MODE`, and
    the shipped `open` default is what declines to authenticate with it. An
    earlier version of this docstring said the header was ignored outright, which
    stopped being true when `AUTH_MODE` landed.)

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
        # The inverse claim, and the one `infra/terraform/` actually carried: not
        # "auth protects this" but "auth does not exist". It stopped being true
        # when `AUTH_MODE` landed — `auth.py` dispatches on the mode and `open` is
        # only the default — and it is the sentence that made an unauthenticated
        # public deployment read as unavoidable rather than as a choice.
        "ignores the authorization header",
    )
    for claim in forbidden:
        assert claim not in text, (
            f"{path.relative_to(REPO_ROOT)} still describes the caller-funded "
            f"credential model ({claim!r}). The server holds "
            f"{EXECUTOR_KEY_ENV} and funds every turn; see docs/KEY_HANDLING.md."
        )


@pytest.mark.parametrize("path", INFRA_FILES, ids=lambda p: p.name)
def test_no_infra_file_promises_a_401_on_an_open_route(path: Path) -> None:
    """The most actively misleading line found: a smoke step asserting
    `/v1/daintree/capabilities` returns 401 without auth.

    Following it teaches an operator that bearer auth is protecting the service.
    It is not: under the shipped `AUTH_MODE=open` default the route answers 200
    to anyone, so the check either fails
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
                "The shipped AUTH_MODE=open default reads no credential here; "
                "it answers 200."
            )


def test_the_origin_readiness_check_names_the_host_it_actually_pins() -> None:
    """`weak_secrets` pins the SERVING origin, and the README explains why: the
    server's key is forwarded there. Naming a different vendor's origin makes the
    explanation false and the instruction unfollowable."""
    text = _read(TF_README)
    assert EXECUTOR_ORIGIN in text, (
        f"the Terraform README should name {EXECUTOR_ORIGIN}, the origin "
        "Settings.weak_secrets actually pins in production"
    )
    assert "openrouter.ai` origin" not in text, (
        "the README still pins the OpenRouter origin, which no readiness check "
        "has enforced since the serving host moved"
    )


# --------------------------------------------------------------------------
# The Terraform path is deprecated, and these hold it deprecated.
#
# `infra/terraform/` was a SECOND way to deploy this service: 246 lines nothing
# applies, no CI gate exercises, and — because there is no terraform binary in
# this repo's toolchain — nothing can even plan. Its defaults published the
# service (`ingress = "INGRESS_TRAFFIC_ALL"`, `allow_unauthenticated = true`)
# while telling the app it was `production`, and since it sets no `AUTH_MODE` the
# application fell back to `open`: an anonymous, Daintree-funded model endpoint.
#
# The decision was to deprecate rather than reach parity, because parity means a
# second copy of the posture rules `cloudbuild.yaml` enforces — a copy nothing
# runs, which is the definition of a thing that drifts. These tests are the only
# executable check the module has, so they assert the two properties that make it
# safe to leave on disk: an apply is refused by default, and no default
# combination could publish a funded service even if it were not.
# --------------------------------------------------------------------------


def _variable_block(text: str, name: str) -> str:
    """The body of one `variable "<name>"` block.

    Terminated on a closing brace in COLUMN ZERO, which is what distinguishes the
    block's own end from the nested `validation {` inside it — HCL indents nested
    blocks, and a non-greedy match to any `}` would stop at the first one.
    """
    block = re.search(rf'variable\s+"{name}"\s*\{{.*?\n\}}\n', text, re.DOTALL)
    assert block, f'the "{name}" variable block could not be located in variables.tf'
    return block.group(0)


def _variable_default(name: str) -> str:
    block = _variable_block(_read(TF_VARIABLES), name)
    default = re.search(r"^\s*default\s*=\s*(\S.*?)\s*$", block, re.MULTILINE)
    assert default, f'the "{name}" variable declares no default'
    return default.group(1)


def _service_resource_body() -> str:
    body = re.search(
        r'resource\s+"google_cloud_run_v2_service"\s+"service"\s*\{.*?\n\}\n',
        _read(TF_MAIN),
        re.DOTALL,
    )
    assert body, "the google_cloud_run_v2_service resource could not be located in main.tf"
    return body.group(0)


def _precondition_expressions(body: str) -> list[str]:
    """The `condition` expression of every `precondition` block, in order.

    Read as `condition =` lines rather than by matching `precondition {` and what
    follows it: the blocks carry explanatory comments between the brace and the
    condition, and an anchored pattern would silently return fewer expressions
    than there are gates — a test that stops seeing a precondition passes just as
    happily as one that sees it holding.

    Only the expression. The `error_message` heredocs below them are prose and
    would swamp any match written over the whole block.
    """
    return [
        " ".join(match.split())
        for match in re.findall(r"^\s*condition\s*=\s*(.+)$", body, re.MULTILINE)
    ]


#: The three refusals, verbatim. Compared as EXACT expressions rather than by
#: looking for the variable names in them, because every interesting regression
#: here keeps the names: `!var.allow_unauthenticated` inverted to
#: `var.allow_unauthenticated`, or an ingress denylist put back where the
#: allowlist is, both mention exactly what a name-presence assertion looks for.
#: A deliberate edit updates this tuple; an accidental one fails.
EXPECTED_PRECONDITIONS = (
    "var.acknowledge_unsupported_terraform_path",
    '!var.allow_unauthenticated && contains(["INGRESS_TRAFFIC_INTERNAL_ONLY", '
    '"INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"], var.ingress)',
    'contains(["development", "test", "staging"], var.environment)',
)


def test_an_unacknowledged_terraform_plan_is_refused() -> None:
    """The gate itself. A `precondition` is evaluated during PLAN, so the refusal
    lands before Terraform proposes a single resource — as opposed to a comment
    at the top of the file, which is what used to carry this claim while the
    module applied perfectly happily."""
    assert _variable_default(ACKNOWLEDGEMENT) == "false", (
        f"{ACKNOWLEDGEMENT} must default to false, or the deprecated Terraform "
        f"path plans without anyone acknowledging what it cannot express"
    )
    assert EXPECTED_PRECONDITIONS[0] in _precondition_expressions(_service_resource_body()), (
        "the Cloud Run service resource has no precondition gating on "
        f"var.{ACKNOWLEDGEMENT}, so an apply from the deprecated path succeeds"
    )


def test_the_refusal_names_the_posture_the_module_cannot_express() -> None:
    """A gate that only says "deprecated" teaches an operator to acknowledge it.

    The message has to name the actual gap — the env vars `cloudbuild.yaml` sets
    and this module does not — because that gap, not the deprecation, is why an
    acknowledged apply is still the wrong thing to do.
    """
    body = _service_resource_body()
    for name in ("AUTH_MODE", "ENTITLEMENT_MODE", "DAINTREE_ENTITLEMENT_URL", "cloudbuild.yaml"):
        assert name in body, (
            f"the deprecation precondition does not mention {name}. An operator "
            f"reading only the failure has to learn what this path cannot set and "
            f"where the real deployment is."
        )


def test_no_default_combination_publishes_a_provider_funded_service() -> None:
    """Every default that decides reachability, checked together.

    Separately each looks like a preference. Together they were the defect: public
    ingress plus an allUsers invoker binding plus `ENVIRONMENT=production` plus the
    application's own `AUTH_MODE=open` fallback is an anonymous endpoint spending
    Daintree's Together key, produced by `terraform apply` with no `-var` at all.
    """
    assert _variable_default("allow_unauthenticated") == "false", (
        "allow_unauthenticated must default false: granting allUsers "
        "roles/run.invoker removes the only gate this module can place in front "
        "of the service, and the application's own default is AUTH_MODE=open"
    )
    ingress = _variable_default("ingress").strip('"')
    assert ingress == "INGRESS_TRAFFIC_INTERNAL_ONLY", (
        f"the ingress default is {ingress!r}. INGRESS_TRAFFIC_INTERNAL_ONLY is the "
        "only value private on its own terms — INTERNAL_LOAD_BALANCER is private "
        "only while no load balancer has been built in front of it, and this "
        "module builds none."
    )
    environment = _variable_default("environment").strip('"')
    assert environment != "production", (
        "this path may not build the production service; cloudbuild.yaml does. "
        "The old default announced production to the app while supplying none of "
        "the posture a production service needs."
    )
    # Derived, not listed: `staging` was chosen because the app hardens on it
    # exactly as on production (weak secrets rejected, the serving origin pinned,
    # local-only features refused). Dropping to `development` would look like a
    # further softening of a production default and would in fact switch every one
    # of those checks off, which is why this asks `Settings` rather than a literal.
    hardened = Settings(  # type: ignore[call-arg]
        TOGETHER_API_KEY="",
        STATE_SIGNING_KEY_CURRENT="",
        ENVIRONMENT=environment,
    ).is_hardened
    assert hardened, (
        f"ENVIRONMENT={environment!r} is not a hardened environment "
        "(Settings.is_hardened), so a service built from this module would accept "
        "the default STATE_SALT and an unpinned serving origin"
    )


def test_an_acknowledged_apply_creates_no_public_binding() -> None:
    """The second and third preconditions, and the reason the first is not enough.

    An acknowledgement variable is a thing operators set. What it buys has to stay
    private, so the public levers are refused on their own terms — and both in one
    condition, because either alone is sufficient exposure: the IAM binding
    removes Google's gate, public ingress removes the network one, and this module
    has no application gate to fall back on.

    Note what this does NOT claim, because the earlier name for it did. These
    preconditions read this module's own INPUTS; they cannot see the world. An
    acknowledged apply aimed at a service that already carries an external load
    balancer and an `allUsers` binding made somewhere else — `infra/gcp/bootstrap.sh`
    makes exactly such a binding — still updates a service the public can reach,
    and Terraform cannot revoke what it does not manage. `environment = "staging"`
    is likewise an application label and not proof that the project or service is
    not the production one. What is actually guaranteed is narrower and still worth
    having: nothing here CREATES a public binding or public ingress.

    Ingress is matched as an ALLOWLIST. A `!= "INGRESS_TRAFFIC_ALL"` denylist reads
    as equivalent and is not: the provider accepts an empty ingress string, omits
    the field from the create request, and the API applies its own default of all
    ingress — public, from a value the denylist calls safe.
    """
    conditions = _precondition_expressions(_service_resource_body())
    for expected in EXPECTED_PRECONDITIONS[1:]:
        assert expected in conditions, (
            "the Cloud Run service is missing the precondition\n"
            f"  {expected}\n"
            f"and carries instead:\n  " + "\n  ".join(conditions) + "\n"
            "An operator who sets "
            f"{ACKNOWLEDGEMENT} = true must still not be able to hand allUsers a "
            "funded endpoint, or to call the result production."
        )


def test_the_service_still_reads_the_variables_the_preconditions_guard() -> None:
    """The other half of every gate above: a precondition on `var.ingress` proves
    nothing if the resource has stopped reading `var.ingress`.

    Each of these three is a one-word edit away from making the refusals decorative
    — a hardcoded `ingress = "INGRESS_TRAFFIC_ALL"`, a `count = 1` on the invoker
    binding, a literal `ENVIRONMENT = "production"` — and every one of them leaves
    the preconditions in place and passing.
    """
    body = _service_resource_body()
    assert re.search(r"^\s*ingress\s*=\s*var\.ingress\s*$", body, re.MULTILINE), (
        "the service no longer takes its ingress from var.ingress, so the "
        "precondition allowlisting that variable guards nothing"
    )
    assert re.search(r'name\s*=\s*"ENVIRONMENT"\n\s*value\s*=\s*var\.environment', body), (
        "the ENVIRONMENT container env is no longer var.environment, so the "
        "precondition refusing production guards nothing"
    )
    invoker = re.search(
        r'resource\s+"google_cloud_run_v2_service_iam_member".*?\n\}\n',
        _read(TF_MAIN),
        re.DOTALL,
    )
    assert invoker, "the allUsers invoker binding could not be located in main.tf"
    assert re.search(r"count\s*=\s*var\.allow_unauthenticated\s*\?\s*1\s*:\s*0", invoker.group(0)), (
        "the allUsers invoker binding no longer counts on var.allow_unauthenticated, "
        "so the precondition holding that variable false guards nothing"
    )


#: A `terraform` sub-command that an operator could copy and run. The point is not
#: the word — the files have to be able to discuss the path they are deprecating —
#: but a RUNNABLE recipe presented without refusal.
#: Anchored at the START of the line, after optional indentation and an optional
#: shell prompt. That is what separates a command a reader can copy from a mention
#: inside a sentence — and these files have to be able to DISCUSS the path they
#: deprecate ("`terraform destroy` still works", "there is no `terraform apply`
#: recipe here"), which a bare word-search forbids.
#: Global flags are allowed between the two words: `terraform -chdir=infra/terraform
#: apply` is as runnable as the bare form and was invisible to the first version of
#: this pattern.
_TERRAFORM_INVOCATION = re.compile(
    r"^\s*(?:[$>]\s*)?`?terraform\s+(?:-\S+\s+)*(?:apply|plan|init|destroy)\b",
    re.IGNORECASE,
)
_REFUSAL = re.compile(
    r"\b(deprecat\w*|unsupported|do not|don't|never|no longer|there is no|refus\w*|"
    r"fails?|failed|removed|instead of)\b",
    re.IGNORECASE,
)


def _unrefused_terraform_invocations(text: str) -> list[tuple[int, str]]:
    """Lines offering a terraform sub-command without saying it is refused.

    Line-scoped, like the 401 tripwire above, and just as limited: it cannot see a
    recipe whose refusal sits in the paragraph above it. A tripwire, not a
    semantic gate. It is worth having anyway because the recipe it exists to catch
    was a fenced `terraform apply \\` block with a `-var project_id=PROJECT` on the
    next line — copy-paste shaped, and nothing in the repo objected.
    """
    return [
        (number, line)
        for number, line in enumerate(text.splitlines(), start=1)
        if _TERRAFORM_INVOCATION.match(line) and not _REFUSAL.search(line)
    ]


@pytest.mark.parametrize("path", INFRA_FILES, ids=lambda p: p.name)
def test_no_infra_file_offers_a_runnable_terraform_recipe(path: Path) -> None:
    """`infra/terraform/README.md` opened with "**Two deployment modes.** The
    defaults are **staging-first**" and then gave a working `terraform apply`, a
    live `curl` against the resulting URL, and a billed `POST /v1/daintree/respond`.

    An operator reads the README, not the header comment in `main.tf` that called
    the module illustrative. So the README is what had to stop inviting them.
    """
    offered = _unrefused_terraform_invocations(_read(path))
    if offered:
        number, line = offered[0]
        pytest.fail(
            f"{path.relative_to(REPO_ROOT)}:{number} offers a terraform command "
            f"without saying the path is deprecated:\n  {line.strip()}\n"
            "The deployment is cloudbuild.yaml (push to main); infra/gcp/ holds "
            "the supporting scripts."
        )


def test_the_terraform_recipe_scan_catches_the_recipe_it_exists_to_catch() -> None:
    """Negative control. The scan above passes over every real file, which proves
    nothing until it is shown failing on the text it was written for — this is the
    recipe that was in the README, verbatim."""
    was_in_the_readme = (
        "```bash\n"
        "terraform apply \\\n"
        "  -var project_id=PROJECT \\\n"
        "  -var image=REGION-docker.pkg.dev/PROJECT/REPO/daintree-assistant@sha256:...\n"
        "```\n"
    )
    assert _unrefused_terraform_invocations(was_in_the_readme), (
        "the scan does not flag the apply recipe it was written to remove"
    )
    assert _unrefused_terraform_invocations(
        "    terraform -chdir=infra/terraform apply\n"
    ), "the scan misses a global flag between the command and its sub-command"
    assert not _unrefused_terraform_invocations(
        "This path is deprecated: `terraform apply` fails at plan time.\n"
        "# variable validation would break `terraform destroy` for whoever applied one.\n"
    ), "the scan cannot tell a refusal from an invitation, so it will force the refusal out"


def test_the_terraform_readme_opens_with_the_deprecation() -> None:
    """First screen, not a footnote. The previous README's first two lines sold
    the path ("staging-first so you can smoke-test the real service"), and
    everything cautionary came far enough down to be scrolled past."""
    opening = "\n".join(_read(TF_README).splitlines()[:12]).lower()
    for expected in ("deprecated", "cloudbuild.yaml"):
        assert expected in opening, (
            f"the first screen of infra/terraform/README.md does not mention "
            f"{expected!r}. Someone who reads only the top of this file must not "
            f"come away thinking Terraform deploys this service."
        )
