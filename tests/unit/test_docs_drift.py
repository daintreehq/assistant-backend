"""Documentation-truth gates.

A tester's trust decision is built out of prose, not source. The specific failure
these tests exist to prevent is a *stale* statement surviving a code change and
becoming the thing a reader believes: a privacy promise the upstream does not
make, a thinking posture that was turned off two commits ago, a claim that the
CLI cannot retry when it demonstrably does, a credential model that inverted.

So every fact below is asserted against the code that implements it, not against
a hardcoded expectation. Change the code and the doc must move with it, in the
same commit — that is the whole point.

Scope note: these guard the *documentation-sensitive* facts named in
docs/PROVIDER_POLICY.md and docs/INTERNAL_BETA.md. They are deliberately not a
spell-checker for the whole corpus.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import get_args

import pytest

from daintree_assistant_server import PROTOCOL_MAX, PROTOCOL_MIN
from daintree_assistant_server import main as main_module
from daintree_assistant_server.auth import authenticate
from daintree_assistant_server.config import (
    CEREBRAS_MODEL,
    INTERACTIVE_REASONING_EFFORT,
    INTERACTIVE_THINKING_ENABLED,
    PUBLIC_MODEL_ID,
    Settings,
)
from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.providers.cerebras import CerebrasProvider
from daintree_assistant_server.services.task_runner import TaskRunner

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "daintree_assistant_server"

README = REPO_ROOT / "README.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
DOC_API = REPO_ROOT / "docs" / "DAINTREE_API.md"
DOC_PROVIDER = REPO_ROOT / "docs" / "PROVIDER_POLICY.md"
DOC_BETA = REPO_ROOT / "docs" / "INTERNAL_BETA.md"
DOC_KEYS = REPO_ROOT / "docs" / "KEY_HANDLING.md"

# Every prose surface a tester or a maintainer reads. The vendored Go CLI is
# excluded: it is a separate repository on its own release cycle, pinned here as a
# submodule, so this suite can only report drift in it, never fix it.
PROSE_FILES = (README, CLAUDE_MD, ENV_EXAMPLE, DOC_API, DOC_PROVIDER, DOC_BETA, DOC_KEYS)


def _read(path: Path) -> str:
    assert path.exists(), f"{path.relative_to(REPO_ROOT)} is missing"
    return path.read_text(encoding="utf-8")


def _python_sources() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


# ---------------------------------------------------------------------------
# The authoritative documents exist at all.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc", [DOC_BETA, DOC_PROVIDER, DOC_KEYS, DOC_API])
def test_authoritative_docs_exist(doc: Path) -> None:
    assert doc.exists(), (
        f"{doc.relative_to(REPO_ROOT)} is the authority for a class of facts these "
        "tests pin. Deleting it silently drops the gate."
    )


# ---------------------------------------------------------------------------
# Privacy: say "does not collect or train on", never "does not store".
#
# THE MECHANISM CHANGED AND THE COPY DELIBERATELY DID NOT. Under OpenRouter the
# no-training floor was something this server ENFORCED per request, by sending
# `data_collection: "deny"` and filtering the eligible endpoint pool. Cerebras is
# a single host: there is no pool, no filter, and nothing to send. What Daintree
# can say now rests entirely on Cerebras's own published policy, cited in
# docs/PROVIDER_POLICY.md section 4.
#
# Cerebras publishes a STRONGER position than the old copy claims — no training,
# and zero retention of inference inputs and outputs by default. The copy is
# still not upgraded to match, and that is a decision rather than an oversight:
# understating a privacy guarantee cannot mislead a user, and overstating one is
# the failure this whole section exists to prevent. Raising the claim to "does
# not retain" is a deliberate act that needs a human to verify Cerebras's terms
# and then relax the scan below — not a default that drifts in.
# ---------------------------------------------------------------------------

# Phrases that assert something stronger than the copy is willing to promise.
#
# Scoped to PROMPT/REQUEST data on purpose. "The server never persists a key" is a
# true and important statement about credentials, and must not trip this scan.
_OVERSTATED_PRIVACY = (
    re.compile(r"neither stores?\s+nor\s+trains?", re.IGNORECASE),
    re.compile(r"\bstores? or trains?\b", re.IGNORECASE),
    # "does not store your prompts", "never retains request data", "won't keep
    # your conversations", …
    re.compile(
        r"\b(?:does not|doesn't|do not|don't|won't|will not|never)\s+"
        r"(?:store|retain|keep)s?\b[^.\n]{0,40}"
        r"\b(?:prompt|request|conversation|input|content|data)",
        re.IGNORECASE,
    ),
    # "prompts are not retained", "request data is never stored", …
    re.compile(
        r"\b(?:prompt|request|conversation|input)s?\b[^.\n]{0,30}"
        r"\b(?:are|is|aren't|isn't)\s+(?:not\s+|never\s+)"
        r"(?:stored|retained|kept|persisted)",
        re.IGNORECASE,
    ),
    re.compile(r"\bzero[- ]retention\b", re.IGNORECASE),
    re.compile(r"\bzero data retention\b", re.IGNORECASE),
    re.compile(r"\bno retention\b", re.IGNORECASE),
    re.compile(r"\bnot retained\b", re.IGNORECASE),
    re.compile(r"\bZDR\b"),
)

# The ONLY way a line may carry one of the phrases above: an explicit inline
# opt-out. Deliberately not a heuristic — an earlier version of this test skipped
# any line containing "not"/"never", which let the exact phrase it was written to
# catch ("does not store your prompts") through untouched. A marker forces a human
# to decide, and greps cleanly.
#
# Legitimate uses are all *meta*: naming the flag we do not send, or tabulating
# the phrasings that are banned. Both belong in docs/PROVIDER_POLICY.md section 4.
_OPT_OUT = "privacy-copy-ok"


def test_nothing_sends_a_per_request_privacy_preference() -> None:
    """The code half, inverted.

    There is no longer a privacy knob on the wire: Cerebras exposes no
    per-request data-handling parameter, and the adapter must not invent one. If
    a future edit starts sending something in this space, the copy above stops
    describing the arrangement and this is where that shows up.
    """
    source = inspect.getsource(CerebrasProvider._build_body)
    for token in ("data_collection", "zdr", "privacy"):
        assert token not in source, (
            f"_build_body now sends {token!r}. The privacy posture is documented "
            "in docs/PROVIDER_POLICY.md section 4 as resting on Cerebras's own "
            "policy, with nothing sent per request; update it in this commit."
        )


@pytest.mark.parametrize("path", PROSE_FILES, ids=lambda p: p.name)
def test_no_document_overstates_the_privacy_guarantee(path: Path) -> None:
    lines = _read(path).splitlines()
    for number, line in enumerate(lines, start=1):
        if _OPT_OUT in line:
            continue
        for pattern in _OVERSTATED_PRIVACY:
            match = pattern.search(line)
            if match is None:
                continue
            pytest.fail(
                f"{path.name}:{number} claims {match.group(0)!r}.\n"
                "Daintree's user-facing promise is 'does not collect or train on'. "
                "It deliberately stops short of a retention claim — see the note at "
                "the top of this section: understating is safe, overstating is the "
                "failure this scan exists to prevent.\n"
                f"If this line is deliberately discussing the banned phrasing, "
                f"append '{_OPT_OUT}' to it.\n"
                "See docs/PROVIDER_POLICY.md section 4."
            )


def test_the_privacy_scan_catches_its_own_motivating_phrase() -> None:
    """A test for the test.

    An earlier revision skipped any line containing "not", which silently exempted
    "does not store your prompts" — the single phrasing the scan exists to catch.
    This pins the fix so the escape hatch cannot come back.
    """
    banned = (
        "Daintree does not store your prompts.",
        "Your prompts are never retained by the provider.",
        "endpoints that neither store nor train on request data",
        "requests are not stored anywhere",
        "we don't keep your conversation data",
        "zero data retention is enforced",
    )
    for sentence in banned:
        assert any(p.search(sentence) for p in _OVERSTATED_PRIVACY), (
            f"the overstated-privacy scan does not catch {sentence!r}"
        )

    allowed = (
        "Requests route only to endpoints that do not collect or train on request data.",
        "The server never persists a key.",
        "Tester content is never logged by the backend.",
    )
    for sentence in allowed:
        offenders = [p.pattern for p in _OVERSTATED_PRIVACY if p.search(sentence)]
        assert not offenders, f"{sentence!r} is accurate copy but trips {offenders}"


def test_provider_policy_names_the_upstream_whose_policy_this_now_is() -> None:
    """The promise has a source, and the doc must name it.

    This is the substantive difference from the old arrangement, and the one a
    reader most needs. The guarantee is no longer something this server enforces
    per request — it is Cerebras's published policy, and a reader who wants to
    check it needs to know whose policy to read.
    """
    text = _read(DOC_PROVIDER)
    assert "cerebras" in text.lower()
    assert "trust.cerebras.ai" in text or "cerebras.ai/privacy" in text, (
        "docs/PROVIDER_POLICY.md section 4 must link Cerebras's own authoritative "
        "data-handling page. The privacy claim now rests on it, so a reader has to "
        "be able to reach it."
    )


def test_provider_policy_states_the_privacy_posture_explicitly() -> None:
    text = _read(DOC_PROVIDER)
    assert "no-training guarantee" in text
    assert "not a no-retention" in text.lower() or "not a no-retention guarantee" in text.lower()


# ---------------------------------------------------------------------------
# Thinking posture.
# ---------------------------------------------------------------------------


def test_the_non_thinking_posture_is_documented() -> None:
    assert INTERACTIVE_THINKING_ENABLED is False, (
        "Interactive thinking was re-enabled. docs/PROVIDER_POLICY.md section 2, "
        ".env.example and CLAUDE.md all state that nothing in this server thinks; "
        "update them in this commit."
    )
    text = _read(DOC_PROVIDER).lower()
    assert "nothing in this server thinks" in text


@pytest.mark.parametrize("path", PROSE_FILES, ids=lambda p: p.name)
def test_no_document_claims_the_interactive_surface_thinks(path: Path) -> None:
    """The exact drift the review caught: `.env.example` said main/utility/gate
    run thinking-high long after the interactive surface was turned off.

    Matched on the shape of the stale sentence rather than the word "thinking",
    which the accurate copy also uses.
    """
    stale = re.compile(
        r"\b(?:main|utility|selector)\b[^.\n]{0,70}"
        r"\b(?:run|runs|use|uses|with)\b[^.\n]{0,20}"
        r"thinking(?:-high)?\b",
        re.IGNORECASE,
    )
    for number, line in enumerate(_read(path).splitlines(), start=1):
        match = stale.search(line)
        if match is None:
            continue
        # A negated or conditional mention is how the accurate copy talks about it
        # ("utility tasks are NON-thinking", "only if that flag is switched back on").
        if re.search(r"\b(?:non-thinking|not|never|no longer|if|off|unless)\b", line, re.I):
            continue
        pytest.fail(
            f"{path.name}:{number} says {match.group(0)!r}. The main completion, "
            "utility tasks and the selector all send reasoning disabled "
            "(INTERACTIVE_THINKING_ENABLED is False; the selector hardcodes it "
            "separately). See docs/PROVIDER_POLICY.md section 2."
        )


def test_nothing_is_documented_as_thinking() -> None:
    """There is no thinking surface left at all, and the doc has to say so.

    Skill learning was the one exception — it ran after the response, so its
    latency never reached a user — and it has been removed. A doc that still
    carves out an exception sends a reader looking for a thinking budget that
    nothing spends.
    """
    text = _read(DOC_PROVIDER).lower()
    assert "nothing in this server thinks" in text
    assert "skill learning is the one surface that still thinks" not in text


# The stale posture survived in *source comments* too — `config.py` justified the
# utility timeout by "every profile runs thinking-high" long after the flag went
# false. Prose files are not the only place a reader forms a belief.
_STALE_THINKING_COMMENT = re.compile(
    r"\b(?:every|each|all)\s+(?:utility\s+)?(?:profile|task)s?\b[^.\n]{0,40}"
    r"\b(?:run|runs|use|uses)\b[^.\n]{0,15}thinking",
    re.IGNORECASE,
)


def test_no_source_comment_claims_utility_tasks_think() -> None:
    offenders: list[str] = []
    for path in _python_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("#") and '"""' not in stripped:
                continue
            if _STALE_THINKING_COMMENT.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert not offenders, (
        f"{offenders} still describe utility tasks as thinking. They read "
        "INTERACTIVE_THINKING_ENABLED, which is False."
    )


def test_task_runner_actually_sends_reasoning_disabled() -> None:
    """The behavioural anchor under all the prose above.

    Whatever the comments say, this is what goes on the wire.
    """
    source = inspect.getsource(TaskRunner)
    assert "INTERACTIVE_THINKING_ENABLED" in source, (
        "TaskRunner no longer reads the shared thinking posture; "
        "docs/PROVIDER_POLICY.md section 2 says it does"
    )


# ---------------------------------------------------------------------------
# Model identity.
# ---------------------------------------------------------------------------


def test_provider_policy_names_the_pinned_models() -> None:
    text = _read(DOC_PROVIDER)
    for model in (CEREBRAS_MODEL, PUBLIC_MODEL_ID):
        assert model in text, (
            f"docs/PROVIDER_POLICY.md does not name {model!r}. The model table is "
            "the documented source of truth for what actually answers a call."
        )


def test_readme_names_the_pinned_main_model() -> None:
    assert CEREBRAS_MODEL in _read(README)


# ---------------------------------------------------------------------------
# Credential ownership: the SERVER holds the upstream key.
#
# This inverted with the move to Cerebras, and it inverted in the direction that
# costs money if the docs are wrong. The old arrangement — every caller's bearer
# token IS the upstream credential — is now false, and a document that still says
# it tells a tester their key funds their turns when Daintree's does.
# ---------------------------------------------------------------------------

_CALLER_FUNDS_THE_TURN = re.compile(
    r"(?:caller|user|your)(?:'s)?\s+(?:own\s+)?(?:bearer\s+token|key|credential)"
    r"[^.\n]{0,80}\b(?:funds?|pays?\s+for|bills?)\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize("path", PROSE_FILES, ids=lambda p: p.name)
def test_no_document_claims_the_caller_funds_the_turn(path: Path) -> None:
    for number, line in enumerate(_read(path).splitlines(), start=1):
        match = _CALLER_FUNDS_THE_TURN.search(line)
        if match is None:
            continue
        # The accurate copy discusses the OLD arrangement in the past tense, or
        # denies it outright.
        if re.search(r"\b(?:no longer|used to|previously|before|not|never|was)\b", line, re.I):
            continue
        pytest.fail(
            f"{path.name}:{number} says {match.group(0)!r}. The server holds the "
            "upstream credential (CEREBRAS_API_KEY) and funds every turn from it; "
            "callers supply no key. See docs/KEY_HANDLING.md."
        )


def test_serving_provider_is_built_with_the_server_credential() -> None:
    """The invariant the copy above depends on, pinned at its source.

    Source inspection rather than a behavioural test because the failure mode is
    textual: someone reinstates ``api_key=None`` and every turn fails in
    ``_resolve_key`` with a message about a propagation bug that no longer
    describes anything. ``inspect.getsource`` on the real function object survives
    reordering and renaming of everything around it.
    """
    builder = inspect.getsource(main_module._build_provider)
    assert "settings.cerebras_api_key" in builder, (
        "main._build_provider no longer passes the server credential. Readiness "
        "gates on CEREBRAS_API_KEY precisely so this cannot be missing at runtime; "
        "a provider built without it cannot answer anything."
    )


def test_the_serving_provider_really_holds_the_credential() -> None:
    """The behaviour behind the doc claim, not just the source text.

    A request that arrives with no caller key must still be servable — that is the
    entire current credential model, and the thing an anonymous CLI depends on.
    """
    settings = Settings(
        STATE_SIGNING_KEY_CURRENT="x" * 32,
        CEREBRAS_API_KEY="csk-server-owned",
    )
    provider = main_module._build_provider(settings)
    assert isinstance(provider, CerebrasProvider)
    # No per-call key: the server's own is what gets resolved.
    assert provider._resolve_key(None).get_secret_value() == "csk-server-owned"


def test_a_caller_supplied_key_still_overrides_the_server_credential() -> None:
    """The seam account authentication will land in.

    A per-request credential must keep winning over the server's, even though
    nothing on the serving path sets one today. Keeping this live is what makes
    account-mapped credentials a value change rather than a plumbing change.
    """
    from pydantic import SecretStr as _SecretStr

    settings = Settings(
        STATE_SIGNING_KEY_CURRENT="x" * 32,
        CEREBRAS_API_KEY="csk-server-owned",
    )
    provider = main_module._build_provider(settings)
    resolved = provider._resolve_key(_SecretStr("csk-caller-supplied"))
    assert resolved.get_secret_value() == "csk-caller-supplied"


# THE DOOR IS OPEN, and the docs must say so plainly.
#
# This is the inverse of the gate that used to live here. Authentication still
# does not vary by environment — ``authenticate`` takes no environment argument —
# but the unconditional outcome flipped from "reject" to "accept": the server
# holds the upstream credential, so there is no per-request credential for a
# bearer to supply. A document that still tells a tester their request will be
# rejected without a key sends them hunting for a credential that does not exist.
_KEY_IS_REQUIRED = re.compile(
    r"(?:request|call|every request)[^.\n]{0,60}"
    r"\b(?:without|lacking|missing)\b[^.\n]{0,30}"
    r"\bkey\b[^.\n]{0,40}\b(?:rejected|refused|401|denied)\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize("path", PROSE_FILES, ids=lambda p: p.name)
def test_no_document_claims_a_caller_key_is_required(path: Path) -> None:
    for number, line in enumerate(_read(path).splitlines(), start=1):
        match = _KEY_IS_REQUIRED.search(line)
        if match is None:
            continue
        # Accurate copy discusses the old rule in the past tense.
        if re.search(r"\b(?:used to|previously|no longer|was|were)\b", line, re.I):
            continue
        pytest.fail(
            f"{path.name}:{number} says {match.group(0)!r}. Every request is "
            "served: auth.authenticate ignores the Authorization header entirely "
            "and the server funds the turn. See auth.py."
        )


def test_the_authorization_header_is_ignored_not_merely_optional() -> None:
    """The distinction that protects a CLI mid-migration.

    "Optional" would mean reading a bearer when one is sent — and a client still
    holding its old OpenRouter key would have that key threaded to the provider
    and forwarded to Cerebras, which rejects a credential it never issued. The
    caller would see ``provider_invalid_api_key``: their key blamed for a header
    they were right to still be sending.

    Asserted at the door AND at the source, because the source is where the
    temptation to "just parse it while we're here" would land.
    """
    for header in (None, "Bearer sk-or-v1-stale", "Bearer bad key", "Basic x"):
        principal = authenticate(header)
        assert principal.id == "anonymous"
        assert not hasattr(principal, "api_key")


def test_no_route_hands_a_caller_supplied_key_to_the_provider() -> None:
    """The other half: even if the door changed, the routes must not thread one.

    Textual, because that is how the regression would arrive — someone
    reinstating ``api_key=principal.api_key`` on one handler while the other four
    stay correct, which no single behavioural test would catch.
    """
    api_dir = SRC_ROOT / "api"
    offenders = [
        f"{path.relative_to(REPO_ROOT)}"
        for path in sorted(api_dir.glob("*.py"))
        if "principal.api_key" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"{offenders} thread a caller-supplied credential to the provider. The "
        "server funds every turn from its own key; a caller's would be forwarded "
        "to Cerebras and rejected as theirs. See auth.py and docs/KEY_HANDLING.md."
    )


# ---------------------------------------------------------------------------
# Retry layers: two hops, both legitimate.
# ---------------------------------------------------------------------------

_CLI_MUST_NOT_RETRY = re.compile(
    r"CLI (?:must never|must not|should never|cannot) stack", re.IGNORECASE
)


def test_no_source_comment_forbids_cli_retries() -> None:
    """The CLI has a deliberate 10-attempt / 2-minute retry layer on the
    CLI->backend hop. A comment forbidding it sends the next reader to delete
    working restart-recovery."""
    offenders = [
        f"{p.relative_to(REPO_ROOT)}"
        for p in _python_sources()
        if _CLI_MUST_NOT_RETRY.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        f"{offenders} still forbid CLI-side retries. They are different hops with "
        "different failure modes — see docs/PROVIDER_POLICY.md section 5."
    )


@pytest.mark.parametrize("path", PROSE_FILES, ids=lambda p: p.name)
def test_no_document_forbids_cli_retries(path: Path) -> None:
    assert _CLI_MUST_NOT_RETRY.search(_read(path)) is None


def test_provider_policy_documents_both_retry_hops() -> None:
    text = _read(DOC_PROVIDER)
    assert "cli/internal/backend/retry.go" in text, (
        "the doc must name the CLI-side retry implementation, so the next reader "
        "can check it rather than assume it does not exist"
    )
    # The DEFAULT, read off the field — not `Settings().max_upstream_retries`,
    # which pydantic-settings would happily source from the developer's own
    # environment and make this assertion machine-dependent.
    default = Settings.model_fields["max_upstream_retries"].default
    # Tolerant of formatting (`= 1`, `is 1`, `, default 1`) but not of the number,
    # which is the fact that can drift.
    assert re.search(rf"MAX_UPSTREAM_RETRIES\D{{0,20}}{default}\b", text), (
        f"docs/PROVIDER_POLICY.md does not state the current default MAX_UPSTREAM_RETRIES={default}"
    )


def test_provider_policy_excludes_key_verification_from_the_retry_scope() -> None:
    """`verify_key` deliberately bypasses `retry_upstream`; the doc used to say
    the retry policy covered "every upstream call", which swept it in."""
    verify = inspect.getsource(CerebrasProvider.verify_key)
    assert "retry_upstream" not in verify, (
        "verify_key now retries. docs/PROVIDER_POLICY.md section 5 states it does "
        "not; update the doc."
    )
    assert "verify" in _read(DOC_PROVIDER).lower()


# ---------------------------------------------------------------------------
# Wire shape: the parameters a single-host provider must and must not send.
#
# What used to live here was an elaborate bidirectional gate over the endpoint-
# selection policy — the doc's JSON block parsed and compared key-for-key against
# `_PROVIDER_ROUTING`. None of it has a subject any more. What replaces it is
# smaller and sharper: three OpenRouter parameters that are now hard 400s, and
# the one spelling that must be right on every single request.
# ---------------------------------------------------------------------------

def doc_section(heading: str, *, ends_at: str, text: str | None = None) -> str:
    """The slice of PROVIDER_POLICY.md under `heading`, or fail loudly.

    Every gate below reads a section by splitting on its heading, and a bare
    ``split(...)[-1]`` is the wrong tool for that: a heading that gets renamed or
    deleted does not fail, it silently yields the WHOLE document, and a gate
    reading the whole document finds whatever it was looking for somewhere else
    and passes. A heading that appears twice is the same failure with the opposite
    cause. Both are asserted here so a doc gate cannot quietly stop gating.
    """
    body = _read(DOC_PROVIDER) if text is None else text
    occurrences = body.count(heading)
    assert occurrences == 1, (
        f"docs/PROVIDER_POLICY.md has {occurrences} occurrences of the heading "
        f"{heading!r}, expected exactly 1. The doc gates locate sections by heading; "
        "a renamed, deleted or duplicated one makes them read the wrong text and "
        "pass for the wrong reason."
    )
    return body.split(heading)[1].split(ends_at)[0]


# Sent on every request by the previous adapter; rejected outright by Cerebras
# ("property is unsupported"). Reintroducing any of them breaks every call, so
# they are checked at the source rather than discovered in production.
_REJECTED_UPSTREAM_FIELDS = ("provider", "session_id", '"reasoning"')


@pytest.mark.parametrize("field", _REJECTED_UPSTREAM_FIELDS)
def test_the_adapter_sends_no_openrouter_only_parameter(field: str) -> None:
    source = inspect.getsource(CerebrasProvider._build_body)
    assert f'body[{field}]' not in source and f"body['{field}']" not in source, (
        f"_build_body emits {field}, which Cerebras rejects with a 400. See "
        "docs/PROVIDER_POLICY.md section 3."
    )


def test_the_adapter_sends_max_completion_tokens_and_never_max_tokens() -> None:
    """One spelling, and the reason it has to be exactly one.

    Cerebras accepts either name ALONE but 400s when both arrive. The normalized
    request field is called ``max_tokens``, so the temptation to pass it straight
    through is real and permanent — and the failure is total, on every request
    that sets a budget, which is all of them.
    """
    source = inspect.getsource(CerebrasProvider._build_body)
    assert 'body["max_completion_tokens"]' in source, (
        "_build_body no longer sends max_completion_tokens"
    )
    assert 'body["max_tokens"]' not in source, (
        "_build_body sends max_tokens as well. Cerebras 400s when both spellings "
        "are present."
    )


def test_cache_affinity_rides_the_openai_standard_field() -> None:
    """Section 3a's claim, asserted where it can actually break.

    The spelling is the whole risk: `session_id` (OpenRouter's name for the same
    idea) is a hard 400 here, and this field is attached to every main turn and
    every utility task. Get it wrong and nothing works — which no mocked test
    would notice, because the rejection lives at Cerebras.
    """
    source = inspect.getsource(CerebrasProvider._build_body)
    assert 'body["prompt_cache_key"]' in source, (
        "_build_body no longer emits prompt_cache_key. "
        "docs/PROVIDER_POLICY.md section 3a says main calls carry one."
    )
    section = doc_section("### 3a.", ends_at="\n## ")
    assert "prompt_cache_key" in section, (
        "docs/PROVIDER_POLICY.md section 3a no longer documents the affinity key."
    )


def test_provider_policy_describes_a_single_host_upstream() -> None:
    """The structural fact the whole document now turns on.

    A reader who believes there is still a pool of interchangeable endpoints will
    go looking for routing controls that do not exist, and will misread every
    availability failure as a policy dead-end.
    """
    section = doc_section("## 3. ", ends_at="\n### ")
    assert re.search(r"single[- ]host|one endpoint|not a router", section, re.I), (
        "docs/PROVIDER_POLICY.md section 3 must state that Cerebras hosts one "
        "endpoint per model. Every removed control follows from that."
    )


def test_no_reasoning_effort_is_sent_by_anything() -> None:
    """The tier constant survives the removal; nothing sends it.

    ``INTERACTIVE_REASONING_EFFORT`` is the value the interactive surface WOULD
    use if ``INTERACTIVE_THINKING_ENABLED`` were flipped back on, kept beside the
    flag so that decision does not also require choosing a tier. With the flag
    off and skill learning gone, no call in the system sets ``reasoning_effort``
    at all — which is what the doc's thinking table now says.
    """
    assert INTERACTIVE_THINKING_ENABLED is False
    assert INTERACTIVE_REASONING_EFFORT  # still defined, still unsent
    text = _read(DOC_PROVIDER).lower()
    assert "no reasoning mode" in text or "does not reason" in text


# ---------------------------------------------------------------------------
# Error taxonomy.
#
# The CLI branches on `error.code`, so these strings are a wire contract. A code
# that exists in the source but appears in no document is one the client author
# never learns to handle.
# ---------------------------------------------------------------------------

# Every code an error factory can produce. Kept explicit rather than derived, so
# ADDING a factory forces a decision about documenting it.
_ERROR_CODES = (
    "invalid_api_key",
    "provider_invalid_api_key",
    "provider_insufficient_credits",
    "provider_key_forbidden",
    # Listed explicitly because the introspection test below cannot reach it:
    # it calls each factory with no arguments, and this one names the provider
    # it is reporting on, so it is skipped there and would otherwise land
    # undocumented through a gate that exists to prevent exactly that.
    "provider_not_configured",
    "upstream_rate_limited",
    "upstream_timeout",
    "upstream_unavailable",
    "upstream_request_rejected",
    "upstream_protocol_error",
    "upstream_runaway_output",
    "upstream_error",
    "internal_error",
    "request_too_large",
    "unsupported_daintree_protocol",
    "system_messages_not_allowed",
    "reserved_tool_name",
    "unknown_task",
)


@pytest.mark.parametrize("code", _ERROR_CODES)
def test_every_error_code_is_documented(code: str) -> None:
    assert code in _read(DOC_API), (
        f"the service can emit {code!r} but docs/DAINTREE_API.md never mentions it. "
        "The CLI branches on error.code; an undocumented code is one no client "
        "author learns to handle."
    )


def test_no_error_factory_produces_an_undocumented_code() -> None:
    """The other direction: catch a factory added without a doc entry.

    Introspects the module rather than trusting the list above, so the two cannot
    drift apart silently.
    """
    import inspect as _inspect

    from daintree_assistant_server import errors as errors_module

    produced: set[str] = set()
    for name, obj in vars(errors_module).items():
        if name.startswith("_") or not _inspect.isfunction(obj):
            continue
        if obj.__module__ != errors_module.__name__:
            continue
        try:
            result = obj()
        except (TypeError, Exception):  # noqa: B014 - needs an argument; not a factory
            continue
        if isinstance(result, DaintreeError) and result.code:
            produced.add(result.code)

    undocumented = sorted(produced - set(_ERROR_CODES))
    assert not undocumented, (
        f"{undocumented} are produced by errors.py but missing from _ERROR_CODES "
        "here — add them and document them in docs/DAINTREE_API.md."
    )


def test_account_failures_are_documented_as_the_callers_to_fix() -> None:
    """The distinction that motivated the split: an exhausted balance is not a
    rejected key, and the doc has to say so."""
    text = _read(DOC_API)
    assert "provider_insufficient_credits" in text
    assert "Add credits" in text
    # And the two 401s must be explained as different, not merely both listed.
    assert "provider_invalid_api_key" in text
    assert "Distinct from `invalid_api_key`" in text


# ---------------------------------------------------------------------------
# Protocol range and environment set.
# ---------------------------------------------------------------------------


def test_protocol_range_is_documented() -> None:
    """`str(PROTOCOL_MAX) in text` alone is near-worthless — "2" appears all over a
    500-line document. Require the header and its value together."""
    text = _read(DOC_API)
    assert re.search(rf"X-Daintree-Protocol\D{{0,40}}{PROTOCOL_MAX}\b", text), (
        f"docs/DAINTREE_API.md does not state the current protocol version "
        f"{PROTOCOL_MAX} alongside the X-Daintree-Protocol header"
    )
    if PROTOCOL_MIN != PROTOCOL_MAX:
        assert f"{PROTOCOL_MIN}..{PROTOCOL_MAX}" in text, (
            "the supported protocol range widened; docs/DAINTREE_API.md still "
            "documents a single version"
        )


def _documented_environments() -> set[str]:
    """The environments named in the section 7 table, as a set."""
    text = _read(DOC_BETA)
    table = text.split("## 7.", 1)[-1].split("\n## ", 1)[0]
    return set(re.findall(r"^\|\s*`([a-z]+)`", table, re.MULTILINE))


def test_internal_beta_documents_exactly_the_supported_environments() -> None:
    """The environment literal is a closed set precisely so hardening cannot fail
    open on a typo. Exact set equality, not one-way inclusion: a documented
    environment that no longer exists is drift too.
    """
    declared = set(get_args(Settings.model_fields["environment"].annotation))
    assert _documented_environments() == declared, (
        "docs/INTERNAL_BETA.md section 7 and Settings.environment disagree: "
        f"documented={sorted(_documented_environments())} declared={sorted(declared)}"
    )


def test_no_admission_control_exists_in_the_service() -> None:
    """The doc records absence-of-rate-limiting as a deliberate decision. Assert
    the absence itself, so ADDING a limiter fails until the reasoning is updated —
    the doc-only version of this test would have happily passed either way.
    """
    text = _read(DOC_BETA).lower()
    assert "no admission control" in text

    # Markers that unambiguously mean *we* are limiting an inbound caller.
    # Deliberately NOT the bare word "rate_limit": the service maps OpenRouter's
    # 429 onto `upstream_rate_limited`, which is us reporting somebody else's
    # limit — the opposite of this test's subject.
    limiter_markers = (
        "slowapi",
        "daintree_rate_limited",
        "KeyAdmission",
        "key_admission",
        "AdmissionService",
    )
    offenders: list[str] = []
    for path in _python_sources():
        source = path.read_text(encoding="utf-8")
        # Strip comments so prose *about* the absence does not trip the scan.
        code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
        offenders.extend(
            f"{path.relative_to(REPO_ROOT)}:{marker}"
            for marker in limiter_markers
            if marker in code
        )
    assert not offenders, (
        f"inbound admission control now exists in the service ({offenders}). "
        "docs/INTERNAL_BETA.md section 4 states the backend deliberately has none, "
        "because it is a pass-through for the caller's own key and funds no spend "
        "of its own. Update that reasoning in the same commit."
    )


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_prompt_dumping_fails_readiness_in_every_hardened_environment(
    environment: str,
) -> None:
    """The last local-only feature, and the reason that gate still exists.

    Skill learning used to be the other entry — and the more dangerous one, since
    it mutated the skill catalog from model output at runtime. It has been
    removed entirely, leaving prompt dumping: it writes FULL prompts to disk,
    which is a development forensics tool and a disclosure risk anywhere else.

    Both hardened environments, and the specific problem — not merely 'some
    problem was reported', which a weak-secret complaint would also satisfy.
    """
    hardened = Settings(
        STATE_SIGNING_KEY_CURRENT="x" * 32,
        STATE_SALT="not-the-default",
        ENVIRONMENT=environment,
        DAINTREE_DUMP_UPSTREAM_DIR="/tmp/dump",
    )
    assert hardened.is_hardened
    problems = hardened.local_only_problems()
    assert any("DAINTREE_DUMP_UPSTREAM_DIR" in p for p in problems), problems


@pytest.mark.parametrize("environment", ["development", "test"])
def test_prompt_dumping_is_permitted_in_local_environments(environment: str) -> None:
    """The other half of the section 7 table."""
    local = Settings(
        STATE_SIGNING_KEY_CURRENT="x" * 32,
        ENVIRONMENT=environment,
        DAINTREE_DUMP_UPSTREAM_DIR="/tmp/dump",
    )
    assert not local.is_hardened
    assert local.local_only_problems() == []


def test_no_document_still_describes_skill_learning() -> None:
    """It is gone, and a document that still offers it is worse than silent.

    A reader following `.env.example` or the README would set
    `SKILL_LEARNING_MODE`, see nothing happen (extra env vars are ignored), and
    have no way to tell whether the feature was broken or absent.
    """
    offenders: list[str] = []
    for path in PROSE_FILES:
        for number, line in enumerate(_read(path).splitlines(), start=1):
            if "SKILL_LEARNING" in line or "skill-learning" in line.lower():
                # A line explaining the REMOVAL is the accurate copy.
                if re.search(r"\b(?:removed|no longer|used to|gone|deleted)\b", line, re.I):
                    continue
                offenders.append(f"{path.name}:{number}")
    assert not offenders, (
        f"{offenders} still describe skill learning, which has been removed."
    )


# ---------------------------------------------------------------------------
# Cost reporting (PROVIDER_POLICY section 5c)
# ---------------------------------------------------------------------------


def test_every_documented_cost_field_exists_on_the_wire_model() -> None:
    """Section 5c's table names fields a CLI will be written against.

    A doc promising `cost.selector` that the model does not carry sends someone
    to build a display for a field that never arrives.
    """
    from daintree_assistant_server.contracts.daintree_api import (
        RespondResponse,
        StreamDone,
        TurnCost,
    )

    assert set(TurnCost.model_fields) == {"total", "main", "selector", "complete"}
    assert "cost" in RespondResponse.model_fields
    assert "cost" in StreamDone.model_fields


def test_the_absent_means_unknown_rule_is_real() -> None:
    """The doc's strongest claim: a missing cost block is never "free".

    Both halves have to hold — the field must be nullable in the model, AND the
    serializer must actually drop it, or the promise is prose only.
    """
    from daintree_assistant_server.contracts.daintree_api import RespondResponse

    assert RespondResponse.model_fields["cost"].default is None
    from daintree_assistant_server.contracts.daintree_api import RespondMessage

    body = RespondResponse(
        request_id="r",
        model="m",
        message=RespondMessage(content="hi"),
        catalog_revision="c",
        prompt_version="p",
    ).model_dump(exclude_none=True)
    assert "cost" not in body


def test_the_adapter_asks_for_no_cost_opt_in() -> None:
    """There is no cost figure to opt into, and asking for one is a 400.

    OpenRouter had a (deprecated, inert) `usage: {include: true}` opt-in, and the
    temptation to reach for it survives the provider swap because `cost` is still
    on `ProviderUsage`. Here it is worse than useless: Cerebras rejects a `usage`
    key outright, so a request that asked for cost would fail entirely rather
    than come back without it.
    """
    import inspect as _inspect

    from daintree_assistant_server.providers import cerebras as _cb

    source = _inspect.getsource(_cb.CerebrasProvider._build_body)
    assert '"usage"' not in source, (
        "_build_body sends a `usage` key. Cerebras rejects it with a 400 — and "
        "reports no cost either way. See docs/PROVIDER_POLICY.md section 5c."
    )
    assert "reports no cost" in DOC_PROVIDER.read_text(encoding="utf-8").lower()


def test_the_documented_total_is_a_sum_of_both_calls() -> None:
    """Section 5c says a turn is normally two billed calls and total covers both.

    Asserted against the arithmetic rather than the prose, so a change that
    quietly reports only the main call fails here.
    """
    from daintree_assistant_server.contracts.daintree_api import SkillsBlock
    from daintree_assistant_server.contracts.extensions import SelectorMeta, SelectorUsage
    from daintree_assistant_server.providers.base import ProviderUsage
    from daintree_assistant_server.services.chat_orchestrator import _RespondMeta
    from daintree_assistant_server.services.turn_accounting import turn_cost

    meta = _RespondMeta(
        request_id="r",
        state=None,
        catalog_revision="c",
        prompt_version="p",
        skills=SkillsBlock(
            selector=SelectorMeta(usage=SelectorUsage(cost=0.0004)),
        ),
        warnings=[],
    )
    cost = turn_cost(ProviderUsage(cost=0.0021), meta)
    assert cost is not None
    assert cost.total == pytest.approx(0.0025)
    assert cost.main == 0.0021
    assert cost.selector == 0.0004


# ---------------------------------------------------------------------------
# Key verification: the contract the CLI branches on.
#
# `KEY_HANDLING.md` claimed for several releases that `usable` was "not yet
# implemented" and told readers not to describe it as available — while the
# endpoint had been returning it, and the CLI had been branching on it. That is
# the most dangerous shape of stale doc: not vague, but confidently wrong about
# the field a client uses to decide whether a sign-in succeeded.
# ---------------------------------------------------------------------------


def _verify_response_fields() -> set[str]:
    """The keys `/auth/verify` can return, read out of the route's own source."""
    from daintree_assistant_server.api import daintree_auth

    source = inspect.getsource(daintree_auth)
    # Both the literal dict returned on rejection and the `out[...] = ` assignments.
    return set(re.findall(r'out\["(\w+)"\]', source)) | set(
        re.findall(r'"(\w+)":\s*(?:False|True|REASON_|_REASON_)', source)
    )


@pytest.mark.parametrize("field", ["valid", "usable", "reason"])
def test_key_handling_documents_every_verification_verdict_field(field: str) -> None:
    """Each verdict field the endpoint returns must appear in the doc that claims
    to be authoritative for the credential lifecycle."""
    assert field in _verify_response_fields(), (
        f"/auth/verify no longer returns {field!r}; update docs/KEY_HANDLING.md §4"
    )
    assert f"`{field}`" in _read(DOC_KEYS), (
        f"docs/KEY_HANDLING.md does not document the `{field}` field that "
        f"/auth/verify returns and the CLI branches on"
    )


def test_no_document_says_usable_is_unimplemented() -> None:
    """The specific stale sentence, kept as a named regression.

    It read: "Not yet implemented: a `usable` field distinct from `valid` ...
    until it lands, do not describe `usable` as available."
    """
    stale = re.compile(r"(not yet implemented|not implemented)[^.\n]{0,60}`?usable`?", re.I)
    for path in PROSE_FILES:
        assert not stale.search(_read(path)), (
            f"{path.relative_to(REPO_ROOT)} still says `usable` is unimplemented — "
            "/auth/verify returns it and the CLI branches on it"
        )


def _stable_reason_codes() -> dict[str, str]:
    """Every `REASON_*` the route defines, discovered rather than listed.

    Hardcoding the three current codes would mean a FOURTH one — the case where
    a client most needs telling — is added to the route and silently absent from
    the documentation, with this test still green.
    """
    from daintree_assistant_server.api import daintree_auth

    return {
        name: value
        for name, value in vars(daintree_auth).items()
        if name.startswith("REASON_") and isinstance(value, str)
    }


def test_the_route_still_defines_reason_codes() -> None:
    """Guards the discovery above: a rename to `_REASON_*` or a move to an enum
    would empty the mapping and make the parametrised test below vacuous."""
    codes = _stable_reason_codes()
    assert len(codes) >= 3, f"expected the REASON_* outcome codes, found {codes}"
    assert "ok" in codes.values()


@pytest.mark.parametrize("constant", sorted(_stable_reason_codes()))
def test_key_handling_lists_every_stable_reason_code(constant: str) -> None:
    """`reason` is the machine-readable outcome a client maps to user-facing copy.
    A code the route can return and the doc does not list is a code no client
    knows to handle."""
    reason = _stable_reason_codes()[constant]
    assert f"`{reason}`" in _read(DOC_KEYS), (
        f"docs/KEY_HANDLING.md does not list the `{reason}` outcome code "
        f"({constant}), which /auth/verify can return"
    )


# ---------------------------------------------------------------------------
# The credential path, checked against the CLI that writes it.
#
# Both authoritative documents said `~/.daintree/credentials.json` while the CLI
# had moved the sign-in to its per-user state root one level down. Telling a
# tester the wrong path to a file holding a spend-authorizing key is a support
# problem and a security one: "delete this to sign out" pointing at nothing.
# ---------------------------------------------------------------------------

CLI_ROOT = REPO_ROOT / "cli"


def _require_cli_submodule() -> None:
    """Skip when `cli/` is genuinely absent — and only then.

    The submodule is pinned and CI does not fetch it, so these checks are a LOCAL
    gate. That is a real limitation and docs/KEY_HANDLING.md says so rather than
    claiming CI protection it does not have. What must NOT happen is a skip that
    hides a parse failure: an unpopulated submodule and a Go-side rename would
    otherwise look identical, and the second one is the case the gate exists for.
    """
    if not (CLI_ROOT / "internal").is_dir():
        pytest.skip("cli/ submodule is not populated (git submodule update --init cli)")


# The CLI's key lifecycle was REMOVED, and these guard that removal rather than
# the flow it replaced. `feat: drop the API-key requirement` deleted the
# credentials package, the sign-in path and the login/logout commands outright,
# which is why docs/KEY_HANDLING.md §5 now says there is no client-side storage.
#
# The direction of the check is inverted on purpose. Nothing can assert that a
# deleted flow still behaves correctly, but the doc DOES make a falsifiable claim
# — that no key is stored — and the way that claim goes stale is the CLI growing
# the feature back while §5 still says it has none.
_REMOVED_CREDENTIAL_SOURCES = (
    Path("internal") / "credentials" / "credentials.go",
    Path("internal") / "cli" / "login.go",
    Path("internal") / "app" / "signin.go",
)


@pytest.mark.parametrize("relative", _REMOVED_CREDENTIAL_SOURCES, ids=lambda p: p.name)
def test_the_cli_still_stores_no_credential(relative: Path) -> None:
    _require_cli_submodule()
    assert not (CLI_ROOT / relative).exists(), (
        f"cli/{relative} is back, so the CLI may store a key again — but "
        f"docs/KEY_HANDLING.md §5 says it stores none. Rewrite §5 against whatever "
        f"the flow now is; a doc claiming 'no credential on disk' while one exists "
        f"is the worst direction for this particular claim to be wrong in."
    )


def test_an_unset_key_sends_no_authorization_header() -> None:
    """The one live claim §5 makes about the wire.

    §5 says an empty `DAINTREE_API_KEY` sends no header at all. That is what lets
    the server ignore the header without a mid-migration client being blamed for
    one it was right to still be sending.
    """
    _require_cli_submodule()
    source = (CLI_ROOT / "internal" / "backend" / "client.go").read_text(encoding="utf-8")
    guarded = re.search(
        r'if\s+c\.apiKey\s*!=\s*""\s*\{\s*req\.Header\.Set\(\s*"Authorization"',
        source,
    )
    assert guarded, (
        "internal/backend/client.go no longer guards the Authorization header on a "
        "non-empty key. docs/KEY_HANDLING.md §5 claims an unset DAINTREE_API_KEY "
        "sends no header."
    )


def test_the_key_is_registered_for_redaction_when_it_is_set() -> None:
    """§5's other live claim. Registration is unconditional in the CLI, which is
    what covers the rare install that exports the variable."""
    _require_cli_submodule()
    source = (CLI_ROOT / "internal" / "app" / "app.go").read_text(encoding="utf-8")
    assert "redact.RegisterSecret(cfg.APIKey)" in source, (
        "internal/app/app.go no longer registers cfg.APIKey for redaction, which "
        "docs/KEY_HANDLING.md §5 claims it does"
    )


@pytest.mark.parametrize("doc", [DOC_BETA, DOC_KEYS])
def test_no_document_tells_a_reader_the_cli_stores_a_key(doc: Path) -> None:
    """Widened from the pre-move-path check it replaces.

    That test guarded against naming the WRONG credentials file. The CLI now
    writes NO credentials file, so naming any of them is the error: a tester told
    to delete a path that cannot exist believes they have signed out, and a
    reviewer reading it believes there is a key on disk to protect.
    """
    text = _read(doc)
    stale = re.compile(r"credentials\.json")
    assert not stale.search(text), (
        f"{doc.relative_to(REPO_ROOT)} names a credentials.json path, but the CLI "
        f"stores no credential (docs/KEY_HANDLING.md §5)"
    )


# ---------------------------------------------------------------------------
# Benchmark prose vs the committed baseline.
#
# The benchmark READMEs carried hand-copied absolute numbers — "16 skills",
# "~20.3 KB selector prompt" — long enough that a reviewer quoted them back as
# current against a 31-skill catalog. Hand-copied metrics decay silently; this
# pins the two that a reader is most likely to act on.
# ---------------------------------------------------------------------------

BASELINE = REPO_ROOT / "benchmarks" / "baseline.json"
DOC_PROMPT_COST = REPO_ROOT / "benchmarks" / "prompt_cost" / "README.md"


_SNAPSHOT_HEADER = re.compile(r"Measured\s+\*{0,2}(\d{4}-\d{2}-\d{2})\*{0,2},?\s+"
                              r"[^\n]*?(\d+)[- ]skills?[^\n]*?(\d+)[- ]tools?", re.I)


def test_the_prompt_cost_readme_declares_exactly_one_current_snapshot() -> None:
    """Two "Measured …" headers above the history section means the file
    contradicts itself, which is how the stale 16-skill table survived beside a
    corrected one: a reader takes whichever they reach first."""
    current = _read(DOC_PROMPT_COST).split("### Snapshot history", 1)[0]
    headers = _SNAPSHOT_HEADER.findall(current)
    assert len(headers) == 1, (
        f"benchmarks/prompt_cost/README.md declares {len(headers)} current snapshots "
        f"({[h[0] for h in headers]}). Dated HISTORY belongs under '### Snapshot "
        f"history'; above it there must be exactly one."
    )


def test_the_prompt_cost_readme_snapshot_matches_the_live_catalog() -> None:
    """The snapshot must describe the catalog that exists NOW.

    Checked against the live catalog and tool fixture rather than
    benchmarks/baseline.json: the baseline declares MOVEMENT BANDS, so the real
    counts can drift inside them while the baseline and the prose stay stale
    together — which is two documents agreeing with each other and neither with
    the code.
    """
    from tools.baseline import capture

    live = capture()
    skills = live["catalog"]["skills"]
    tools = live["tools"]["count"]
    current = _read(DOC_PROMPT_COST).split("### Snapshot history", 1)[0]
    header = _SNAPSHOT_HEADER.search(current)
    assert header, (
        "benchmarks/prompt_cost/README.md has no dated 'Measured <date>, N skills / "
        "M tools' line — the snapshot cannot be checked for staleness without one"
    )
    assert (int(header.group(2)), int(header.group(3))) == (skills, tools), (
        f"the prompt_cost README snapshot says {header.group(2)} skills / "
        f"{header.group(3)} tools; the live catalog and tool fixture are "
        f"{skills} / {tools}. Re-run `python -m benchmarks.prompt_cost.bench run` "
        f"and refresh the table."
    )


def test_the_prompt_cost_readme_does_not_sell_a_retired_selector_prompt() -> None:
    """Selection is local now — zero upstream bytes. The README must not present
    a selector-prompt size as a CURRENT cost; the retired figure may appear only
    as explicitly-labelled history."""
    current = _read(DOC_PROMPT_COST).split("### Snapshot history", 1)[0]
    assert not re.search(r"\| selector prompt \|\s*~[\d.]+\s*KB", current), (
        "the prompt_cost README still quotes a live selector prompt size; "
        "selection is local and sends no prompt upstream"
    )
    assert "local two-stage selector" in current


def test_the_benchmark_readme_ci_claim_matches_the_workflow() -> None:
    """It said the suite "is not part of ... CI" while CI ran the deterministic
    dashboard as a step. Both halves are asserted — the prose AND the workflow —
    because pinning only the prose lets the CI step be deleted with the doc left
    claiming it runs, which is the same lie the other way round.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "benchmarks.dashboard.bench" in workflow, (
        "the deterministic dashboard is no longer a CI step, but benchmarks/README.md "
        "tells contributors it is"
    )
    text = _read(REPO_ROOT / "benchmarks" / "README.md")
    assert re.search(r"CI runs the\s+deterministic dashboard", text), (
        "benchmarks/README.md no longer states that CI runs the deterministic "
        "dashboard — check it has not reverted to claiming benchmarks are outside CI"
    )
