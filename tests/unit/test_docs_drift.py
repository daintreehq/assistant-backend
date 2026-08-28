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
from daintree_assistant_server.config import (
    EXECUTOR_KEY_ENV,
    EXECUTOR_MODEL,
    EXECUTOR_REASONING_EFFORT,
    INTERACTIVE_REASONING_EFFORT,
    INTERACTIVE_THINKING_ENABLED,
    PUBLIC_MODEL_ID,
    Settings,
)
from daintree_assistant_server.errors import DaintreeError
from daintree_assistant_server.providers.baseten import BasetenProvider
from daintree_assistant_server.services.task_runner import TaskRunner

#: The adapter production builds. Named rather than inlined so the credential
#: test below asserts the class as well as the key.
_SERVING_PROVIDER_CLASS = BasetenProvider

#: Where the host publishes its data-handling position. Written out rather than
#: derived from the API origin, because they are genuinely different domains:
#: Baseten serves from `inference.baseten.co` and publishes policy at
#: `baseten.co`.
_POLICY_DOMAIN = "baseten.co"

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
    """The promise has a source, and the doc must name the CURRENT one.

    This is the substantive difference from the old arrangement, and the one a
    reader most needs: the guarantee is no longer something this server enforces
    per request, it is the serving host's published policy, so a reader has to
    know whose policy to read.

    That makes WHICH HOST a privacy fact. This test used to assert the literal
    string "cerebras", which meant it kept passing after the host moved to
    a new host and actively required the stale name to stay — a drift gate holding
    the drift in place. It follows the serving constants now.
    """
    text = _read(DOC_PROVIDER).lower()
    assert "baseten" in text, (
        "docs/PROVIDER_POLICY.md does not name Baseten, the host whose published "
        "policy the privacy claim rests on."
    )
    assert _POLICY_DOMAIN in text, (
        "docs/PROVIDER_POLICY.md must link Baseten's own authoritative "
        "data-handling page. The privacy claim rests on it, so a reader has to be "
        "able to reach it."
    )


def test_provider_policy_states_the_privacy_posture_explicitly() -> None:
    text = _read(DOC_PROVIDER)
    assert "no-training guarantee" in text
    assert "not a no-retention" in text.lower() or "not a no-retention guarantee" in text.lower()


# ---------------------------------------------------------------------------
# Thinking posture.
# ---------------------------------------------------------------------------


def test_the_thinking_posture_is_documented() -> None:
    """The posture INVERTED with the serving model, and the doc must carry it.

    This gate used to require the sentence "nothing in this server thinks", which
    was true of DeepSeek V4 Flash — a model that reasons by default and can be
    held down with `reasoning: {"enabled": false}`. GLM-5.3-Flash cannot be held
    down at all: reasoning is always on, `none` is rejected, and an OMITTED
    `reasoning_effort` means `high`. So the old sentence is not merely stale, it
    is backwards, and a reader who believed it would think an omitted field was
    the cheap default when it is the most expensive one available.
    """
    assert INTERACTIVE_THINKING_ENABLED is False, (
        "The flag flipped. On this host it means 'request the floor', not 'no "
        "reasoning occurs' — if it is now True, docs/PROVIDER_POLICY.md section 2 "
        "and .env.example describe a posture that no longer holds; update them in "
        "this commit."
    )
    assert EXECUTOR_REASONING_EFFORT == "low", (
        "The pinned effort moved off the floor. That is a cost and latency change "
        "on every turn and every utility task, so docs/PROVIDER_POLICY.md section "
        "2 must be rewritten with it."
    )
    text = _read(DOC_PROVIDER).lower()
    assert "cannot be run non-thinking" in text, (
        "docs/PROVIDER_POLICY.md section 2 no longer states that the serving "
        "model cannot be run non-thinking, which is the whole posture."
    )
    assert "reasoning_effort" in text, (
        "docs/PROVIDER_POLICY.md must name the field that actually controls this; "
        "it is the only control the host exposes."
    )


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


@pytest.mark.parametrize("path", PROSE_FILES, ids=lambda p: p.name)
def test_no_document_claims_a_hard_zero_on_reasoning(path: Path) -> None:
    """The retired absolute must not come back anywhere in the prose.

    "Nothing in this server thinks" was accurate for two model generations and is
    now false, so it is the single most likely sentence to be copied forward from
    an older revision. It is forbidden by exact string rather than by shape,
    because the accurate copy still discusses thinking at length and a
    shape-matcher would fire on that.

    The two stale runbook-learning carve-outs are kept in the same guard: the
    sentence they qualified was written before the runbook rename, so forbidding
    only the newer wording would let the original claim return.
    """
    text = _read(path).lower()
    for stale in (
        "nothing in this server thinks",
        "runbook learning is the one surface that still thinks",
        "skill learning is the one surface that still thinks",
    ):
        assert stale not in text, (
            f"{path.name} says {stale!r}. The serving model reasons "
            "unconditionally and the server pins the `low` floor; see "
            "docs/PROVIDER_POLICY.md section 2."
        )


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
    for model in (EXECUTOR_MODEL, PUBLIC_MODEL_ID):
        assert model in text, (
            f"docs/PROVIDER_POLICY.md does not name {model!r}. The model table is "
            "the documented source of truth for what actually answers a call."
        )


def test_readme_names_the_pinned_main_model() -> None:
    assert EXECUTOR_MODEL in _read(README)


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
            "upstream credentials and funds every turn from them; "
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
    expected = f"settings.{EXECUTOR_KEY_ENV.lower()}"
    assert expected in builder or "settings.serving_api_key" in builder, (
        "main._build_provider no longer passes the server credential. Readiness "
        f"gates on {EXECUTOR_KEY_ENV} precisely so this cannot be missing at "
        "runtime; a provider built without it cannot answer anything."
    )


def test_the_serving_provider_really_holds_the_credential() -> None:
    """The behaviour behind the doc claim, not just the source text.

    A request that arrives with no caller key must still be servable — that is the
    entire current credential model, and the thing an anonymous CLI depends on.
    """
    settings = Settings(**{
        "STATE_SIGNING_KEY_CURRENT": "x" * 32,
        EXECUTOR_KEY_ENV: "sk-server-owned",
    })
    provider = main_module._build_provider(settings)
    assert isinstance(provider, _SERVING_PROVIDER_CLASS)
    # No per-call key: the server's own is what gets resolved.
    assert provider._resolve_key(None).get_secret_value() == "sk-server-owned"


def test_a_caller_supplied_key_still_overrides_the_server_credential() -> None:
    """The seam account authentication will land in.

    A per-request credential must keep winning over the server's, even though
    nothing on the serving path sets one today. Keeping this live is what makes
    account-mapped credentials a value change rather than a plumbing change.
    """
    from pydantic import SecretStr as _SecretStr

    settings = Settings(**{
        "STATE_SIGNING_KEY_CURRENT": "x" * 32,
        EXECUTOR_KEY_ENV: "sk-server-owned",
    })
    provider = main_module._build_provider(settings)
    resolved = provider._resolve_key(_SecretStr("sk-caller-supplied"))
    assert resolved.get_secret_value() == "sk-caller-supplied"


# THE DOOR IS OPEN BY DEFAULT, and the docs must say so plainly.
#
# Verification exists now, behind `AUTH_MODE`, but the shipped posture is still
# `open`: the server holds the upstream credential, so there is no per-request
# credential for a bearer to supply. A document that tells a tester their request
# will be rejected without a key sends them hunting for something that does not
# exist on any deployment running today.
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
            "served on the default posture: AUTH_MODE is `open`, under which "
            "auth.authenticate does not read the Authorization header at all and "
            "the server funds the turn. See auth.py."
        )


def test_the_open_door_is_still_the_default_posture() -> None:
    """The distinction that protects a CLI mid-migration.

    "Optional" would mean reading a bearer when one is sent — and a client still
    holding its old key would have that key threaded to the provider and
    forwarded upstream, which rejects a credential it never issued. The caller
    would see ``provider_invalid_api_key``: their key blamed for a header they
    were right to still be sending.

    Verification EXISTS now, so the promise is carried by ``AUTH_MODE`` rather
    than by there being no code to break it. Asserted on the default rather than
    on the mechanism: a deployment that sets nothing must not have moved.
    """
    from daintree_assistant_server.config import Settings

    settings = Settings(STATE_SIGNING_KEY_CURRENT="x" * 32, BASETEN_API_KEY="k")
    assert settings.auth_mode == "open"
    text = _read(SRC_ROOT / "auth.py")
    assert 'if mode == "open"' in text and "return ANONYMOUS" in text, (
        "auth.py no longer short-circuits the open door before reading the header"
    )


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
    off and runbook learning gone, no call in the system sets ``reasoning_effort``
    at all — which is what the doc's thinking table now says.

    The doc assertion moved with the host. It used to check that §2 called the
    posture a property of the MODEL ("no reasoning mode"), which was Cerebras's
    situation; on Together it is a flag genuinely held down, and a doc still
    claiming the model cannot reason would be describing the wrong mechanism —
    the one where forgetting to send the disable object costs nothing.
    """
    assert INTERACTIVE_THINKING_ENABLED is False
    assert INTERACTIVE_REASONING_EFFORT  # still defined, still unsent
    text = _read(DOC_PROVIDER).lower()
    assert 'reasoning: {"enabled": false}' in text, (
        "docs/PROVIDER_POLICY.md §2 no longer names the object that actually "
        "switches reasoning off. The serving model reasons by DEFAULT, so the "
        "posture is a flag held down, not a property of the model."
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
    # The Daintree ACCOUNT family. Which of these a deployment can raise depends
    # on its posture — the identity codes under AUTH_MODE=enforce, the
    # subscription and dependency codes under ENTITLEMENT_MODE=enforce, none of
    # them under the shipped defaults — and two are unreachable for other reasons
    # (auth_permission_denied needs a second OAuth client, usage_limit_reached
    # needs plan allowances nobody has decided). Documented regardless: the
    # taxonomy, the docs and the client have to agree before anything depends on
    # them, and afterwards is too late to notice they do not.
    "auth_required",
    "auth_token_invalid",
    "auth_token_expired",
    "auth_session_revoked",
    "auth_client_not_allowed",
    "auth_permission_denied",
    "subscription_required",
    "subscription_inactive",
    "usage_limit_reached",
    "account_rate_limited",
    "auth_dependency_unavailable",
    "entitlement_unavailable",
    "usage_accounting_unavailable",
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
    # Raised from BOTH sides of the upstream call: by `services/context_budget.py`
    # when the assembled prompt is visibly too large, and by the provider adapter
    # when the host says so first. One code either way, which is the contract the
    # CLI branches on.
    "context_window_exceeded",
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

    Runbook learning used to be the other entry — and the more dangerous one, since
    it mutated the runbook catalog from model output at runtime. It has been
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


def test_no_document_still_describes_runbook_learning() -> None:
    """It is gone, and a document that still offers it is worse than silent.

    A reader following `.env.example` or the README would set
    `RUNBOOK_LEARNING_MODE`, see nothing happen (extra env vars are ignored), and
    have no way to tell whether the feature was broken or absent.
    """
    offenders: list[str] = []
    for path in PROSE_FILES:
        for number, line in enumerate(_read(path).splitlines(), start=1):
            lowered = line.lower()
            # The feature was called "skill learning" when it existed and its env var was
            # SKILL_LEARNING_*. Scanning only the renamed spelling would miss a document
            # that still offers the setting a reader could actually type.
            if (
                "RUNBOOK_LEARNING" in line
                or "SKILL_LEARNING" in line
                or "runbook-learning" in lowered
                or "skill-learning" in lowered
            ):
                # A line explaining the REMOVAL is the accurate copy.
                if re.search(r"\b(?:removed|no longer|used to|gone|deleted)\b", line, re.I):
                    continue
                offenders.append(f"{path.name}:{number}")
    assert not offenders, (
        f"{offenders} still describe runbook learning, which has been removed."
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

    # `fast_response` joined when the preamble stage landed: a visible turn now
    # makes TWO billed calls, and a total that hides one of them is the same
    # "partial sum presented as complete" the `complete` flag exists to prevent.
    assert set(TurnCost.model_fields) == {
        "total",
        "main",
        "fast_response",
        "selector",
        "complete",
    }
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
    temptation to reach for it survives a provider swap because `cost` is still
    on `ProviderUsage`.
    """
    import inspect as _inspect

    from daintree_assistant_server.providers import openrouter as _or

    source = _inspect.getsource(_or.OpenRouterProvider._build_body)
    assert '"usage"' not in source, (
        "_build_body sends a `usage` key. See docs/PROVIDER_POLICY.md section 5c."
    )


def test_the_documented_total_is_a_sum_of_both_calls() -> None:
    """Section 5c says a turn is normally two billed calls and total covers both.

    Asserted against the arithmetic rather than the prose, so a change that
    quietly reports only the main call fails here.
    """
    from daintree_assistant_server.contracts.daintree_api import RunbooksBlock
    from daintree_assistant_server.contracts.extensions import SelectorMeta, SelectorUsage
    from daintree_assistant_server.providers.base import ProviderUsage
    from daintree_assistant_server.services.chat_orchestrator import _RespondMeta
    from daintree_assistant_server.services.turn_accounting import turn_cost

    meta = _RespondMeta(
        request_id="r",
        state=None,
        catalog_revision="c",
        prompt_version="p",
        runbooks=RunbooksBlock(
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


# The CLI stores ONE secret, and §5 has to keep saying which one.
#
# The claim these guard has been rewritten twice, in opposite directions, and the
# second rewrite is why they look the way they do. `feat: drop the API-key
# requirement` deleted the credentials package, the sign-in path and the
# login/logout commands, and §5 then said — correctly, for a while — that the CLI
# stored nothing at all. Account authentication put persistence back: the CLI now
# keeps exactly one secret, the OAuth refresh token, in the operating system's
# credential service (`cli/internal/auth/store.go`, `keyring.go`).
#
# So an absence check is no longer enough, and the three tests below are split by
# what they can actually establish. Naming the deleted paths still catches the OLD
# flow coming back, but it passed unchanged straight through the pin that
# introduced the keyring — which is exactly the failure mode this comment exists to
# stop a later reader from re-creating. A doc saying "no credential on disk" while
# one is on disk is the worst direction for this particular claim to be wrong in,
# so the store itself is asserted positively as well.
_REMOVED_CREDENTIAL_SOURCES = (
    Path("internal") / "credentials" / "credentials.go",
    Path("internal") / "cli" / "login.go",
    Path("internal") / "app" / "signin.go",
)


@pytest.mark.parametrize("relative", _REMOVED_CREDENTIAL_SOURCES, ids=lambda p: p.name)
def test_the_cli_never_restores_the_old_key_lifecycle(relative: Path) -> None:
    """The pre-OAuth flow — a prompted API key in a per-user credentials file — is
    gone and must not come back. This says nothing about the refresh token the
    keyring now holds; that is the test below."""
    _require_cli_submodule()
    assert not (CLI_ROOT / relative).exists(), (
        f"cli/{relative} is back, so the CLI may collect and store an API key "
        f"again. docs/KEY_HANDLING.md §5 describes a single OAuth refresh token in "
        f"the OS credential service and no key lifecycle at all — rewrite §5 "
        f"against whatever the flow now is."
    )


def test_the_only_persisted_secret_is_the_refresh_token() -> None:
    """§5's positive claim, and the one an absence check cannot make.

    Two halves, because the interesting way for this to go wrong is not the store
    disappearing but its TERMS changing. A credential store that quietly degrades
    to a file on disk when the keychain is unavailable is worse than one that
    refuses, since the user is told they are signed in and never told the terms
    changed — so the in-memory fallback is asserted by name, not merely the
    existence of a keyring.
    """
    _require_cli_submodule()
    store = (CLI_ROOT / "internal" / "auth" / "store.go").read_text(encoding="utf-8")
    assert "Exactly one secret is persisted: the refresh token." in store, (
        "cli/internal/auth/store.go no longer says the refresh token is the only "
        "persisted secret. docs/KEY_HANDLING.md §5 says exactly that, and names the "
        "access token as memory-only — check what changed before editing this."
    )
    keyring = (CLI_ROOT / "internal" / "auth" / "keyring.go").read_text(encoding="utf-8")
    assert "NewMemoryStore(), TierMemory" in keyring, (
        "cli/internal/auth/keyring.go no longer degrades to the in-memory store when "
        "no OS credential service is available, so a machine without one may now be "
        "persisting somewhere else. docs/KEY_HANDLING.md §5 promises no silent "
        "plaintext: the session evaporates on exit and the user is told so."
    )


def test_no_credential_is_sent_on_the_public_discovery_path() -> None:
    """§5's live claim about the wire, and it is NOT "an unset key sends no header".

    That older claim died with the frozen `apiKey string` it described. The client
    resolves a `TokenSource` once at construction — an account credential CHANGES
    between requests, and a frozen string would pin the client to a token that
    stops working in an hour — and `NewAccountTokenSource` hands back a live
    manager precisely WHEN `DAINTREE_API_KEY` is empty. So on a signed-in install
    an empty key sends a bearer token, and a test asserting otherwise would have
    been pinning a property the CLI no longer has.

    What survives is narrower and is the half the backend actually depends on:
    the paths a client must be able to call before it holds anything to sign in
    with are on a closed allowlist that bypasses credential lookup entirely, and a
    token source yielding nothing sets no header. Together those are what let this
    server answer `/v1/daintree/auth/config` to a caller with no credential, and
    what keeps a signed-out install from being blamed for a header it never sent.
    """
    _require_cli_submodule()
    tokensource = (CLI_ROOT / "internal" / "backend" / "tokensource.go").read_text(
        encoding="utf-8"
    )
    assert '"/v1/daintree/auth/config"' in tokensource, (
        "cli/internal/backend/tokensource.go no longer lists /v1/daintree/auth/config "
        "among the paths callable with no credential. That endpoint is the bootstrap: "
        "requiring a token to learn how to get a token is a loop no client can exit "
        "(src/daintree_assistant_server/api/daintree_auth.py)."
    )
    assert "func (NoTokenSource) AccessToken" in tokensource and (
        'return ""' in tokensource
    ), (
        "cli/internal/backend/tokensource.go no longer has a NoTokenSource that "
        "yields an empty token, so a signed-out install may now send something."
    )
    client = (CLI_ROOT / "internal" / "backend" / "client.go").read_text(encoding="utf-8")
    guarded = re.search(
        r'if\s+token\s*!=\s*""\s*\{\s*req\.Header\.Set\(\s*"Authorization"',
        client,
    )
    assert guarded, (
        "cli/internal/backend/client.go no longer guards the Authorization header on "
        "a non-empty token, so an empty one may now be sent as `Bearer `. "
        "docs/KEY_HANDLING.md §5 says a source yielding nothing sends no header."
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
# The benchmark READMEs carried hand-copied absolute numbers — "16 runbooks",
# "~20.3 KB selector prompt" — long enough that a reviewer quoted them back as
# current against a 31-runbook catalog. Hand-copied metrics decay silently; this
# pins the two that a reader is most likely to act on.
# ---------------------------------------------------------------------------

BASELINE = REPO_ROOT / "benchmarks" / "baseline.json"
DOC_PROMPT_COST = REPO_ROOT / "benchmarks" / "prompt_cost" / "README.md"


_SNAPSHOT_HEADER = re.compile(r"Measured\s+\*{0,2}(\d{4}-\d{2}-\d{2})\*{0,2},?\s+"
                              r"[^\n]*?(\d+)[- ]runbooks?[^\n]*?(\d+)[- ]tools?", re.I)


def test_the_prompt_cost_readme_declares_exactly_one_current_snapshot() -> None:
    """Two "Measured …" headers above the history section means the file
    contradicts itself, which is how the stale 16-runbook table survived beside a
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
    runbooks = live["catalog"]["runbooks"]
    tools = live["tools"]["count"]
    current = _read(DOC_PROMPT_COST).split("### Snapshot history", 1)[0]
    header = _SNAPSHOT_HEADER.search(current)
    assert header, (
        "benchmarks/prompt_cost/README.md has no dated 'Measured <date>, N runbooks / "
        "M tools' line — the snapshot cannot be checked for staleness without one"
    )
    assert (int(header.group(2)), int(header.group(3))) == (runbooks, tools), (
        f"the prompt_cost README snapshot says {header.group(2)} runbooks / "
        f"{header.group(3)} tools; the live catalog and tool fixture are "
        f"{runbooks} / {tools}. Re-run `python -m benchmarks.prompt_cost.bench run` "
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


# ---------------------------------------------------------------------------
# Retired architecture, stated in the present tense.
# ---------------------------------------------------------------------------

#: Claims that were TRUE of an earlier Daintree and are now false. Each is a
#: phrase specific enough that a false positive is unlikely and a real
#: reintroduction is certain to trip it.
#:
#: These are not ordinary doc-quality nits. Repository prose is what a coding
#: agent reads as context before changing anything, so a document that describes
#: a deleted architecture in the present tense is an instruction to rebuild it.
#: `docs/DAINTREE_API.md` was exactly that for a whole generation: it documented
#: caller-supplied OpenRouter keys, a `provider` endpoint-selection block and a
#: model-call runbook selector, all removed, while calling itself the wire
#: contract.
_RETIRED_CLAIMS = (
    "caller's own api key as the bearer",
    "the key is their openrouter key",
    "runbook selection runs on a separate small model",
    # The pre-rename wording of the same retired claim.
    "skill selection runs on a separate small model",
    "endpoint-selection policy",
    "throughput-sorted",
    "caller owns the key and pays the bill",
    "openrouter is the sole provider",
    "pass-through for the caller's own",
    "tester pastes their own",
    "the key never leaves the request path",
)

#: A line may discuss a retired design when it says so. Kept deliberately small:
#: the escape hatch is for labelled history, not for softening a live claim.
_HISTORY_MARKERS = (
    "historical",
    "used to",
    "no longer",
    "previously",
    "was deleted",
    "were deleted",
    "git history",
    "retired",
    "before the",
    # Struck-through text is history by typography; a reader cannot mistake it.
    "~~",
    "superseded",
    "was written when",
)


def test_no_document_states_a_retired_architecture_in_the_present_tense() -> None:
    docs = [README, DOC_API, DOC_PROVIDER, DOC_BETA, DOC_KEYS, REPO_ROOT / "CLAUDE.md"]
    problems: list[str] = []
    for path in docs:
        if not path.exists():
            continue
        lines = _read(path).splitlines()
        for number, line in enumerate(lines, start=1):
            # Prose here is hard-wrapped, so a negation regularly lands at the end
            # of the previous line ("…so there is no\nendpoint-selection policy").
            # Scanning one line at a time would read those as live claims.
            previous = lines[number - 2] if number >= 2 else ""
            # Only the previous line's TAIL, and only to complete a wrapped
            # sentence — a history marker earlier in the paragraph must not
            # license a live claim on this line.
            wrapped = "" if not previous.rstrip() else previous.rstrip()[-40:]
            lowered = (wrapped + " " + line).lower()
            # A history marker excuses its own SENTENCE. Normally that is the line
            # it appears on — a previous line ending "This was deleted previously."
            # must not wave through a fresh claim on the next one. But prose here
            # is hard-wrapped, so when the claim itself only completes by spanning
            # the wrap, the sentence began on the previous line and that line's
            # marker governs it.
            here = line.lower()
            marked_here = any(marker in here for marker in _HISTORY_MARKERS)
            marked_before = any(marker in previous.lower() for marker in _HISTORY_MARKERS)
            if marked_here:
                continue
            for claim in _RETIRED_CLAIMS:
                index = lowered.find(claim)
                if index < 0:
                    continue
                # A NEGATED mention is the correction, not the defect: "there is
                # no endpoint-selection policy" is exactly the sentence this test
                # wants the docs to contain. Look only at the words immediately
                # before the claim so a distant "no" elsewhere in a long line
                # cannot excuse a live assertion.
                # The negation has to GOVERN the claim, so it must be adjacent:
                # "there is no endpoint-selection policy" is the correction this
                # test wants, while "There is no ambiguity: the caller owns the
                # key" is a live assertion with an unrelated "no" nearby. Only a
                # copula or a bare "no"/"never" immediately before the phrase
                # counts.
                preceding = lowered[max(0, index - 24) : index].rstrip()
                if any(
                    preceding.endswith(negation)
                    for negation in ("no", "not", "never", "nor", "neither")
                ):
                    continue
                # Found only by spanning the wrap: the sentence started on the
                # previous line, so that line's history marker applies to it.
                if marked_before and claim not in here:
                    continue
                problems.append(f"{path.name}:{number} states {claim!r}")
    assert not problems, (
        "Retired architecture described as current:\n  "
        + "\n  ".join(problems)
        + "\n\nEither correct the statement or mark the passage as history "
        "(a nearby 'historical', 'no longer', 'used to' … makes the intent explicit)."
    )


# ---------------------------------------------------------------------------
# An UNVERIFIED privacy section must not be quoted as if it were verified.
# ---------------------------------------------------------------------------

#: The marker §4 carries while nobody has read the current host's terms. It is
#: prose rather than a machine flag on purpose — a reader who reaches the section
#: has to be stopped by the first thing they see, not by a comment.
_PRIVACY_UNVERIFIED = "unverified against the current host"

#: The claim itself, in the forms a summary reaches for. These are exactly what
#: `README.md` said until this gate was written: an external trust statement,
#: sourced from a section that is explicitly not about the host now serving.
_NO_TRAINING_CLAIMS = (
    "not collected for training",
    "no-training guarantee",
    "does not train on",
    "not used for training",
    # Added after `docs/INTERNAL_BETA.md` was found asserting exactly this to
    # TESTERS — the highest-stakes surface of the four, since it is the copy
    # onboarding reads out — while slipping past every phrasing above. The claim
    # travels in more shapes than the ones it was first caught in.
    "do not collect or train on",
    "does not collect or train on",
    "no training on",
)

#: Surfaces a reader takes a claim FROM. `PROVIDER_POLICY.md` is excluded because
#: it is the section under caveat — it has to be able to describe the claim it is
#: withholding, and it carries the warning that makes that safe.
_SUMMARY_SURFACES = ("README.md", "CLAUDE.md", "docs/INTERNAL_BETA.md", "docs/KEY_HANDLING.md")


def test_no_summary_repeats_a_privacy_claim_the_policy_has_not_verified() -> None:
    """A caveat in one document does not travel to the documents quoting it.

    §4 has carried an explicit "not verified for this host" warning since the
    provider moved, and `README.md` went on asserting "Requests are not collected
    for training" the whole time — a hard external trust statement, sourced from
    a section that says in bold it is about a different vendor. That is the exact
    failure mode a caveat has: it protects the page it sits on and nothing else.

    So the gate is conditional, not absolute. While §4 says it is unverified, no
    summary may state the claim. Once somebody reads Together's terms and rewrites
    §4, this test stops applying by itself — which is the right shape, because the
    claim becomes repeatable at exactly the moment it becomes true.
    """
    policy = _read(DOC_PROVIDER).lower()
    if _PRIVACY_UNVERIFIED not in policy:
        pytest.skip(
            "docs/PROVIDER_POLICY.md §4 no longer declares itself unverified — the "
            "claim is repeatable and this gate has done its job"
        )

    offenders: list[str] = []
    for name in _SUMMARY_SURFACES:
        path = REPO_ROOT / name
        if not path.exists():
            continue
        for number, line in enumerate(_read(path).lower().splitlines(), start=1):
            if _OPT_OUT in line:
                continue
            for claim in _NO_TRAINING_CLAIMS:
                # "do not repeat it", "used to state" and similar are the honest
                # way to MENTION the claim; only asserting it is the failure.
                if claim in line and not re.search(
                    r"\b(?:unverified|not verified|do not repeat|used to|previously|no longer|until)\b",
                    line,
                ):
                    offenders.append(f"{name}:{number}: {claim!r}")

    assert not offenders, (
        "a summary asserts a privacy claim docs/PROVIDER_POLICY.md §4 says is "
        "unverified for the current host:\n  " + "\n  ".join(offenders) + "\n\n"
        "Either verify §4 against the host's published terms and rewrite it, or "
        "stop stating the claim here. An external trust statement nobody has "
        "checked is the worst thing in this repository to be wrong about."
    )


# ---------------------------------------------------------------------------
# The wire contract: what the adapter sends, and what the prose says it sends.
#
# `docs/PROVIDER_POLICY.md` §8 is explicit that the code proves exactly one class
# of fact — WHICH PARAMETERS WE SEND — and that this is what the drift tests pin.
# These are that pin for the cache-affinity field. It is the claim most worth
# holding: a reader who believes a cache key is sent optimizes the key, and the
# only lever that exists is literal prefix stability.
# ---------------------------------------------------------------------------

#: EVERY maintained prose file, not just the seven a reader is pointed at.
#: `PROSE_FILES` deliberately omits benchmark documentation and `TODO.md` — the
#: first because most drift there is harmless, the second because it is future
#: tense by construction. Neither exemption survives contact with THIS claim:
#: the false sentence that motivated this scan had a twin in
#: `benchmarks/prompt_cost/README.md` ("a stable `session_id` — what the server
#: sends today"), describing a paid experiment built on it, and nothing would
#: have looked there. A wire fact is not future tense wherever it is written.
_VENDORED = ("cli", ".venv", "build", "node_modules", ".git")


def _all_prose_files() -> list[Path]:
    files = [
        path
        for path in REPO_ROOT.rglob("*.md")
        if not any(part in _VENDORED for part in path.relative_to(REPO_ROOT).parts)
        # Benchmark RESULTS are generated artifacts, not prose anyone maintains.
        and "results" not in path.relative_to(REPO_ROOT).parts
    ]
    assert files, "no prose files found — the walk is broken, not the repository"
    return sorted(files)


def test_the_prose_walk_reaches_the_documents_this_scan_exists_for() -> None:
    """A corpus that silently stopped including a file is a scan that passes.

    Named explicitly rather than trusted to the glob: `PROVIDER_POLICY.md` is
    where the fact lives, `CLAUDE.md` is where it was wrong, and the prompt-cost
    README is the one `PROSE_FILES` would have missed.
    """
    found = {p.relative_to(REPO_ROOT).as_posix() for p in _all_prose_files()}
    for required in (
        "CLAUDE.md",
        "README.md",
        "TODO.md",
        "docs/PROVIDER_POLICY.md",
        "docs/DAINTREE_API.md",
        "benchmarks/prompt_cost/README.md",
        "benchmarks/latency/README.md",
    ):
        assert required in found, f"{required} is outside the affinity scan's corpus"
    assert not any(f.startswith("cli/") for f in found), "vendored CLI must stay excluded"


#: Both vendors' WIRE spellings, plus the NORMALIZED request's own field name.
#: All three are absent from the body; `cache_session_id` is included because
#: "`cache_session_id` is sent to Together" is exactly as wrong as the other two
#: and a lookbehind excluding it wholesale let that sentence through.
_AFFINITY_FIELDS = re.compile(r"prompt_cache_key|cache_session_id|(?<!cache_)session_id")

#: A sentence may NAME a field it is telling you is not sent. What fails is a
#: sentence asserting one is. These are the verbs that make it an assertion.
_SENDS = re.compile(
    r"\b(?:sends?|sending|sent|carries|carrying|carried|includes?|including|"
    r"included|contains?|containing|passes|passing|passed|forwards?|forwarded|"
    r"posts?|posted|adds?|added|appends?|appended|emits?|emitted|attaches?|"
    r"attached|attaching|sets?|supplies|supplied|reaches|reaching|reached|"
    r"puts?|transmits?|transmitted)\b"
)

#: ...unless the same CLAUSE is what negates it. Clause, not line: "no
#: `provider` block is sent, and a `prompt_cache_key` is attached" negates the
#: wrong half, and a line-wide search would read the whole sentence as safe.
_NOT_SENT = re.compile(
    r"\b(?:no|not|never|nothing|neither|nor|without|dropped?|drops|withheld|"
    r"withholds?|omitted?|omits|unsent|stripped?|strips|would)\b"
)

#: A clause about a version that no longer exists is not a claim about what this
#: one sends, and this repository's prose is full of them — "the retired Cerebras
#: adapter's spelling", "an older revision sent". Kept SEPARATE from `_NOT_SENT`
#: rather than folded in, because the two exemptions are different arguments and
#: a single widening list is how a scan quietly stops scanning: negation says the
#: sentence denies the claim, history says the sentence is not about today.
_HISTORICAL = re.compile(
    r"\b(?:used to|previously|formerly|historically|no longer|retired|deleted|"
    r"older|earlier|prior|former|since removed|at the time)\b"
)

#: `cache_session_id` genuinely IS carried — on the normalized request, which
#: `_build_body` then reads and discards. A clause saying so is true, and a scan
#: that failed it would push the docs toward describing the field less clearly.
_INTERNAL_CARRIER = re.compile(
    r"normali[sz]ed request|providerchatrequest|normali[sz]ed "
    r"``?providerchatrequest|on the request object"
)

#: Where one clause ends and the next begins. Markdown prose is one long
#: sentence more often than it should be, and the conjunction is where a true
#: half and a false half get joined.
_CLAUSE_SPLIT = re.compile(r"[;.]|(?:,?\s+(?:and|but|while|whereas|though)\s+)|,")


def _paragraphs(text: str) -> list[tuple[int, str]]:
    """Blank-line-separated paragraphs, each with the line it starts on.

    Scanning LINES misses a claim markdown soft-wrapped across two of them —
    "Every request includes the affinity field" on one line and the field's name
    on the next share no line, so a line-local scan sees a verb with no field
    and a field with no verb, and passes both.
    """
    out: list[tuple[int, str]] = []
    buffer: list[str] = []
    start = 1
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip():
            if not buffer:
                start = number
            buffer.append(line.strip())
            continue
        if buffer:
            out.append((start, " ".join(buffer)))
            buffer = []
    if buffer:
        out.append((start, " ".join(buffer)))
    return out


def _asserts_the_field_is_sent(paragraph: str) -> str | None:
    """The offending clause, or ``None`` if the paragraph makes no such claim."""
    for clause in _CLAUSE_SPLIT.split(paragraph.lower()):
        if not clause or not _AFFINITY_FIELDS.search(clause):
            continue
        if not _SENDS.search(clause):
            continue
        if (
            _NOT_SENT.search(clause)
            or _HISTORICAL.search(clause)
            or _INTERNAL_CARRIER.search(clause)
        ):
            continue
        return clause.strip()
    return None


@pytest.mark.parametrize("path", _all_prose_files(), ids=lambda p: p.name)
def test_no_document_claims_a_cache_affinity_field_is_sent(path: Path) -> None:
    """CLAUDE.md said calls "still send a `prompt_cache_key`". They never did.

    The adapter computes the key and `_build_body` drops it, because a host with
    automatic prefix caching advertises no spelling to send it under and an
    unadvertised parameter is a needless 400 risk. The prose disagreeing with
    that is not a cosmetic error: it aims cache work at a field nobody reads, and
    it did — a paid benchmark A/B'd a parameter that never left the process.
    """
    offenders: list[str] = []
    for line_number, paragraph in _paragraphs(_read(path)):
        clause = _asserts_the_field_is_sent(paragraph)
        if clause is not None:
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line_number}: {clause}")

    assert not offenders, (
        "prose claims a cache-affinity field reaches the wire:\n  "
        + "\n  ".join(offenders)
        + "\n\nThe adapter sends neither spelling (docs/PROVIDER_POLICY.md §3a). "
        "Say what it does instead: the key is computed, and dropped."
    )


@pytest.mark.parametrize(
    "prose",
    [
        # The sentence this test was written for, held as a fixture. A scan that
        # no longer fires on what motivated it has silently stopped working.
        "calls still send a `prompt_cache_key` naming their prefix, but as a "
        "*hint* the upstream may ignore",
        # Unrelated negation. A line-wide `_NOT_SENT` search reads the "No" and
        # clears the whole sentence, including the false half after it.
        "No `provider` block is sent, and a `prompt_cache_key` is attached to "
        "every request.",
        # Verbs the first draft of `_SENDS` did not know.
        "The adapter forwards `prompt_cache_key` upstream.",
        "Each body contains a `prompt_cache_key`.",
        "We add a `session_id` to every completion request.",
        # The internal spelling. Excluding it wholesale let this through.
        "`cache_session_id` is sent to Together on every turn.",
        # Markdown soft wrap: the verb and the field name never share a line.
        "Every generation request includes the cache-affinity field\n"
        "`prompt_cache_key`, so a stable session stays warm.",
    ],
)
def test_the_affinity_scan_catches_a_claim_it_must_catch(prose: str) -> None:
    paragraphs = _paragraphs(prose)
    assert any(_asserts_the_field_is_sent(text) for _, text in paragraphs), prose


@pytest.mark.parametrize(
    "prose",
    [
        # The true statements have to survive, or the fix for a failure is
        # deletion rather than correction.
        "No `prompt_cache_key` is sent, and no `provider` block.",
        "`session_id` is computed and never sent to the host.",
        "It is dropped before the body is built, under both spellings "
        "(`prompt_cache_key`, `session_id`).",
        "`_build_body` strips the `prompt_cache_key` it was handed.",
        "Every main conversation call and utility task carries a "
        "`cache_session_id` on the normalized request.",
        "An older revision sent a `prompt_cache_key`; this one does not.",
        "`prompt_cache_key` was the retired Cerebras adapter's spelling.",
    ],
)
def test_the_affinity_scan_permits_a_true_statement(prose: str) -> None:
    paragraphs = _paragraphs(prose)
    assert not any(_asserts_the_field_is_sent(text) for _, text in paragraphs), prose


def test_the_historical_exemption_is_not_a_universal_escape_hatch() -> None:
    """It clears a clause about an old revision, not a clause next to one.

    `_HISTORICAL` is the widest of the three exemptions and the easiest to
    over-apply. Clause scoping is what keeps it honest: a true history note and a
    false present-tense claim in one sentence must still fail on the second half.
    """
    prose = (
        "`session_id` was the retired OpenRouter spelling, and we now send "
        "`prompt_cache_key` on every request."
    )
    assert any(_asserts_the_field_is_sent(text) for _, text in _paragraphs(prose))


def test_both_cached_token_shapes_are_read() -> None:
    """§3b of the provider policy describes a fallback. This is the fallback.

    Pinned here rather than only in the provider tests because the doc is what a
    reader consults before trusting a cache ratio, and a fallback that quietly
    stopped existing would leave that section describing an adapter we no longer
    ship.
    """
    from daintree_assistant_server.providers.base import ProviderUsage

    nested = ProviderUsage.from_wire(
        {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 64}}
    )
    flat = ProviderUsage.from_wire({"prompt_tokens": 100, "cached_tokens": 64})
    assert (nested.cached_tokens, nested.cached_tokens_source) == (64, "nested")
    assert (flat.cached_tokens, flat.cached_tokens_source) == (64, "flat")

    policy = _read(DOC_PROVIDER)
    assert "prompt_tokens_details" in policy, (
        "docs/PROVIDER_POLICY.md no longer documents where cached tokens arrive; "
        "the parser reads two shapes and the doc is where that is explained"
    )


def test_no_document_claims_the_serving_path_spends_a_caller_key() -> None:
    """The server funds every turn from its own key, whatever the auth posture.

    `ProviderChatRequest.api_key` still exists as the slot an account-mapped
    credential would occupy, so its comment claiming the serving path "always"
    fills it with the caller's own key was the credential model exactly
    inverted — on the one field where being wrong routes a real secret.

    Account authentication did NOT change this. A verified principal carries an
    identity and no credential: an account is an entitlement to spend Daintree's
    key, not a mapping to one of the caller's.
    """
    from daintree_assistant_server.auth import Principal

    for attribute in ("api_key", "token", "access_token", "credential"):
        assert attribute not in Principal.__dataclass_fields__, (
            f"Principal now carries {attribute} — this test and every doc "
            "describing the credential model need rewriting together"
        )

    base = _read(SRC_ROOT / "providers" / "base.py")
    assert "always the\n    # CALLER's own key" not in base
    assert "CALLER's own key (their bearer token)" not in base, (
        "providers/base.py claims the serving path threads the caller's key. "
        "It threads None; the provider's own configured key pays."
    )


def test_the_version_posture_field_is_documented() -> None:
    """A field a release gate reads has to be described where the wire is.

    `/version` is public, unauthenticated and probed by Cloud Run, so the standing
    rule is that nothing lands there for convenience. Publishing the two rollout
    modes was a deliberate exception — the pre-promotion smoke has to prove a
    candidate came up in the mode the release asked for, and `auth/config` cannot
    tell `open` from `observe` because nothing a caller can observe does either.
    An exception recorded only in the code is one that reads as an oversight to
    the next person, and the next person deletes it.

    Both mode names are derived from `config.py` rather than typed here, so a
    third mode on either axis fails this until the document names it.
    """
    from daintree_assistant_server.config import AuthMode, EntitlementMode

    text = _read(DOC_API)
    assert '"posture"' in text, (
        "GET /version publishes a posture block that docs/DAINTREE_API.md never shows"
    )
    for setting in ("AUTH_MODE", "ENTITLEMENT_MODE"):
        assert setting in text, f"the /version posture reports {setting}; the doc omits it"
    for mode in (*get_args(AuthMode), *get_args(EntitlementMode)):
        assert f"`{mode}`" in text, (
            f"{mode!r} is a posture value /version can report and the doc never names it"
        )


def test_every_setting_is_named_in_env_example() -> None:
    """A setting absent from `.env.example` is a setting nobody knows exists.

    This file is the only enumeration of the configuration surface a reader ever
    sees — `Settings` is 1,600 lines of prose-heavy source, and Cloud Run's own
    env list names only the dozen values the release trigger passes. So the gate
    is coverage, not commentary: every field must appear as an assignment, live
    for the ones a deployment sets and commented for the ones whose defaults are
    the answer.

    It was 57 of 100 when this was written, and the missing 43 were not the
    obscure half — they included `MAIN_OUTPUT_LINT_ENABLED`, which CLAUDE.md
    names as one of exactly three switches whose right value is "whatever
    production says this week", and `UPSTREAM_CONNECT_TIMEOUT_SECONDS`, which
    sat outside a section headed "Timeouts (seconds)" that a reader would
    reasonably take as complete.

    Derived from `model_fields` rather than a hardcoded list, so a new setting
    fails this in the commit that adds it. The alias is what is matched, because
    the alias is what an operator types: two fields are spelled `DAINTREE_*` in
    the environment and something shorter on the object.
    """
    assigned = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", _read(ENV_EXAMPLE), re.M))
    aliases = {
        str(field.alias or name.upper()) for name, field in Settings.model_fields.items()
    }

    undocumented = sorted(aliases - assigned)
    assert not undocumented, (
        f"{len(undocumented)} settings are absent from .env.example: "
        f"{', '.join(undocumented)}. Add each one to the section it belongs to — "
        "commented out, with its default, if it is not a knob a deployment turns."
    )

    # And the other direction, which is the failure that outlives a deletion: a
    # removed setting leaves a line an operator can still copy into `.env`, where
    # `extra='ignore'` swallows it in silence. There is no way to discover that
    # the value is doing nothing.
    stray = sorted(assigned - aliases)
    assert not stray, (
        f".env.example offers settings that no longer exist: {', '.join(stray)}. "
        "Pydantic ignores unknown keys, so a reader who copies one gets no error "
        "and no effect."
    )
