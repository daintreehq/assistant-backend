"""Documentation-truth gates.

A tester's trust decision is built out of prose, not source. The specific failure
these tests exist to prevent is a *stale* statement surviving a code change and
becoming the thing a reader believes: a privacy promise the routing policy does
not make, a thinking posture that was turned off two commits ago, a claim that
the CLI cannot retry when it demonstrably does.

So every fact below is asserted against the code that implements it, not against
a hardcoded expectation. Change the code and the doc must move with it, in the
same commit — that is the whole point.

Scope note: these guard the *documentation-sensitive* facts named in
docs/PROVIDER_POLICY.md and docs/INTERNAL_BETA.md. They are deliberately not a
spell-checker for the whole corpus.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path
from typing import get_args

import pytest

from daintree_assistant_server import PROTOCOL_MAX, PROTOCOL_MIN
from daintree_assistant_server import main as main_module
from daintree_assistant_server.auth import authenticate
from daintree_assistant_server.config import (
    INTERACTIVE_THINKING_ENABLED,
    OPENROUTER_MODEL,
    OPENROUTER_REASONING_EFFORT,
    PUBLIC_MODEL_ID,
    SKILL_BUILDER_MODEL,
    SKILL_IMPROVER_MODEL,
    SKILL_LEARNING_REASONING_EFFORT,
    Settings,
)
from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.providers.openrouter import (
    _PROVIDER_ROUTING,
    _PROVIDER_ROUTING_STRUCTURED,
    OpenRouterProvider,
)
from daintree_assistant_server.providers.policy import DEFAULT_POLICY, PrivacyMode
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
# Privacy: `data_collection: "deny"` is a no-training floor, NOT no-retention.
# ---------------------------------------------------------------------------

# Phrases that assert something stronger than the routing policy actually sends.
# `data_collection: "deny"` filters to endpoints that do not COLLECT or train on
# prompt data; an endpoint in that pool may still hold a request transiently. Zero
# data retention is OpenRouter's separate `zdr` flag, which we do not send.
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


def test_routing_policy_does_not_claim_zero_retention() -> None:
    """The code half: if `zdr` is ever sent, this test must be revisited."""
    assert "zdr" not in _PROVIDER_ROUTING, (
        "The routing policy now sends `zdr`. That is a stronger promise than the "
        "current copy makes: update docs/PROVIDER_POLICY.md section 4, the README, "
        "CLAUDE.md, .env.example and the CLI onboarding copy, then relax the "
        "overstated-phrase scan below."
    )
    assert _PROVIDER_ROUTING["data_collection"] == "deny"


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
                '`data_collection: "deny"` guarantees no collection or training. It '
                "does NOT guarantee no retention — that is OpenRouter's separate "
                "`zdr` flag, which this server does not send.\n"
                "Say 'does not collect or train on'. If this line is deliberately "
                f"discussing the banned phrasing, append '{_OPT_OUT}' to it.\n"
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


def test_the_shipped_privacy_mode_matches_the_documented_posture() -> None:
    """The enum and the prose are two halves of one promise."""
    assert DEFAULT_POLICY.privacy is PrivacyMode.NO_TRAINING, (
        "the shipped privacy mode changed. docs/PROVIDER_POLICY.md section 4 says "
        "'Current posture: NO_TRAINING' and every user-facing string is written to "
        "match; update them in this commit."
    )
    assert "NO_TRAINING" in _read(DOC_PROVIDER)


@pytest.mark.parametrize("mode", list(PrivacyMode))
def test_every_privacy_mode_has_accurate_copy(mode: PrivacyMode) -> None:
    """The description ships with the enum precisely so a client cannot invent its
    own wording.

    Note this is NOT the corpus-wide banned-phrase scan: a retention claim is
    accurate for ZDR (it is exactly what that mode buys) and inaccurate for
    NO_TRAINING. The blunt scan cannot tell those apart, so the assertion is
    per-mode.
    """
    text = mode.description
    assert "collect or train" in text, (
        f"PrivacyMode.{mode.name}.description omits the no-training guarantee, "
        "which every mode provides"
    )
    if mode is PrivacyMode.NO_TRAINING:
        assert "not enforced" in text, (
            "the default mode's copy must say explicitly that zero data retention "
            "is NOT enforced — silence reads as a promise"
        )
    else:
        assert "zero data retention" in text.lower()


def test_provider_policy_states_the_privacy_posture_explicitly() -> None:
    text = _read(DOC_PROVIDER)
    assert "no-training guarantee" in text
    assert "not a no-retention" in text.lower() or "not a no-retention guarantee" in text.lower()
    assert "zdr" in text.lower(), "the doc must name the flag it is NOT sending"


# ---------------------------------------------------------------------------
# Thinking posture.
# ---------------------------------------------------------------------------


def test_interactive_surface_is_documented_as_non_thinking() -> None:
    assert INTERACTIVE_THINKING_ENABLED is False, (
        "Interactive thinking was re-enabled. docs/PROVIDER_POLICY.md section 2, "
        ".env.example and CLAUDE.md all state the interactive surface is "
        "non-thinking; update them in this commit."
    )
    text = _read(DOC_PROVIDER)
    assert "interactive surface is non-thinking" in text.lower()


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


def test_skill_learning_is_documented_as_the_only_thinking_surface() -> None:
    text = _read(DOC_PROVIDER).lower()
    assert "skill learning is the one surface that still thinks" in text


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
    for model in (OPENROUTER_MODEL, SKILL_BUILDER_MODEL, SKILL_IMPROVER_MODEL, PUBLIC_MODEL_ID):
        assert model in text, (
            f"docs/PROVIDER_POLICY.md does not name {model!r}. The model table is "
            "the documented source of truth for what actually answers a call."
        )


def test_readme_names_the_pinned_main_model() -> None:
    assert OPENROUTER_MODEL in _read(README)


# ---------------------------------------------------------------------------
# Caller-key ownership: the server holds no upstream credential.
# ---------------------------------------------------------------------------

_SERVER_OWNS_CREDENTIALS = re.compile(
    r"server owns[^.\n]{0,120}\bupstream credentials\b", re.IGNORECASE
)


@pytest.mark.parametrize("path", PROSE_FILES, ids=lambda p: p.name)
def test_no_document_claims_the_server_owns_upstream_credentials(path: Path) -> None:
    text = _read(path)
    match = _SERVER_OWNS_CREDENTIALS.search(text)
    assert match is None, (
        f"{path.name} says {match.group(0)!r}. The serving path is built with "
        "api_key=None (main._build_provider); every request's bearer token IS the "
        "upstream credential. See docs/KEY_HANDLING.md."
    )


def test_serving_provider_is_built_without_a_default_credential() -> None:
    """The invariant the copy above depends on, pinned at its source.

    Source inspection rather than a behavioural test because the failure mode is
    textual: someone passes ``settings.openrouter_api_key`` into the serving
    provider and the server starts quietly billing a shared account instead of
    failing loudly in ``_resolve_key``. ``inspect.getsource`` on the real function
    object survives reordering and renaming of everything around it.
    """
    builder = inspect.getsource(main_module._build_provider)
    assert "api_key=None" in builder
    assert "openrouter_api_key" not in builder, (
        "main._build_provider now reads settings.openrouter_api_key. That turns a "
        "loud propagation failure into silently billing a shared account. See "
        "docs/KEY_HANDLING.md section 1."
    )


def test_the_serving_provider_really_holds_no_credential() -> None:
    """The behaviour behind the doc claim, not just the source text.

    A provider with no default credential must reject a call that arrives without
    one rather than substituting something of its own.
    """
    settings = Settings(
        STATE_SIGNING_KEY_CURRENT="x" * 32,
        OPENROUTER_API_KEY="sk-or-v1-should-never-be-used",
    )
    provider = main_module._build_provider(settings)
    assert isinstance(provider, OpenRouterProvider)
    assert provider._default_api_key is None
    with pytest.raises(DaintreeError) as excinfo:
        provider._resolve_key(None)
    assert excinfo.value.status_code >= 500


# A doc line describing a way to reach the service WITHOUT a caller key. Auth does
# not vary by environment (auth.authenticate is unconditional), so any such line is
# stale. Matched as a phrase rather than the bare word "unauthenticated", because
# the accurate copy uses that word too ("there is no unauthenticated mode").
_UNAUTH_MODE = re.compile(
    r"(?:unauthenticated|without a (?:caller )?key|no bearer)[^.\n]{0,60}"
    r"\b(?:mode|access|path|allowed|permitted|works|supported|development)\b",
    re.IGNORECASE,
)
_UNAUTH_DENIAL = re.compile(r"\b(?:no|never|not|rejected|removed|gone)\b", re.IGNORECASE)


@pytest.mark.parametrize("path", PROSE_FILES, ids=lambda p: p.name)
def test_no_document_describes_an_unauthenticated_mode(path: Path) -> None:
    for number, line in enumerate(_read(path).splitlines(), start=1):
        match = _UNAUTH_MODE.search(line)
        if match is None:
            continue
        # The accurate statement is a denial ("there is no unauthenticated mode",
        # "the development shortcut was removed"). Require one on the same line.
        assert _UNAUTH_DENIAL.search(line), (
            f"{path.name}:{number} appears to describe reaching the service without "
            f"a caller key: {match.group(0)!r}. Every environment requires one "
            "(auth.authenticate), and without a caller key there is no upstream "
            "credential to run the turn with."
        )


@pytest.mark.parametrize("header", [None, "", "Bearer", "Basic abc", "Bearer "])
def test_authentication_rejects_a_missing_key_unconditionally(header: str | None) -> None:
    """The code fact behind the copy above.

    ``authenticate`` takes no environment argument at all, which is the real
    guarantee — but a behavioural test says so in a way a future refactor cannot
    quietly undo.
    """
    with pytest.raises(DaintreeError) as excinfo:
        authenticate(header)
    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "invalid_api_key"


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
    verify = inspect.getsource(OpenRouterProvider.verify_key)
    assert "retry_upstream" not in verify, (
        "verify_key now retries. docs/PROVIDER_POLICY.md section 5 states it does "
        "not; update the doc."
    )
    assert "verify" in _read(DOC_PROVIDER).lower()


# ---------------------------------------------------------------------------
# Structured-call routing.
# ---------------------------------------------------------------------------


def _documented_routing_policy() -> dict[str, object]:
    """The ```json block in docs/PROVIDER_POLICY.md section 3, parsed.

    Comparing parsed dicts rather than grepping substrings is what makes this gate
    bidirectional: a changed *value* fails, and so does a key that was deleted
    from the code but left behind in the prose. A substring scan catches neither.
    """
    blocks = re.findall(r"```json\n(.*?)```", _read(DOC_PROVIDER), re.DOTALL)
    for block in blocks:
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:  # pragma: no cover - a prose example
            continue
        if isinstance(parsed, dict) and "data_collection" in parsed:
            return parsed
    pytest.fail(
        "docs/PROVIDER_POLICY.md section 3 no longer contains a parseable ```json "
        "block for the routing policy. That block is the documented contract; keep "
        "it machine-readable."
    )


def test_documented_routing_policy_matches_the_code_exactly() -> None:
    assert _documented_routing_policy() == _PROVIDER_ROUTING, (
        "the provider block in docs/PROVIDER_POLICY.md section 3 has drifted from "
        "_PROVIDER_ROUTING in providers/openrouter.py. That block is what a reader "
        "believes the server sends."
    )


def test_structured_routing_bound_is_documented() -> None:
    """The structured policy is the base policy plus one key; assert both halves."""
    extra = {
        key: value
        for key, value in _PROVIDER_ROUTING_STRUCTURED.items()
        if _PROVIDER_ROUTING.get(key) != value
    }
    assert set(extra) == {"preferred_max_latency"}, (
        "the structured-call policy now differs from the base policy by more than "
        f"preferred_max_latency ({sorted(extra)}); document the new keys in "
        "docs/PROVIDER_POLICY.md section 3"
    )
    text = _read(DOC_PROVIDER)
    assert "preferred_max_latency" in text
    for key, value in extra["preferred_max_latency"].items():
        # Tolerant of `2.0` vs `2` and of surrounding formatting; strict about the
        # number, which is the fact that drifts.
        pattern = rf'"{key}"\D{{0,4}}{re.escape(str(value).rstrip("0").rstrip("."))}'
        assert re.search(pattern, text), (
            f"docs/PROVIDER_POLICY.md does not document preferred_max_latency {key}={value}"
        )


def test_reasoning_effort_tiers_are_documented() -> None:
    """The doc's thinking table names an effort tier per surface; pin the strings."""
    text = _read(DOC_PROVIDER)
    for effort in {OPENROUTER_REASONING_EFFORT, SKILL_LEARNING_REASONING_EFFORT}:
        assert f"`{effort}`" in text or f"**{effort}**" in text, (
            f"docs/PROVIDER_POLICY.md section 2 does not name the {effort!r} effort "
            "tier that config.py actually sends"
        )


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
    "upstream_rate_limited",
    "upstream_timeout",
    "upstream_no_compliant_provider",
    "upstream_unavailable",
    "upstream_request_rejected",
    "upstream_protocol_error",
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
def test_skill_learning_fails_readiness_in_every_hardened_environment(
    environment: str,
) -> None:
    """Both hardened environments, and the specific problem — not merely 'some
    problem was reported', which a weak-secret complaint would also satisfy."""
    hardened = Settings(
        STATE_SIGNING_KEY_CURRENT="x" * 32,
        STATE_SALT="not-the-default",
        ENVIRONMENT=environment,
        SKILL_LEARNING_MODE="apply",
    )
    assert hardened.is_hardened
    problems = hardened.local_only_problems()
    assert any("SKILL_LEARNING_MODE" in p for p in problems), (
        f"skill learning no longer fails readiness in {environment}, but "
        f"docs/INTERNAL_BETA.md item 9 and section 7 both say it does: {problems}"
    )


@pytest.mark.parametrize("environment", ["development", "test"])
def test_skill_learning_is_permitted_in_local_environments(environment: str) -> None:
    """The other half of the section 7 table."""
    local = Settings(
        STATE_SIGNING_KEY_CURRENT="x" * 32,
        ENVIRONMENT=environment,
        SKILL_LEARNING_MODE="apply",
    )
    assert not local.is_hardened
    assert not any("SKILL_LEARNING_MODE" in p for p in local.local_only_problems())


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


def test_the_doc_does_not_claim_a_cost_opt_in_is_needed() -> None:
    """OpenRouter's `usage: {include: true}` is deprecated and inert.

    The request builder must not send it, and the doc must not tell a reader to.
    A stale instruction here costs nothing to follow and teaches the wrong model
    of where the number comes from.
    """
    import inspect as _inspect

    from daintree_assistant_server.providers import openrouter as _or

    source = _inspect.getsource(_or)
    assert '"include"' not in source, "the deprecated usage opt-in crept back in"
    assert "deprecated and has no effect" in DOC_PROVIDER.read_text(encoding="utf-8")


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


def _cli_credentials_path() -> str:
    """The credential path the vendored CLI actually resolves.

    Raises rather than returning None on a parse failure. Returning None and
    skipping would turn any Go-side change this regex does not understand — a
    switch to `path.Join`, a renamed constant, a computed filename — into a green
    build, and the whole point is to notice when the CLI moves the file.
    """
    config = (CLI_ROOT / "internal" / "config" / "config.go").read_text(encoding="utf-8")
    credentials = (
        CLI_ROOT / "internal" / "credentials" / "credentials.go"
    ).read_text(encoding="utf-8")
    root = re.search(r'stateRootSubpath\s*=\s*filepath\.Join\(([^)]*)\)', config)
    name = re.search(r'FileName\s*=\s*"([^"]+)"', credentials)
    assert root, (
        "cannot find `stateRootSubpath = filepath.Join(...)` in the vendored "
        "config.go — the CLI changed how it resolves the state root, so the "
        "credential path in docs/ can no longer be checked and may be wrong"
    )
    assert name, (
        "cannot find `FileName = \"...\"` in the vendored credentials.go — the "
        "CLI changed how it names the credential file"
    )
    parts = [p.strip().strip('"') for p in root.group(1).split(",")]
    return "~/" + "/".join(parts) + "/" + name.group(1)


@pytest.mark.parametrize("doc", [DOC_BETA, DOC_KEYS])
def test_the_documented_credential_path_is_the_one_the_cli_writes(doc: Path) -> None:
    _require_cli_submodule()
    path = _cli_credentials_path()
    assert path in _read(doc), (
        f"{doc.relative_to(REPO_ROOT)} does not name {path}, which is where the "
        f"CLI actually stores the sign-in. A tester told to delete the wrong file "
        f"believes they have signed out."
    )


@pytest.mark.parametrize("doc", [DOC_BETA, DOC_KEYS])
def test_no_document_names_the_old_credential_path(doc: Path) -> None:
    """The pre-move path, which is a real directory that will simply contain no
    credentials file — so the mistake reads as "already signed out"."""
    text = _read(doc)
    stale = re.compile(r"~/\.daintree/credentials\.json")
    assert not stale.search(text), (
        f"{doc.relative_to(REPO_ROOT)} names the pre-move credential path"
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


def test_the_prompt_cost_readme_quotes_the_current_selector_size() -> None:
    """The single number the original staleness got most wrong: it advertised a
    ~20.3 KB selector prompt against a real 44 KB one, and a reader sizing a
    change against that is out by a factor of two."""
    from tools.baseline import capture

    kb = capture()["prompt"]["selector_prompt_bytes"] / 1024
    current = _read(DOC_PROMPT_COST).split("### Snapshot history", 1)[0]
    quoted = re.search(r"selector prompt \|\s*~([\d.]+)\s*KB", current)
    assert quoted, "the prompt_cost README snapshot no longer quotes a selector prompt size"
    assert abs(float(quoted.group(1)) - kb) < 1.0, (
        f"the prompt_cost README says the selector prompt is ~{quoted.group(1)} KB; "
        f"it is {kb:.1f} KB"
    )


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


# ---------------------------------------------------------------------------
# The credential-lifecycle table, checked against the vendored CLI.
#
# That table is the reason this whole section exists: it read "the CLI accepts a
# pasted token and persists it immediately: it has no caller of
# /v1/daintree/auth/verify ... and no credential-clear path" while the CLI had
# been doing all three for some time. A status table nobody checks becomes an
# inventory of what USED to be missing.
#
# Each row is pinned to the symbol that implements it. These are necessarily
# coarse — the presence of a call is not proof of its ordering — so the one
# ordering that actually matters (verify BEFORE persist; a typo must never reach
# disk) is checked positionally.
# ---------------------------------------------------------------------------

# Each entry is `row number -> (file, symbol)`. The row number matters: the test
# reads the table row too, so a requirement whose CODE has landed while its STATUS
# still says ⏳ fails. Without that, the map proved only that a symbol exists
# somewhere in the CLI — which is true of a row nobody updated.
CLI_LIFECYCLE_EVIDENCE = {
    "states the key is an OpenRouter key that OpenRouter bills": (
        CLI_ROOT / "internal" / "cli" / "login.go", "KeyPurposeNotice",
    ),
    "repairs a group- or world-readable credential directory on save": (
        CLI_ROOT / "internal" / "credentials" / "credentials.go", "tightenDirMode(dir)",
    ),
    "decodes `usable` as an optional field, not a plain bool": (
        CLI_ROOT / "internal" / "backend" / "client.go", "Usable *bool",
    ),
    "shows the resolved endpoint before asking for the key": (
        CLI_ROOT / "internal" / "cli" / "login.go", "promptEndpoint",
    ),
    "verifies the key against the backend during sign-in": (
        CLI_ROOT / "internal" / "app" / "signin.go", "backend.CheckSignIn",
    ),
    "removes the credential on logout": (
        CLI_ROOT / "internal" / "cli" / "login.go", "credentials.Delete",
    ),
    "registers the new key as a redaction secret": (
        CLI_ROOT / "internal" / "app" / "signin.go", "redact.RegisterSecret",
    ),
    "fails doctor on a credential file wider than 0600": (
        CLI_ROOT / "internal" / "cli" / "doctorreport.go", "StatusFail",
    ),
}

# Row number -> the symbol whose PRESENCE means that row can no longer be ⏳.
# Chosen to appear only once the behaviour exists, not merely once the function
# does: `promptEndpoint` predates the endpoint actually being displayed, so row 2
# keys on the restatement itself.
CLI_LIFECYCLE_ROWS: dict[int, tuple[Path, str]] = {
    1: (CLI_ROOT / "internal" / "cli" / "login.go", "KeyPurposeNotice"),
    2: (CLI_ROOT / "internal" / "cli" / "login.go", "Signing in to"),
    3: (CLI_ROOT / "internal" / "app" / "signin.go", "backend.CheckSignIn"),
    7: (CLI_ROOT / "internal" / "cli" / "login.go", "credentials.Delete"),
    9: (CLI_ROOT / "internal" / "app" / "signin.go", "redact.RegisterSecret"),
}


@pytest.mark.parametrize("behaviour", sorted(CLI_LIFECYCLE_EVIDENCE))
def test_key_handling_only_claims_cli_behaviour_the_vendored_source_has(
    behaviour: str,
) -> None:
    _require_cli_submodule()
    path, symbol = CLI_LIFECYCLE_EVIDENCE[behaviour]
    assert symbol in path.read_text(encoding="utf-8"), (
        f"docs/KEY_HANDLING.md §5 claims the CLI {behaviour}, but {symbol} is gone "
        f"from {path.relative_to(REPO_ROOT)}. Either the CLI regressed or the pin "
        f"moved; the doc says ✅ either way, which is the failure this test exists for."
    )


def _lifecycle_rows() -> dict[int, str]:
    """The §5 lifecycle table, as `row number -> status cell`."""
    section = _read(DOC_KEYS).split("### Required lifecycle", 1)[-1]
    rows: dict[int, str] = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.split("|")]
        if len(cells) >= 4 and cells[1].isdigit():
            rows[int(cells[1])] = cells[3]
    return rows


@pytest.mark.parametrize("row", sorted(CLI_LIFECYCLE_ROWS))
def test_a_lifecycle_row_whose_code_has_landed_is_not_still_marked_pending(
    row: int,
) -> None:
    """The other direction, and the one that actually bit.

    Checking only that a claimed ✅ has its symbol catches a REGRESSION. It does
    nothing about the opposite drift: the pin moves, the behaviour arrives, and the
    row still says ⏳ because nobody looked. That is how a status table turns into
    an inventory of what used to be missing — the failure this section was rewritten
    to fix in the first place.
    """
    _require_cli_submodule()
    path, symbol = CLI_LIFECYCLE_ROWS[row]
    if symbol not in path.read_text(encoding="utf-8"):
        pytest.skip(f"row {row}'s implementation is genuinely absent from the pin")
    status = _lifecycle_rows().get(row)
    assert status is not None, f"docs/KEY_HANDLING.md §5 has no row {row}"
    assert "⏳" not in status, (
        f"docs/KEY_HANDLING.md §5 row {row} is still ⏳, but {symbol} is present in "
        f"{path.relative_to(REPO_ROOT)} — the pin moved and the status did not"
    )


def test_the_cli_verifies_the_key_before_it_reaches_disk() -> None:
    """The ordering claim, which symbol presence alone cannot establish.

    "Verify, then persist" is the whole value of row 3: a typo must never reach
    disk. Both calls being present in the file would satisfy the coarse check
    above even if they were the wrong way round.
    """
    _require_cli_submodule()
    source = (CLI_ROOT / "internal" / "app" / "signin.go").read_text(encoding="utf-8")
    verify = source.find("backend.CheckSignIn(")
    persist = source.find("credentials.Save(")
    assert verify != -1 and persist != -1, "sign-in no longer verifies or no longer persists"
    assert verify < persist, (
        "credentials.Save now runs before backend.CheckSignIn — a rejected key "
        "would reach disk, which docs/KEY_HANDLING.md §5 row 3 says cannot happen"
    )
