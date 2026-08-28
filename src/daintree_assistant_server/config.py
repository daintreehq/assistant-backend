"""Service configuration.

Settings are parsed once at startup into an immutable object. Secret values
use :class:`~pydantic.SecretStr` so they never appear in logs, tracebacks, or
``repr`` output. Required secrets default to empty strings here and are
validated by the readiness path (:meth:`Settings.missing_secrets`) so the
process fails *readiness* — not import — when a secret is absent.
"""

from __future__ import annotations

import os
from functools import lru_cache
from ipaddress import ip_address
from typing import ClassVar, Literal
from urllib.parse import unquote, urlsplit

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---- THE serving identity — one host, one model, both hardcoded -----------
#
# Baseten's Model APIs serve `zai-org/GLM-5.3-Flash`, and it serves everything:
# the main completion and every utility task. Baseten is a model HOST rather
# than a router, so one endpoint serves the weights and there is nothing to
# select between. Runbook selection makes no upstream call at all — it is the
# local ONNX classifier in `runbooks/local/`.
#
# This was briefly a two-mode registry (`modes.py`), with a `pro` tier on
# Cerebras. That is gone: Cerebras's pricing structure could not be made to work
# for this product, so there is no second route to choose between and a registry
# over one entry was a lie about the shape of the thing. Git history has it, and
# a genuine second tier would land there again rather than as a flag here.
#
# **The host is a latency decision and the model is a capability one, and this
# move changed BOTH at once** — which is the thing to remember when reading a
# benchmark taken before it. The previous identity was
# `deepseek-ai/DeepSeek-V4-Flash-0731` on Together, itself chosen for host
# latency (the 62 real utility-task payloads replayed at Together p50 1100ms /
# p90 1702ms against OpenRouter's 3414ms / 6013ms on the SAME weights, 3.1x).
# Every latency and cost figure recorded anywhere in this repo predating this
# change describes those weights on that host, so none of them carry over
# unre-measured. Git history has the whole of it.
EXECUTOR_MODEL = "zai-org/GLM-5.3-Flash"

#: The credential, base URL and production origin for that one host. Named as
#: constants rather than inlined so readiness messages, the doctor route and the
#: tests that pin the credential model all read the same three strings.
EXECUTOR_KEY_ENV = "BASETEN_API_KEY"
EXECUTOR_URL_ENV = "BASETEN_BASE_URL"
EXECUTOR_ORIGIN = "inference.baseten.co"

#: The alias returned to clients. The product promise is Daintree, not a
#: permanent contract with Baseten or Z.ai.
PUBLIC_MODEL_ID = "daintree-assistant"

#: The serving model's input context window, in tokens. Hardcoded beside the
#: model id for the same reason the model is: it is a property of the weights we
#: chose, not an operational dial.
#:
#: **The HOST publishes this one, and it was read from there** — Baseten's
#: `/v1/models` listing carries `context_length: 1048576` for this model, which
#: is strictly better evidence than the previous entry had (DeepSeek's published
#: figure for weights served elsewhere, with nothing here having asked). It is
#: still not a probe of what the endpoint will actually ACCEPT — `tests/live/`
#: is still empty (`TODO.md`) — so `services/context_budget.py` keeps refusing a
#: turn only when the INPUT alone clears the whole window rather than at the
#: reservation boundary: a ceiling we did not test against may bound a paid
#: turn, so it is allowed to bound only the ones no reading of it could rescue.
#:
#: Note this is an EIGHTFOLD increase over the 131,072 that stood here for
#: DeepSeek V4 Flash. Nothing in the budgeter scales with it, so the practical
#: effect is that `exceeds_window` becomes very nearly unreachable and the
#: upstream's own length rejection becomes the real limit — which is the same
#: code either way (`_CONTEXT_LENGTH_MARKERS`).
EXECUTOR_CONTEXT_TOKENS = 1_048_576

#: The `reasoning_effort` sent on every executor call.
#:
#: **This model cannot be run non-thinking**, which is new and inverts the rule
#: the rest of this file was written under. GLM-5.3-Flash reasons
#: unconditionally: Baseten lists it "always enabled", there is no `none`, and
#: an ABSENT field means `high`. So unlike DeepSeek — where the unified
#: `reasoning: {"enabled": false}` genuinely reached zero reasoning tokens —
#: there is no off switch to hold down here, only a floor to sit on.
#:
#: `low` is that floor, and it is close enough to off to be worth the move:
#: measured over 6 trials on one proof-and-verify prompt, `low` reasoned on 1 of
#: 6 (median 0, the one non-zero trial costing 16 tokens) against the absent
#: field's 6 of 6 (median 479.5, spread 161-2519). Passed to the adapter at
#: construction (`main.py`) rather than read by it, so the product decision
#: stays here beside the model id and the adapter keeps its one job.
EXECUTOR_REASONING_EFFORT = "low"

#: Completion-token allowance for the reasoning the serving model emits ANYWAY.
#:
#: Not a thinking budget — `thinking_headroom_tokens` (4,000) is that, and it is
#: still added only when the interactive posture is deliberately switched on.
#: This is the much smaller correction for a fact that has no off switch: reasoning
#: tokens are generated tokens and share `max_tokens` with the visible answer, so
#: on this model a profile's cap is no longer entirely the profile's to spend.
#:
#: It exists because the tightest utility profiles cap output at 300 tokens. Before
#: the move, a non-thinking task genuinely got all 300; now any reasoning comes out
#: of them, and a JSON task truncated at `finish_reason=length` burns its one
#: repair retry on the same cap and can fail twice. Adding this back to the cap
#: restores the visible budget the profile intended rather than enlarging it.
#:
#: 64 rather than something rounder: the most reasoning observed at the `low` floor
#: across every probe was 16 tokens, so this is ~4x the measured worst case while
#: adding only a fifth to the smallest cap. Raise it if a `length` truncation is
#: ever actually seen on a utility task; do not raise it speculatively, because
#: every token here IS also available to visible output.
EXECUTOR_REASONING_ALLOWANCE_TOKENS = 64

# ---------------------------------------------------------------------------
# The FAST-RESPONSE role. A second internal provider, NOT a second product.
# ---------------------------------------------------------------------------
#
# A Cerebras/Gemma product TIER lived here once — `modes.py`, per-mode
# credentials, a per-turn route on the wire — and it was deleted when its pricing
# could not be made to work (CLAUDE.md). This is not that. There is no registry,
# no caller-facing choice and no second public model: a request still targets
# `daintree-assistant`, and the backend composes two internal calls behind it.
#
# Hardcoded for the same reason the executor model is: which model writes the
# preamble is a product decision that has one right answer, not an operational
# dial. What IS operational — whether the stage runs at all, and for how many
# sessions — lives in `Settings` below, because the right value for that is
# whatever production says this week.
FAST_RESPONSE_MODEL = "gpt-oss-120b"
#: Reasoning effort for the preamble call.
#:
#: `gpt-oss-120b` is a reasoning model and Cerebras defaults it to `medium`, so —
#: exactly like the executor's `reasoning: {"enabled": false}` — omission is not
#: neutral here, it BUYS thinking. Reasoning tokens are generated tokens: they
#: count against `max_completion_tokens` and against the clock, on the one call
#: whose whole value is arriving before the executor does. `low` is the floor
#: Cerebras exposes; there is no "off".
FAST_RESPONSE_REASONING_EFFORT = "low"
#: Whether the fast-response stage runs at all. **It does not.**
#:
#: Hardcoded here rather than exposed as an env var for the same reason the model
#: ids are: which model writes a preamble, and whether one is written, are product
#: decisions with one right answer. The stage USED to carry `off`/`shadow`/
#: `visible` modes and a rollout percentage; those were deleted once it shipped,
#: and reintroducing them as a `.env` switch is exactly what that deletion was
#: rejecting. Turning the stage off is a code change, which is the honest
#: description of what it is.
#:
#: **Why off.** The preamble buys a visible line while the reader waits, and it is
#: worth precisely as much as the wait is long. On the previous executor that gap
#: was seconds. On `zai-org/GLM-5.3-Flash` via Baseten the executor's first bytes
#: arrive fast enough that a line announcing work which has already started reads
#: as noise, and it delays the real answer to say something that stops being true
#: almost at once. The stage's own design already conceded the principle —
#: `PreambleGate` throws away a line that loses the race to the executor's first
#: visible byte. Setting this False stops paying to enter the race.
#:
#: Nothing is torn out: `_build_fast_response_provider` returns None,
#: `FastResponseService.available` reads False off that, and every eligibility
#: check downstream already handles it (tests and benchmarks have always taken
#: that path). Flip this to True to restore the stage.
#:
#: Note `CEREBRAS_API_KEY` is consequently NO LONGER REQUIRED — see
#: `missing_secrets()`, whose entry for it existed only because the stage could
#: not be switched off.
FAST_RESPONSE_ENABLED = False

FAST_RESPONSE_KEY_ENV = "CEREBRAS_API_KEY"
FAST_RESPONSE_URL_ENV = "CEREBRAS_BASE_URL"
FAST_RESPONSE_ORIGIN = "api.cerebras.ai"
#: The host name, for traces and metrics. Never reaches a client.
FAST_RESPONSE_PROVIDER = "cerebras"
EXECUTOR_PROVIDER = "baseten"

#: Why this model and not the 31B Gemma that used to sit here: the stage no
#: longer writes a considered opening the executor continues from. It writes one
#: short line, from one message and at most three runbooks, and it may decline to
#: write anything — a job whose quality bar is "is this worth saying" rather than
#: "is this well argued". What it needs instead is to LAND FIRST, because the two
#: calls now go out together and a preamble that arrives after the executor's
#: first visible byte is discarded unshown. `gpt-oss-120b` on Cerebras is the
#: fastest thing available that can still make the judgement.

__all__ = [
    "EXECUTOR_CONTEXT_TOKENS",
    "EXECUTOR_MODEL",
    "EXECUTOR_REASONING_ALLOWANCE_TOKENS",
    "EXECUTOR_REASONING_EFFORT",
    "FAST_RESPONSE_ENABLED",
    "FAST_RESPONSE_MODEL",
    "FAST_RESPONSE_REASONING_EFFORT",
    "PUBLIC_MODEL_ID",
    "Settings",
    "get_settings",
]

# The runbook loaded whenever a turn produces a `docs_query` — i.e. whenever the
# server is about to search the documentation MCP. SERVER-PINNED, not classified:
# by the time this matters the selector has ALREADY answered "is this a
# documentation question", so routing the matching runbook through a SECOND
# judgement over the same evidence is a coin flip about a fact already in hand.
# The same argument as foundation pairing, one step earlier.
#
# Because it is pinned, it is also HIDDEN FROM THE SELECTOR (`Catalog.selector_metadata`
# omits it): a runbook the server always supplies is one the classifier should never
# spend prompt bytes reading or risk mis-picking on an operational turn. That
# hiding is what keeps the two decisions from disagreeing.
#
# An id the catalog does not contain is a silent no-op — `resolve_active_runbooks`
# filters unknown ids — so deleting the runbook file degrades to the previous
# behaviour rather than breaking the turn.
DOCS_RUNBOOK_ID = "daintree.docs.answer-usage-question"

#: The score a NEWLY-LOADED runbook must reach before its ``opener`` is shown.
#:
#: Hardcoded beside the model ids, and for the same reason: whether the product
#: announces itself is a product decision with one right answer, not an
#: operational dial. See ``runbooks/openers.py`` for what the gate does with it.
#:
#: **0.90 is chosen from a measurement, and the measurement's main finding is
#: that this knob is weak.** Run over the 177-case routing corpus (173 turns
#: reach the classifier), scoring what the gate would actually announce — the
#: top-ranked newly-loaded non-foundation runbook:
#:
#: ===========  =========  =======  =====  ================
#: threshold    announces  correct  wrong  correct silenced
#: ===========  =========  =======  =====  ================
#: 0.00         135        118      16     0
#: 0.90         134        118      15     0
#: 0.99         127        116      10     3
#: ===========  =========  =======  =====  ================
#:
#: The head is saturated: the median score is 1.000 for runbooks the case
#: REQUIRED and 0.964 for ones it did not, so no floor separates them cleanly.
#: Everything above 0.90 buys a wrong announcement by spending a right one, which
#: is a bad trade for a cosmetic line. 0.90 is therefore the last value that costs
#: nothing — well clear of the 0.65 admission floor, so a runbook the selector
#: merely tolerated stays quiet, while the confident majority still speaks.
#:
#: What actually contains the residual ~11% is the OPENER TEXT, not this number.
#: An opener is required to be generic enough to remain true of any turn its
#: runbook plausibly answers (``RunbookMeta.opener``), so a misroute produces a
#: vague-but-honest line rather than a confident wrong one. Raising this constant
#: is the wrong response to a bad opener; rewriting the opener is the right one.
#:
#: Re-measure with ``scratch``-style access to ``benchmarks.runbook_selection``
#: after a selector retrain — a recalibrated head moves this table, and the
#: corpus is the only place the shape is visible.
RUNBOOK_OPENER_THRESHOLD = 0.90

# Thinking posture — hardcoded like the model ids, deliberately NOT env-config.
# There is one optimum and the server ships it.
#
# **THIS FLAG NO LONGER MEANS "NO REASONING HAPPENS", AND THAT CHANGED WITH THE
# HOST.** It used to be literally true: DeepSeek V4 Flash reasons whenever the
# field is absent, so every call sent `reasoning: {"enabled": false}`, which
# Together honoured — measured, `reasoning_tokens` left the usage block
# entirely. GLM-5.3-Flash has no such switch (see EXECUTOR_REASONING_EFFORT
# above): it reasons unconditionally, and the cheapest posture available is the
# `low` floor rather than off.
#
# So what `False` means HERE is now "ask this host for the least reasoning it
# will do", and `providers/baseten.py` is what translates it — a request
# carrying `thinking_disabled=True` is sent at the floor, not with the field
# omitted, because omission on this host buys `high`. In practice the floor is
# very close to the old posture (1 of 6 trials reasoned at all, median 0
# tokens), which is why the flag keeps its name and its value rather than being
# renamed for a difference that does not show up in a usage block.
#
# What it does NOT do any more is guarantee a zero. `/doctor` and
# `/v1/daintree/capabilities` publish it as `thinking_enabled: false`, and that
# now reads as a statement about what the server REQUESTS rather than about what
# the model does. Worth revisiting if a caller ever gates on it.
#
# Runbook learning used to be the one deliberate exception — it ran
# fire-and-forget after the response, so its latency never reached a user. It
# has been removed.
INTERACTIVE_THINKING_ENABLED = False
# The tier the interactive surface would send if that posture were ever flipped
# back on — i.e. if the server ever wanted MORE than the floor. Kept beside the
# flag rather than inlined at the one call site that reads it
# (services/task_runner.py) so the pair stays legible as a single decision — and
# so flipping the flag does not also require choosing a tier. On this host the
# accepted values are `low` | `high` | `max`; `high` is also what an omitted
# field would have bought.
INTERACTIVE_REASONING_EFFORT = "high"

# The development default salt; rejected in production by weak_secrets().
DEFAULT_STATE_SALT = "daintree-state-salt"



def _origin_problem(*, value: str, expected_host: str, env_name: str) -> str | None:
    """``None`` when ``value`` is exactly ``https://<expected_host>``.

    Extracted from the single-provider version so both providers get the SAME
    check rather than a copy each. The strictness is deliberate on every clause:
    a non-443 port is a different origin; userinfo (``https://x@inference.baseten.co``)
    still reaches the host but credentials do not belong in this URL in any form;
    and a malformed port must fail readiness rather than the first request.
    """
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == expected_host
            and parsed.port in (None, 443)
            and parsed.username is None
        )
    except ValueError:  # e.g. a non-numeric port
        valid = False
    if valid:
        return None
    return (
        f"{env_name} must be the https://{expected_host} origin in production "
        "(a server API key is sent to it)"
    )


#: Characters that must never appear in a URL this server publishes. `urlsplit`
#: SILENTLY NORMALISES them away — it strips leading/trailing spaces and C0
#: controls and deletes embedded tab/CR/LF — so a value containing any of them
#: validates as the clean URL and is then published RAW. The client receives
#: something it will parse differently or reject outright, and every trace of why
#: was erased before the check ran. Backslash is here for the same reason from the
#: other direction: `urlsplit` does not treat it as an authority separator and
#: browser-lineage parsers do, so `https://host\\evil/` is two different URLs
#: depending on who reads it.
_URL_FORBIDDEN = frozenset(chr(c) for c in range(0x21)) | {"\x7f", "\\"}

#: Hostnames that name the reader's own machine. Refused in production/staging on
#: any scheme: a manifest pointing every client at its own loopback interface is
#: not a deployment, and `https://` does not make it one. Names belong here — a
#: production issuer called `localhost` is wrong however it resolves.
_LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_loopback_literal(host: str) -> bool:
    """Whether ``host`` is a LITERAL loopback IP address.

    Names are deliberately refused, ``localhost`` included, and the asymmetry with
    :data:`_LOCAL_HOSTNAMES` above is the point. That set answers "is this a
    machine-local name?", which is the right question for refusing one in
    production. This function answers "is plaintext safe here?", and only an IP
    literal establishes that: ``evil.localhost`` resolves through DNS or
    ``/etc/hosts`` to wherever someone points it, so a name-based exemption would
    let a plaintext token exchange — carrying the authorization code AND the PKCE
    verifier — travel to a remote host.

    A trailing dot is NOT stripped, for the same reason: ``127.0.0.1.`` is not
    IP-literal syntax, it is a fully-qualified DNS name that merely looks like
    one, and it is resolved as a name.

    The client applies exactly this rule (`isLoopbackHost` in
    `internal/auth/discovery.go`), so accepting a name here would mean publishing
    a manifest it refuses.
    """
    try:
        return ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _origin_of(value: str) -> tuple[str, str, int] | None:
    """``(scheme, host, port)`` with the default port made explicit, or ``None``.

    Defaulting the port is what makes the comparison correct rather than merely
    convenient: ``https://x.supabase.co`` and ``https://x.supabase.co:443`` are
    the same origin, and a same-origin check that compared ``None`` against
    ``443`` would reject a perfectly good configuration. A scheme with no default
    port yields ``None`` rather than a sentinel, because a fabricated port would
    make two unrelated origins compare equal.
    """
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:  # a non-numeric or out-of-range port
        return None
    if not parsed.scheme or not parsed.hostname:
        return None
    if port is None:
        default = {"https": 443, "http": 80}.get(parsed.scheme)
        if default is None:
            return None
        port = default
    return (parsed.scheme, parsed.hostname, port)


#: Bounds one published URL. The client caps the WHOLE manifest body, so an
#: unbounded value here is not merely untidy — enough of it makes every field
#: unreadable at once, and the failure names the size rather than the field that
#: caused it.
_MAX_AUTH_URL_LENGTH = 2048


def _url_shape_problem(
    *, value: str, env_name: str, allow_fragment: bool = False
) -> str | None:
    """The checks every published URL gets, whatever it is for.

    Deliberately runs BEFORE parsing, because parsing is what destroys the
    evidence. Nothing here echoes ``value``: these strings reach the startup log
    and, outside production, a readiness response, and the one input most likely
    to be malformed is one with a credential accidentally pasted into it.

    ``allow_fragment`` is the one rule that varies by role. A fragment is
    meaningless on an issuer or a token endpoint and suspicious on a callback, but
    an ordinary account page may legitimately be a single-page app whose route
    lives in one — and the client permits it there.
    """
    if len(value) > _MAX_AUTH_URL_LENGTH:
        return f"{env_name} is longer than {_MAX_AUTH_URL_LENGTH} characters"
    if any(ch in _URL_FORBIDDEN for ch in value):
        return f"{env_name} contains whitespace or a control character"
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # A lone surrogate, which env can carry in through surrogateescape. It
        # survives every check below and then raises while the response is being
        # serialized — turning a configuration mistake into a 500 on a public
        # endpoint.
        return f"{env_name} is not valid UTF-8"
    origin = _origin_of(value)
    if origin is None:
        return f"{env_name} is not a usable absolute URL (check the scheme, host and port)"
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        return f"{env_name} must not embed credentials in the URL"
    if parsed.fragment and not allow_fragment:
        return f"{env_name} must not carry a URL fragment"
    if not 1 <= origin[2] <= 65535:
        return f"{env_name} names an unusable port"
    return None


def _auth_url_problem(
    *, value: str, env_name: str, hardened: bool, allow_fragment: bool = False
) -> str | None:
    """``None`` when ``value`` is a URL this server may publish to a client.

    HTTPS is required because everything named by these URLs carries or mints a
    credential, and because the CLI is instructed to REFUSE a manifest whose
    endpoints are not HTTPS — publishing one would be handing every client a
    configuration it must throw away. The loopback exemption exists so a local
    Supabase (``http://127.0.0.1:54321/auth/v1``) is developable.

    Both relaxations are withdrawn together in production/staging, and the SECOND
    one is the less obvious: a real deployment's manifest is read by clients on
    other machines, so a host naming the READER's own machine — or a private or
    link-local address only reachable from inside one network — sends each of them
    somewhere different, and none of those places is us. ``https://`` does not
    redeem it, which is why the check is on the host rather than on the scheme.
    """
    problem = _url_shape_problem(
        value=value, env_name=env_name, allow_fragment=allow_fragment
    )
    if problem:
        return problem
    origin = _origin_of(value)
    assert origin is not None  # _url_shape_problem returned None, so it parsed
    scheme, host, _ = origin
    if hardened:
        if host.lower() in _LOCAL_HOSTNAMES or _is_private_host(host):
            return (
                f"{env_name} must name a publicly reachable host in production "
                "(a loopback or private address points every client at its own network)"
            )
        if scheme != "https":
            return f"{env_name} must be an https:// URL"
        return None
    if scheme == "https":
        return None
    if scheme == "http" and _is_loopback_literal(host):
        return None
    return (
        f"{env_name} must be an https:// URL (http:// is allowed only on a literal "
        "loopback address such as 127.0.0.1, and only outside production — a NAME "
        "like localhost resolves wherever it is pointed, so it does not establish "
        "that there is no network to intercept)"
    )


def _is_private_host(host: str) -> bool:
    """Whether ``host`` is an IP literal that is not routable on the public internet.

    Only IP LITERALS. A DNS name is not resolved: resolution at startup would make
    readiness depend on the network, and the answer could change afterwards anyway.
    """
    try:
        address = ip_address(host)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    )


#: The only identity providers whose tokens this product accepts, and the only
#: sites its links may open. Pinned rather than configured, and the reason is the
#: same as certificate pinning: discovery may supply the project, the client id,
#: the endpoints and the paths, but it must not be able to choose which company
#: issues a user's tokens or which site is asked for their card details.
#:
#: The CLI pins exactly these two lists (`internal/auth/discovery.go`), and
#: refuses a manifest naming anything else. Duplicating them here is not
#: redundancy — it is what turns "the client will reject this" into "this server
#: never published it", which is the difference between a login that fails
#: confusingly at a user's machine and a deploy that fails at readiness.
_ALLOWED_ISSUER_SUFFIXES = ("supabase.co", "daintree.org")
_ALLOWED_LINK_SUFFIXES = ("daintree.org",)

#: The one route on the website that this server may forward a caller's bearer to,
#: written out in full because a HOST is not a destination.
#:
#: `_ALLOWED_LINK_SUFFIXES` pins the origin, and for an account LINK — a page a
#: user opens in their own browser, already carrying their own session — that is
#: the whole question. The entitlement call is not that. It attaches the CALLER's
#: Supabase access token to a request this server originates, so every path on
#: every `daintree.org` subdomain is a place that credential can be delivered:
#: a form handler, a logging endpoint, a preview deployment, a route somebody adds
#: next month. Origin-only pinning answers "is it us" and leaves "is it the route
#: that is supposed to receive this" unasked.
#:
#: Exactly one spelling, and the trailing-slash variant is REFUSED rather than
#: normalised. Rewriting it would mean this server silently changing a
#: security-relevant destination on an operator's behalf — and the two spellings
#: are genuinely different requests to the far end, so the only honest answer to
#: an ambiguous one is to make somebody write down which they meant.
_ENTITLEMENT_PATH = "/api/assistant/entitlement"

#: The website ORIGINS a hardened deployment may forward a caller's bearer to,
#: keyed by ``ENVIRONMENT``.
#:
#: `_ALLOWED_LINK_SUFFIXES` answers "is it us" and `_ENTITLEMENT_PATH` answers
#: "is it the route". Together they still leave every `daintree.org` subdomain a
#: place a caller's Supabase access token can be delivered, and the set of those
#: is not fixed: a preview deployment, a marketing host, a service somebody
#: stands up next month. A suffix rule cannot tell any of them from the website,
#: because being a subdomain is the only thing it knows how to check.
#:
#: So in the environments where that matters the origin is ENUMERATED rather than
#: pattern-matched. The port rides along for free: these are full origins with
#: the default made explicit, so `https://staging.daintree.org:8443/...` is a
#: different origin and is refused without needing a rule of its own.
#:
#: Keyed on ``ENVIRONMENT`` and never inferred from the hostname. Deriving the
#: environment from the value being validated would let the value choose which
#: rules it is judged by, which is not a check.
#:
#: `development` and `test` are deliberately ABSENT rather than empty, and the
#: difference is load-bearing: absent means "fall through to the suffix rule and
#: the loopback carve-out", which is what makes a locally-run website
#: developable. An empty tuple would mean "no origin is approved" and refuse
#: everything.
#:
#: **The production entries are the apex and `www`, and they have not been
#: confirmed against a deployed production website.** Production ships with no
#: entitlement URL at all today, so nothing currently rests on them; the failure
#: mode if they are wrong is a readiness refusal naming the approved set, at the
#: moment somebody first configures production, which is loud and one line to
#: fix. That is the right way round — the alternative is a suffix rule that
#: accepts whatever is typed.
_APPROVED_ENTITLEMENT_ORIGINS: dict[str, tuple[str, ...]] = {
    "staging": ("https://staging.daintree.org",),
    "production": ("https://daintree.org", "https://www.daintree.org"),
}

#: An OAuth client id goes into a URL query string and, on the client, into a
#: credential-store account key. Both bounds mirror the CLI's.
_CLIENT_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.~"
)
_MAX_CLIENT_ID_LENGTH = 200


# ---- The private-staging subject gate -------------------------------------
#
# `AUTH_MODE=enforce` proves a token is genuine. It does not prove that whoever
# holds it was ever meant to be here: public signup on the website is deliberate,
# so under enforcement alone every verified account of the one allowlisted OAuth
# client reaches the entitlement lookup and, past it, work funded by the server's
# own key. That is the correct posture for the product and the wrong posture for
# a private staging deployment, whose whole premise is that the operator names
# the people who may in.
#
# So `STAGING_ALLOWED_SUBJECT_DIGESTS` carries an explicit membership list, one
# full SHA-256 digest of a canonical subject per line. Digests rather than
# subjects because the value lives in Secret Manager, in a deploy substitution
# and in an operator's clipboard, and none of those is a place to keep a real
# account identifier — a digest names an account to the gate and to nothing else.
#
# The bounds below exist so that a value which is nearly right is REFUSED rather
# than silently reinterpreted. An allowlist that quietly drops the line it could
# not parse is an allowlist that locks out the person it was edited to admit, and
# nothing about the deployment would look wrong.
_SUBJECT_DIGEST_LENGTH = 64
_SUBJECT_DIGEST_CHARS = frozenset("0123456789abcdef")

#: 1 is a real deployment (one operator); 64 is far past any private staging
#: cohort. Crossing it means the wrong value was pasted, not a busy beta.
_MAX_SUBJECT_GATE_ENTRIES = 64

#: The largest legal list, exactly: 64 entries of 64 characters, each followed by
#: its LF. Checked against the RAW BYTES and BEFORE decoding, which is the whole
#: reason it is a byte count rather than a character count — a multi-megabyte
#: value should be refused by its size, not decoded first so that its size can be
#: measured in characters.
_MAX_SUBJECT_GATE_BYTES = _MAX_SUBJECT_GATE_ENTRIES * (_SUBJECT_DIGEST_LENGTH + 1)

#: The membership label. It is not a version of anything (see CLAUDE.md) — it is
#: a name for WHICH list is loaded, so an operator reading `/version` can tell
#: whether the revision in front of them has the membership they just published.
#:
#: It is echoed on a public unauthenticated endpoint, so the charset is narrow —
#: but be precise about what that buys. It is SYNTACTIC safety: no whitespace, no
#: markup, no control characters, nothing that changes the shape of a log line or
#: a JSON body. It is NOT semantic: 32 lowercase-hex characters are a legal label
#: and also the first half of a digest, and a de-hyphenated UUID fits too. That
#: the value carries no subject material is an operator contract, stated where an
#: operator reads it (`.env.example`), and no validator here can enforce it.
_MAX_GATE_REVISION_LENGTH = 32
_GATE_REVISION_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._-")
_GATE_REVISION_LEADING_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


def parse_subject_digests(value: str) -> tuple[frozenset[str], str | None]:
    """The gate's membership list, or the one sentence explaining why there is none.

    Returns ``(entries, problem)``; a problem always comes with an EMPTY set, so
    every caller that ignores the problem still fails closed. That pairing is
    deliberate rather than tidy — readiness reads the problem and refuses to come
    up, while the door reads only the set, and the door must not be the place
    where a parse failure is first noticed.

    The byte ceiling is applied to the bytes the environment actually carried,
    before any of them are decoded. ``os.environ`` has already decoded once, under
    the filesystem encoding and ``surrogateescape``; ``os.fsencode`` is that
    decode's exact inverse, so it recovers the original bytes whatever the locale
    is — including the ones that were never valid UTF-8, which then fail the
    strict decode below rather than surviving as lone surrogates all the way into
    a digest comparison. Hardcoding UTF-8 for the round trip would be right only
    where the filesystem encoding already is UTF-8, which is true of the runtime
    image and is not a property worth depending on silently.

    Nothing here is trimmed, normalised or lowercased. A digest is a lookup key:
    accepting two spellings of one entry means the gate's membership depends on
    which spelling the operator happened to paste, and repairing the input is how
    a value that was validated stops being the value that is used.
    """
    if not value:
        return frozenset(), (
            "STAGING_ALLOWED_SUBJECT_DIGESTS is empty (a gate with no members "
            "would refuse every account, including the operator's)"
        )
    try:
        raw = os.fsencode(value)
    except UnicodeEncodeError:
        return frozenset(), "STAGING_ALLOWED_SUBJECT_DIGESTS is not valid UTF-8"
    if len(raw) > _MAX_SUBJECT_GATE_BYTES:
        return frozenset(), (
            f"STAGING_ALLOWED_SUBJECT_DIGESTS is {len(raw)} bytes, which is past the "
            f"largest legal list ({_MAX_SUBJECT_GATE_BYTES} bytes: "
            f"{_MAX_SUBJECT_GATE_ENTRIES} entries and their separators)"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return frozenset(), "STAGING_ALLOWED_SUBJECT_DIGESTS is not valid UTF-8"

    if text.startswith("\ufeff"):
        # A BOM is what a Windows editor writes and what a `gcloud` paste keeps.
        # It would otherwise be a stray character on the FIRST entry only, so the
        # gate would admit everyone but the person at the top of the list.
        return frozenset(), (
            "STAGING_ALLOWED_SUBJECT_DIGESTS starts with a byte-order mark "
            "(save it as UTF-8 without a BOM)"
        )
    if "\r" in text:
        return frozenset(), (
            "STAGING_ALLOWED_SUBJECT_DIGESTS contains a carriage return "
            "(entries are separated by a single LF, never CR or CRLF)"
        )

    # Exactly one optional terminal LF. A second one is a blank final entry and is
    # reported as such below, rather than absorbed.
    body = text[:-1] if text.endswith("\n") else text
    entries = body.split("\n")

    # Before any per-entry work, and deliberately not folded into the byte
    # ceiling: the two bounds catch different mistakes. A list of 65 full digests
    # is over the byte ceiling anyway, but a list of 5,000 short lines is small on
    # the wire and is still not an allowlist anybody wrote by hand.
    if len(entries) > _MAX_SUBJECT_GATE_ENTRIES:
        return frozenset(), (
            f"STAGING_ALLOWED_SUBJECT_DIGESTS has {len(entries)} entries, past the "
            f"maximum of {_MAX_SUBJECT_GATE_ENTRIES}"
        )

    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        if not entry:
            return frozenset(), (
                f"STAGING_ALLOWED_SUBJECT_DIGESTS line {index} is blank "
                "(every line must be one digest)"
            )
        if entry != entry.strip() or any(character.isspace() for character in entry):
            return frozenset(), (
                f"STAGING_ALLOWED_SUBJECT_DIGESTS line {index} contains whitespace "
                "(a digest is 64 hex characters and nothing else)"
            )
        if len(entry) != _SUBJECT_DIGEST_LENGTH:
            return frozenset(), (
                f"STAGING_ALLOWED_SUBJECT_DIGESTS line {index} is {len(entry)} "
                f"characters, not {_SUBJECT_DIGEST_LENGTH}"
            )
        if not set(entry) <= _SUBJECT_DIGEST_CHARS:
            lowered = entry.lower()
            if set(lowered) <= _SUBJECT_DIGEST_CHARS:
                # Its own message because it is the likely paste, and because
                # lowercasing it here would be the exact repair this parser must
                # not make — see the docstring.
                return frozenset(), (
                    f"STAGING_ALLOWED_SUBJECT_DIGESTS line {index} is uppercase "
                    "(digests are lowercase hexadecimal)"
                )
            return frozenset(), (
                f"STAGING_ALLOWED_SUBJECT_DIGESTS line {index} is not hexadecimal"
            )
        if entry in seen:
            # Not harmless: a duplicate means the list an operator counted is not
            # the list the gate holds, and the count is the only thing anyone can
            # see about it.
            return frozenset(), (
                f"STAGING_ALLOWED_SUBJECT_DIGESTS line {index} repeats an earlier entry"
            )
        seen.add(entry)

    return frozenset(seen), None


def _gate_revision_problem(value: str) -> str | None:
    """Everything wrong with the membership label itself.

    Rejects rather than trims, for the reason ``_client_id_problems`` does: this
    string is published verbatim on ``/version``, so validating one value and
    publishing another is how a space reaches an operator's diff.
    """
    if value != value.strip():
        return "STAGING_SUBJECT_GATE_REVISION must not have leading or trailing whitespace"
    if not value or len(value) > _MAX_GATE_REVISION_LENGTH:
        return (
            f"STAGING_SUBJECT_GATE_REVISION must be 1-{_MAX_GATE_REVISION_LENGTH} "
            "characters"
        )
    if value[0] not in _GATE_REVISION_LEADING_CHARS:
        return "STAGING_SUBJECT_GATE_REVISION must start with a lowercase letter or a digit"
    if not set(value) <= _GATE_REVISION_CHARS:
        return (
            "STAGING_SUBJECT_GATE_REVISION may hold only lowercase letters, digits, "
            "and . _ -"
        )
    return None


def _host_allowed(host: str, suffixes: tuple[str, ...]) -> bool:
    """Whether ``host`` is one of ``suffixes`` or a subdomain of one.

    A trailing dot is stripped first: ``daintree.org.`` and ``daintree.org`` are
    the same name to DNS, and only one of them matches a plain comparison.
    """
    # One trailing dot, not all of them: the client removes a single suffix, so
    # `proj.supabase.co..` matches here and is refused there.
    cleaned = host.lower().strip("[]")
    cleaned = cleaned[:-1] if cleaned.endswith(".") else cleaned
    return any(cleaned == suffix or cleaned.endswith("." + suffix) for suffix in suffixes)


def _normalised_origin(origin: tuple[str, str, int] | None) -> tuple[str, str, int] | None:
    """``origin`` with one trailing dot removed from the host.

    `urlsplit` already lowercases a hostname, so case needs nothing here. The dot
    does: it is the root label, `example.org.` and `example.org` resolve
    identically, and an enumerated origin comparison that refused the dotted
    spelling would be stricter than the resolver — and stricter than
    :func:`_host_allowed`, which strips it for exactly this reason and which this
    comparison replaces for hardened environments.
    """
    if origin is None:
        return None
    scheme, host, port = origin
    return (scheme, host[:-1] if host.endswith(".") else host, port)


def _is_under_path(child: str, parent: str) -> bool:
    """Whether ``child``'s path is contained by ``parent``'s, on a segment boundary.

    A prefix test is not containment, and it fails in two ways that both matter:

    * ``/auth/v1malicious/token`` has ``/auth/v1`` as a prefix and is a different
      route, which a sibling handler on the same host can own.
    * ``/auth/v1/../capture`` passes a prefix test and resolves, in any server or
      proxy that normalises dot segments, to ``/capture`` — outside the issuer
      entirely.

    Dot segments are refused rather than normalised: normalising would mean
    betting that our interpretation matches the far end's, and a real OAuth
    endpoint never contains one.

    **Both paths are percent-decoded before the comparison**, and that single call
    is what makes the dot-segment rule mean anything. ``urlsplit`` leaves
    ``.path`` encoded, so ``/auth/v1/%2e%2e/capture`` splits into the innocuous
    segments ``auth``, ``v1``, ``%2e%2e``, ``capture`` — no ``..`` in sight — and
    sails through, while every server and proxy downstream decodes it to
    ``/auth/v1/../capture`` and resolves it to ``/capture``, outside the issuer
    entirely. (Go's ``url.Parse`` decodes on the way in, which is why the client
    catches this one for free and this side had to be told.) Decoding also splits
    an encoded slash into a real segment boundary, which can only make the check
    see more segments and therefore only make it stricter.
    """
    child_path = unquote(child or "/")
    parent_path = unquote(parent or "/").rstrip("/")
    if any(segment in (".", "..") for segment in child_path.split("/")):
        return False
    if not parent_path:
        return True
    return child_path == parent_path or child_path.startswith(parent_path + "/")


def _loopback_redirect_problem(*, value: str, env_name: str) -> str | None:
    """``None`` when ``value`` is the CLI's fixed IPv4-loopback OAuth callback.

    It gets the same shape checks every other published URL gets — this value is
    served to anyone who asks ``/v1/daintree/auth/config``, so a credential or a
    control character in it is exactly as bad here as in the issuer — plus three
    refusals of its own, each closing a real hole rather than tidying the string:

    * **Not loopback.** A redirect URI naming any other host sends the
      authorization code somewhere off the machine that asked for it.
    * **``localhost`` rather than ``127.0.0.1``.** It resolves through the host's
      own name lookup, so a ``/etc/hosts`` entry or an IPv6-first resolver can
      point it at a listener that is not the CLI's. The master spec binds IPv4
      loopback for exactly this reason, and a name is not a binding.
    * **No explicit port.** Supabase matches redirect URIs by exact string, so a
      port the client picks at runtime can never match a registered URI. A missing
      one here means the value was written for a client that chooses its own,
      which cannot work.
    """
    problem = _url_shape_problem(value=value, env_name=env_name)
    if problem:
        return problem
    parsed = urlsplit(value)
    origin = _origin_of(value)
    assert origin is not None  # _url_shape_problem returned None, so it parsed
    scheme, host, _ = origin
    if scheme != "http" or host != "127.0.0.1":
        return (
            f"{env_name} must be an http://127.0.0.1:<port>/<path> loopback callback "
            "(not localhost, which resolves through the host's name lookup, and not "
            "a non-loopback address, which would send the authorization code off-box)"
        )
    if parsed.port is None:
        return f"{env_name} must name an explicit port (Supabase matches redirect URIs exactly)"
    if not parsed.path or parsed.path == "/":
        return f"{env_name} must name a callback path"
    return None


#: The three tool-projection postures. Declared HERE rather than beside the
#: projector because config sits below runbooks in the import graph — the
#: catalog reads DOCS_RUNBOOK_ID from this module, so the arrow cannot reverse.
ToolProjectionMode = Literal["disabled", "shadow", "enforced"]

#: Training-trace capture. Closed for the same reason ``ToolProjectionMode`` is:
#: every gate below keys on this value, so a typo in a free-form string ("full",
#: "internal") would skip the content gate — failing OPEN, in the one direction
#: that matters when the subject is recording people's content.
TraceCaptureMode = Literal["off", "metadata", "full_internal"]
TraceSinkKind = Literal["null", "filesystem", "gcs"]

#: The account-authentication posture. An operational ROLLOUT switch, closed for
#: the same reason every mode above is: each gate keys on this value, so a
#: free-form typo ("enforced", "Observe") would fall through to the permissive
#: branch — failing OPEN on the one setting whose whole job is to stop doing that.
#:
#: It is NOT one of the product decisions CLAUDE.md says must be hardcoded. Which
#: model answers a turn has one right answer we have benchmarked; whether today's
#: fleet is checking tokens is a question only production can answer, and the
#: answer changes during a rollout. The precedent is `TOOL_PROJECTION_MODE` — a
#: switch over behaviour that WITHHOLDS something from a caller, kept operational
#: precisely so "turn it off" costs a config-only revision rather than a deploy.
#:
#: open      the Authorization header is not read at all. Every request is served
#:           anonymously and the server funds it — today's shipped posture, and
#:           the default, so a deployment that sets nothing does not move.
#: observe   a token that IS present is parsed and verified, and the outcome is
#:           recorded. Authorization is UNCHANGED: an observed identity grants
#:           nothing, denies nothing, and a request with no token or a bad one is
#:           served exactly as `open` would serve it. Evidence, not enforcement.
#: enforce   a verified token is required; failures are the account 4xx family.
AuthMode = Literal["open", "observe", "enforce"]

#: What a verified caller is ENTITLED to, which is a different question from who
#: they are — and staged separately for exactly that reason. Identity says the
#: token is genuine; entitlement says the account behind it has paid. Merging the
#: two rollouts would mean a billing outage presenting as a signed-out user.
#:
#: off       paid routes never ask. Today's behaviour, and the default, so a
#:           deployment that sets nothing does not move.
#: observe   paid routes resolve the verdict and log it, and NEVER refuse on it.
#:           The mirror of `AUTH_MODE=observe`: evidence against real traffic
#:           before anyone's turn depends on the answer. It is not free — a paid
#:           request costs one lookup against the website — so watch latency as
#:           well as the verdict counts.
#: enforce   `access: granted` is required. A known missing or inactive plan is
#:           the typed 402 family; an answer we could not obtain is the typed 503,
#:           never a guess.
#:
#: `observe` and `enforce` both require `AUTH_MODE=enforce`. There is nobody to
#: ask about under the open door — the anonymous principal has no subject — so an
#: entitlement mode above an unenforced identity would resolve nothing on every
#: request and report that as evidence.
EntitlementMode = Literal["off", "observe", "enforce"]

#: The OAuth callback, and **not** deployment configuration despite looking like
#: it. The client compiles this exact string in (`internal/auth/loopback.go`:
#: "registered with the identity provider and cannot vary at runtime"), sends it
#: in the authorization request, and compares the manifest's value against it for
#: EQUALITY — so a backend that could vary it could only ever be wrong. It is the
#: same binary against every environment, and Supabase registers the same loopback
#: URI for each OAuth client, so there is nothing here for an environment to
#: change.
#:
#: Hardcoded for the reason CLAUDE.md gives for hardcoding the model: one right
#: answer, already settled, and an env var over it would only be a way to break a
#: login. `tests/unit/test_auth_config_cli_contract.py` pins it against the
#: client's own three constants.
OAUTH_REDIRECT_URI = "http://127.0.0.1:42813/oauth/callback"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    # ---- Environment ----------------------------------------------------
    # A CLOSED set on purpose. Every hardening gate below keys on this value, so a
    # free-form string meant a typo ("prod", "Production", "staging") silently skipped
    # weak-secret checks and the local-only-feature gate — i.e. it failed OPEN, in
    # the one direction that matters. (Authentication does not vary by environment
    # either: the posture is `AUTH_MODE`, which is read the same way everywhere —
    # see auth.py.)
    environment: Literal["development", "test", "staging", "production"] = Field(
        default="development", alias="ENVIRONMENT"
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    build_sha: str = Field(default="", alias="BUILD_SHA")

    # ---- Bind address (honored by `python -m daintree_assistant_server`) -
    # Cloud Run injects PORT=8080; local dev sets a fixed port in .env so the
    # CLI can always target it.
    host: str = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=8080, alias="PORT")

    # ---- Upstream provider (Baseten Model APIs, OpenAI-compatible) -------
    # The model is hardcoded (see EXECUTOR_MODEL above). Only the key and base URL
    # come from the environment.
    baseten_base_url: str = Field(
        default="https://inference.baseten.co/v1", alias="BASETEN_BASE_URL"
    )
    # THE server's upstream credential, and the only one there is. Daintree funds
    # every turn from this key: callers no longer supply their own (see auth.py),
    # so this is required for the server to answer anything and is gated by
    # ``missing_secrets``. Local dev reads it from .env; Cloud Run injects it from
    # Secret Manager.
    baseten_api_key: SecretStr = Field(default=SecretStr(""), alias="BASETEN_API_KEY")

    # ---- Second provider (OpenRouter) — OPTIONAL, and deliberately not gating.
    # Funds the ONE utility task that does not run on the serving model: context
    # compaction, which routes to a model an order of magnitude cheaper per input
    # token than the serving model precisely because it re-reads conversation
    # history (see OPENROUTER_COMPACTION_MODEL in services/task_runner.py).
    #
    # It is absent from ``missing_secrets`` ON PURPOSE, and that is the whole
    # difference between the two credentials. Without BASETEN_API_KEY nothing
    # can be answered at all, so its absence must fail readiness. Without this
    # one everything still works except a single task that nothing on the serving
    # path calls yet — so a deployment that never sets it is a correct
    # deployment, not a broken one. Gating readiness on it would take the whole
    # server down over a feature it is not using.
    #
    # There is no OPENROUTER_BASE_URL to match ``baseten_base_url``: that one
    # exists so tests and local proxies can redirect the credential-bearing
    # serving path, and ``weak_secrets`` pins it back to the real origin in
    # production. This provider is constructed against its module default and
    # nothing has asked to move it.
    openrouter_api_key: SecretStr = Field(default=SecretStr(""), alias="OPENROUTER_API_KEY")


    # ---- Thinking headroom -----------------------------------------------
    # The thinking POSTURE itself is hardcoded (see INTERACTIVE_THINKING_ENABLED at
    # the top of this module): main/utility/selector are non-thinking, only the
    # runbook-learning gate and builder/improver still think. reasoning_content
    # round-trips through the wire contract so a tool-call turn can replay the
    # chain-of-thought that produced its calls (the provider adapter renames it to
    # the wire's `reasoning` on the way out), which the assembler preserves —
    # that path stays live because the posture is a flag, not a deletion. Thinking
    # mode ignores temperature/top_p.
    #
    # Because max_tokens is SHARED by reasoning + visible output, a small-output
    # THINKING call adds this headroom to its tight output cap so the
    # chain-of-thought can't starve the answer into a finish_reason=length
    # truncation. Under the current posture the runbook-learning gate is the only
    # such call; utility tasks add it only if thinking is switched back on, since
    # a non-thinking task would otherwise be handed 4k tokens of slack it can
    # spend on VISIBLE output, silently overrunning its profile's cap. The main
    # /respond call never uses this: it runs on a single flat budget
    # (MAIN_DEFAULT_MAX_TOKENS == MAIN_MAX_TOKENS_CEILING, clamped for runaway
    # containment), and a client MAY request its own main max_tokens — so headroom
    # there would fight the ceiling rather than help.
    thinking_headroom_tokens: int = Field(default=4000, ge=0, alias="THINKING_HEADROOM_TOKENS")

    # ---- Main-model output shape ---------------------------------------
    # The narrow Daintree terminal is ~56 columns wide. The fallback for a client
    # that reports no width: the display-context tail block and the output linter
    # both resolve through it.
    main_terminal_width: int = Field(default=56, ge=20, alias="MAIN_TERMINAL_WIDTH")
    # max_tokens policy. The client MAY send generation.max_tokens; when it omits one
    # we apply the default, and either way we clamp to a hard ceiling so a degenerate
    # turn (the observed 7k-token prompt-recitation dump) cannot run unbounded. The
    # default matches the ceiling: the completion budget is SHARED by reasoning +
    # prose + tool-call arguments, and the old compact 2048 default repeatedly cut
    # real multi-spawn tool batches off mid-arguments (finish_reason=length, three
    # sessions on 2026-07-05/06) — each truncation costing a full extra round to
    # re-issue the amputated calls, strictly more expensive than finishing the
    # generation. Runaway containment is the ceiling's job, not the default's.
    main_default_max_tokens: int = Field(default=8000, ge=1, alias="MAIN_DEFAULT_MAX_TOKENS")
    main_max_tokens_ceiling: int = Field(default=8000, ge=1, alias="MAIN_MAX_TOKENS_CEILING")

    # ---- Main-model output linting (observability + optional repair) ----
    # The non-stream path lints the final assistant message (pipe-table width, visible
    # <think> leakage, oversized dumps) and LOGS violations — pure observability by
    # default. Turn repair on to also deterministically convert an over-width pipe table
    # to a per-entity list before returning (never a model call; streaming is untouched).
    main_output_lint_enabled: bool = Field(default=True, alias="MAIN_OUTPUT_LINT_ENABLED")
    main_output_repair_tables: bool = Field(default=False, alias="MAIN_OUTPUT_REPAIR_TABLES")
    # A final assistant message larger than this many characters is flagged as an
    # oversized dump (the recitation failure mode). Observability only.
    main_output_warn_chars: int = Field(default=8000, ge=1, alias="MAIN_OUTPUT_WARN_CHARS")
    # The hard stop for the same failure mode, on the STREAMING path where the
    # lint above is only a post-hoc log. Once a reply with no tool calls has
    # streamed this many visible characters it is not a long answer, it is a
    # generation that has stopped converging, and the server cuts the upstream
    # off rather than paying out the rest of the token ceiling.
    #
    # Twice the DEFAULT MAIN_OUTPUT_WARN_CHARS, deliberately: one warn-width is
    # already the line past which a reply is logged as an oversized dump, so this
    # is not a judgement call about long answers — it is double an amount the
    # output contract already considers wrong. Observed runaways reached ~33.5k
    # characters (the full 8000-token ceiling) before stopping on their own.
    #
    # The two settings are INDEPENDENT, not derived: this is where the sizing came
    # from, not a relationship that is maintained. Moving the warn width does not
    # move the hard stop, and should not — one governs a log line, the other kills
    # a live generation, and coupling them would let a logging tweak change when
    # the server hangs up on a model. 0 disables the guard.
    main_stream_runaway_chars: int = Field(default=16000, ge=0, alias="MAIN_STREAM_RUNAWAY_CHARS")

    # ---- State signing --------------------------------------------------
    state_signing_key_current: SecretStr = Field(
        default=SecretStr(""), alias="STATE_SIGNING_KEY_CURRENT"
    )
    state_signing_key_previous: SecretStr = Field(
        default=SecretStr(""), alias="STATE_SIGNING_KEY_PREVIOUS"
    )
    state_signing_kid: str = Field(default="state-dev", alias="STATE_SIGNING_KID")
    state_signing_kid_previous: str = Field(default="", alias="STATE_SIGNING_KID_PREVIOUS")
    state_token_ttl_seconds: int = Field(default=604800, alias="STATE_TOKEN_TTL_SECONDS")
    state_salt: SecretStr = Field(default=SecretStr(DEFAULT_STATE_SALT), alias="STATE_SALT")

    # ---- Limits ---------------------------------------------------------
    max_request_bytes: int = Field(default=8 * 1024 * 1024, alias="MAX_REQUEST_BYTES")
    max_tools: int = Field(default=128, alias="MAX_TOOLS")

    # ---- Tool-definition bounds (defense before prompt assembly) --------
    max_tool_definition_bytes: int = Field(
        default=2 * 1024 * 1024, alias="MAX_TOOL_DEFINITION_BYTES"
    )
    max_tool_schema_depth: int = Field(default=16, alias="MAX_TOOL_SCHEMA_DEPTH")
    max_tool_schema_properties: int = Field(default=512, alias="MAX_TOOL_SCHEMA_PROPERTIES")
    max_tool_schema_string_bytes: int = Field(
        default=16 * 1024, alias="MAX_TOOL_SCHEMA_STRING_BYTES"
    )

    # ---- Task result_schema bounds (terminal_extract_json only) ------
    max_result_schema_bytes: int = Field(default=32 * 1024, alias="MAX_RESULT_SCHEMA_BYTES")
    max_result_schema_depth: int = Field(default=12, alias="MAX_RESULT_SCHEMA_DEPTH")
    max_result_schema_properties: int = Field(default=128, alias="MAX_RESULT_SCHEMA_PROPERTIES")
    max_result_schema_enum: int = Field(default=128, alias="MAX_RESULT_SCHEMA_ENUM")
    max_result_schema_string: int = Field(default=4096, alias="MAX_RESULT_SCHEMA_STRING")
    # Floored at 2 deliberately: every runbook resolves to a domain foundation, and the
    # orchestrator admits a specific and its foundation as one atomic unit. A budget of 1
    # can never fit that unit, so a cap of 1 would silently load NOTHING rather than
    # loading less. This subsumes the earlier ge=0 guard against a cap of 0 admitting one
    # id anyway; max_meta_header_bytes was pruned on main as dead.
    max_active_runbooks: int = Field(default=3, ge=2, alias="MAX_ACTIVE_RUNBOOKS")

    # ---- Timeouts (seconds) --------------------------------------------
    # The selector gates the turn's first byte, so this bound is a latency budget,
    # not just a failure guard. Non-thinking selector calls benchmark at 1.2-2.6s
    # worst-case; 8s is generous slack on top of that. A transient failure gets ONE
    # retry (the selector's own cap, not MAX_UPSTREAM_RETRIES), bounding a
    # full-timeout selector phase to ~16s before it fails open; a malformed-then-
    # repaired response can double that in the worst case. (httpx applies this as
    # per-phase connect/read/write timeouts, not one total wall clock — close
    # enough for a budget.)
    selector_timeout_seconds: float = Field(default=8.0, alias="SELECTOR_TIMEOUT_SECONDS")
    main_timeout_seconds: float = Field(default=840.0, alias="MAIN_TIMEOUT_SECONDS")
    # Default budget for utility tasks. Deliberately NOT tightened to the
    # aspirational 25s: a profile reads inputs up to 120k chars, and this is an
    # httpx READ budget (the gap between chunks, not a total wall clock), so a
    # tight cap risks repeatable timeouts on the heavy extractions rather than on
    # anything pathological. Revisit only from measured latency distributions. The
    # heavyweight transcript/graph tasks also carry an explicit 60s on their
    # TaskProfile so a future default cut cannot silently starve them.
    # (The original justification here was the ~4k-token reasoning budget of a
    # thinking-high profile. Utility tasks are NON-thinking now — see
    # INTERACTIVE_THINKING_ENABLED — so that argument no longer applies; the input
    # size one does.)
    utility_timeout_seconds: float = Field(default=60.0, alias="UTILITY_TIMEOUT_SECONDS")
    upstream_connect_timeout_seconds: float = Field(
        default=10.0, alias="UPSTREAM_CONNECT_TIMEOUT_SECONDS"
    )

    # ---- Selector -------------------------------------------------------
    # The selector is HARDCODED non-thinking (see INTERACTIVE_THINKING_ENABLED at
    # the top of this module); its prompt's reason-first JSON key order is the
    # inline reasoning step. It reached this posture first, on its own first-byte
    # latency argument, and keeps its own hardcode rather than reading the shared
    # flag — flipping the interactive posture back on must not silently drag the
    # selector with it.
    # Sized with wide margin over the observed 120-190-token completions: the
    # reason-first key order puts runbook_ids LAST in the JSON, so a length
    # truncation would now lose the critical key (parse fail -> repair -> possibly
    # degraded). The cap is runaway containment, not a spend target.
    selector_max_output_tokens: int = Field(default=800, alias="SELECTOR_MAX_OUTPUT_TOKENS")

    # ---- Utility tasks --------------------------------------------------
    # Named small-model tasks (checkpoint, summarize, classify, workflow_plan, …) are
    # NON-THINKING, like the rest of the interactive surface — they read
    # INTERACTIVE_THINKING_ENABLED (task_runner.py), which is False. Each
    # TaskProfile's max_output_tokens therefore sizes its whole completion;
    # thinking_headroom_tokens is added only if that flag is switched back on, so a
    # non-thinking task is never handed 4k tokens of slack to spend on visible
    # output. json tasks use response_format json_object; no utility task uses a
    # forced tool call.

    # ---- Retries --------------------------------------------------------
    # ONE HOP ONLY: backend -> Baseten. Applies to every upstream call (main
    # completion, stream open, selector, utility tasks): retries transient
    # failures only while no model output or replay-unsafe side effect has been
    # exposed. Streaming metadata may already be public during an open/prefetch
    # retry. One retry (= up to 2 attempts) is the normal posture.
    #
    # A SECOND retry layer exists on the OTHER hop — CLI -> backend
    # (cli/internal/backend/retry.go: up to 10 attempts inside a 2-minute
    # elapsed window, sized to ride out a backend restart). That is deliberate,
    # not a stacking bug: it recovers a different failure mode on a different
    # network, and it never replays a deterministic failure (401/403/400/426), an
    # application verdict keyed on error.code, or a turn that has already
    # streamed visible tokens. docs/PROVIDER_POLICY.md documents how the two
    # layers compose; do not "fix" one by assuming the other does not exist.
    max_upstream_retries: int = Field(default=1, ge=0, alias="MAX_UPSTREAM_RETRIES")
    retry_backoff_base_seconds: float = Field(
        default=0.25, ge=0.0, alias="RETRY_BACKOFF_BASE_SECONDS"
    )

    # ---- Runbook-aware tool projection ------------------------------------
    # The one exception to "no env toggles, the server ships one optimum": this
    # is not a product decision being deferred to config, it is the kill switch
    # for a behaviour that withholds capability from the model. Model identity
    # and thinking posture are hardcoded because there IS one right answer;
    # here the right answer is whatever production says this week, and the value
    # of being able to answer "turn it off" in the time it takes Cloud Run to
    # roll a config-only revision is the whole point.
    #
    # `shadow` is the default, so a deployment that sets nothing behaves exactly
    # as it did while projection was measurement-only. Set the PERCENT before
    # flipping the mode: `enforced` at the default 100 is every session at once.
    tool_projection_mode: ToolProjectionMode = Field(default="shadow", alias="TOOL_PROJECTION_MODE")
    # Session-stable, so a conversation stays in one arm for its whole life —
    # a percentage re-rolled per request would offer a continuation round a
    # different inventory than the round that started the work. 100 rather than
    # 0 so that `enforced` means enforced: a mode that silently enforces nothing
    # is a worse default than one that needs a second variable set alongside it.
    tool_projection_rollout_percent: int = Field(
        default=100, ge=0, le=100, alias="TOOL_PROJECTION_ROLLOUT_PERCENT"
    )

    # ---- Training traces (a SEPARATE plane from operational logging) -----
    #
    # `observability.py` is metadata-only and stays that way. This is the other
    # plane: exact content, restricted access, written for later replay and model
    # training. They are not two verbosity levels of one system — combining them
    # would put user content in front of everyone who can read a log line.
    #
    # `off` is the default, so a deployment that sets nothing records nothing.
    # That is the correct default for a service with no account model and
    # therefore nobody to ask for consent.
    trace_capture_mode: TraceCaptureMode = Field(default="off", alias="TRACE_CAPTURE_MODE")
    trace_sink: TraceSinkKind = Field(default="null", alias="TRACE_SINK")
    trace_gcs_bucket: str = Field(default="", alias="TRACE_GCS_BUCKET")
    trace_gcs_prefix: str = Field(default="raw/v1", alias="TRACE_GCS_PREFIX")
    trace_filesystem_dir: str = Field(default="", alias="TRACE_FILESYSTEM_DIR")
    # Its OWN key, never the state-signing key or the state salt. Sharing would
    # make a compromise of either a compromise of both, and would silently
    # re-pseudonymise every trace whenever the other was rotated — splitting one
    # subject into two identities in the middle of a corpus.
    trace_identity_key: SecretStr = Field(default=SecretStr(""), alias="TRACE_IDENTITY_KEY")
    # The bounded wait before `done`. Uploads run concurrently with generation, so
    # the normal path has nothing left to flush; this budget is for the abnormal
    # one. A user does not hold a connection open for a training record.
    trace_final_flush_grace_seconds: float = Field(
        default=0.5, ge=0.0, le=5.0, alias="TRACE_FINAL_FLUSH_GRACE_SECONDS"
    )
    trace_max_pending_bytes: int = Field(
        default=33_554_432, ge=0, alias="TRACE_MAX_PENDING_BYTES"
    )
    trace_max_event_bytes: int = Field(default=8_388_608, ge=0, alias="TRACE_MAX_EVENT_BYTES")

    # ---- Fast response (the Cerebras stage) ------------------------------
    #
    # There is NO mode and NO rollout percentage. The stage runs on every eligible
    # turn, of every deployment, and `CEREBRAS_API_KEY` is required for readiness
    # like any other credential the server spends.
    #
    # Both toggles existed to get the feature into production carefully — `off` /
    # `shadow` / `visible` plus a session-bucketed percentage. That decision is
    # made, and keeping the dials would leave the product's shape configurable in
    # exactly the way `CLAUDE.md` says it must not be: which model, which posture,
    # which prompt are benchmarked decisions with one right answer, not operational
    # dials. Turning the stage off is now a code change, which is the honest
    # description of what it would be.
    # Short and absolute, though no longer because the executor is waiting on it —
    # the two calls go out together now, and nothing is behind this one. It is
    # tight because the WINDOW is: a preamble is shown only until the executor's
    # first visible byte, and one that arrives after that is dropped unshown. A
    # call still running past this point is spending money on an answer that can
    # no longer be used, which is also why there is no retry.
    fast_response_timeout_seconds: float = Field(
        default=3.0, gt=0.0, le=15.0, alias="FAST_RESPONSE_TIMEOUT_SECONDS"
    )
    cerebras_api_key: SecretStr = Field(default=SecretStr(""), alias="CEREBRAS_API_KEY")
    cerebras_base_url: str = Field(default="", alias="CEREBRAS_BASE_URL")
    # Account-gated at Cerebras, so off unless someone has confirmed it is enabled.
    cerebras_prompt_cache_enabled: bool = Field(
        default=False, alias="CEREBRAS_PROMPT_CACHE_ENABLED"
    )

    # ---- Account authentication (Supabase Auth as an OAuth 2.1 server) ---
    #
    # Every value here is DEPLOYMENT IDENTITY, not product policy. Which Supabase
    # project issues our tokens and which OAuth client the CLI registers as differ
    # per environment and cannot be hardcoded; what a valid token MUST look like
    # (the audience, the signing algorithms, the clock skew, what each client is
    # permitted to do) is policy with one right answer and is hardcoded beside the
    # verifier instead. Nothing here is a secret: a JWKS URL, an issuer and a
    # public client id are published to any caller by `/v1/daintree/auth/config`,
    # which is why they are absent from `missing_secrets()` — listing them there
    # would tell an operator to mount public configuration out of Secret Manager.
    auth_mode: AuthMode = Field(default="open", alias="AUTH_MODE")
    #: The exact issuer, ending in `/auth/v1`. Compared for equality against the
    #: token's `iss`, so a token minted by a different Supabase project — the
    #: production one against a staging deployment, or vice versa — is refused.
    supabase_auth_issuer: str = Field(default="", alias="SUPABASE_AUTH_ISSUER")
    supabase_jwks_url: str = Field(default="", alias="SUPABASE_JWKS_URL")
    supabase_oauth_authorize_url: str = Field(default="", alias="SUPABASE_OAUTH_AUTHORIZE_URL")
    supabase_oauth_token_url: str = Field(default="", alias="SUPABASE_OAUTH_TOKEN_URL")
    supabase_oauth_client_id: str = Field(default="", alias="SUPABASE_OAUTH_CLIENT_ID")
    daintree_account_url: str = Field(default="", alias="DAINTREE_ACCOUNT_URL")
    daintree_subscribe_url: str = Field(default="", alias="DAINTREE_SUBSCRIBE_URL")
    #: Published so the client can schedule its own refresh. These MIRROR the
    #: Supabase project's session settings — this server neither sets nor enforces
    #: them — so they are the one pair here that can drift without anything
    #: noticing: raise the project's session length and a stale value tells every
    #: client to sign in early. Change them together.
    #: Bounded at both ends. The floor keeps a typo from telling every client to
    #: refresh continuously; the ceiling (one year) exists because the client
    #: decodes these into a fixed-width integer, so an unbounded Python int would
    #: not merely be implausible — it would fail to decode and take the whole
    #: manifest with it.
    auth_access_token_seconds: int = Field(
        default=3600, ge=60, le=31_536_000, alias="AUTH_ACCESS_TOKEN_SECONDS"
    )
    auth_session_max_age_seconds: int = Field(
        default=2_592_000, ge=60, le=31_536_000, alias="AUTH_SESSION_MAX_AGE_SECONDS"
    )

    # ---- Account entitlement (the website's billing authority) ------------
    #
    # Staged INDEPENDENTLY of `AUTH_MODE`, and the independence is the point: a
    # deployment can enforce identity for a week and only then start asking the
    # billing question, so the two rollouts fail separately and are diagnosed
    # separately.
    #
    # The URL is not a secret and holds no credential of ours: the caller's own
    # Supabase bearer authenticates the lookup, which is why there is no service
    # key here to configure and why this sits with the public OAuth values rather
    # than in `missing_secrets()`.
    entitlement_mode: EntitlementMode = Field(default="off", alias="ENTITLEMENT_MODE")
    daintree_entitlement_url: str = Field(default="", alias="DAINTREE_ENTITLEMENT_URL")

    # ---- Private-staging subject gate -----------------------------------
    #
    # See `parse_subject_digests` above for what the value must look like and
    # `subject_gate_problems` below for when it is required. In short: under
    # ENVIRONMENT=staging with AUTH_MODE=enforce, only the subjects named here may
    # reach entitlement resolution or funded provider work.
    #
    # A `SecretStr` even though a digest is one-way, because the LIST is the
    # sensitive thing rather than any entry: it enumerates a private cohort, and
    # the whole point of the type is that nothing prints it by accident.
    staging_allowed_subject_digests: SecretStr = Field(
        default=SecretStr(""), alias="STAGING_ALLOWED_SUBJECT_DIGESTS"
    )
    #: The one part of the gate that is safe to publish, and the only way an
    #: operator can tell which membership a running revision holds. Not a version
    #: number (CLAUDE.md): a name for the list, changed when the list changes.
    staging_subject_gate_revision: str = Field(
        default="", alias="STAGING_SUBJECT_GATE_REVISION"
    )

    # ---- Policy ---------------------------------------------------------
    allow_remote_image_urls: bool = Field(default=False, alias="ALLOW_REMOTE_IMAGE_URLS")
    expose_debug_selector_reason: bool = Field(default=False, alias="EXPOSE_DEBUG_SELECTOR_REASON")

    # ---- Upstream request dumping (LOCAL DEVELOPMENT ONLY) ---------------
    # When set, every upstream request body is written to this directory as
    # numbered pretty-printed JSON — the prompt-cache forensics tool (diff two
    # dumps to find the byte where two "identical" requests diverge). The dump
    # contains FULL prompts, so a set value in production fails readiness (see
    # local_only_problems).
    dump_upstream_dir: str | None = Field(default=None, alias="DAINTREE_DUMP_UPSTREAM_DIR")

    # ---- Runbook overlay directory (LOCAL DEVELOPMENT ONLY) ----------------
    # A second directory of runbook files merged OVER the packaged catalog: a new
    # id is added, a colliding id replaces the packaged runbook. It exists so a
    # runbook can be authored in a scratch directory against a running server —
    # POST /v1/daintree/runbooks/reload rebuilds the catalog without a restart.
    # The path is read once, at startup; only its CONTENTS are re-read on
    # reload. A set value in production fails readiness (see
    # local_only_problems) and the reload route is not mounted there at all: a
    # deploy must serve the catalog its image shipped, not whatever happens to
    # be on the instance's disk.
    runbooks_overlay_dir: str | None = Field(default=None, alias="DAINTREE_RUNBOOKS_OVERLAY_DIR")

    # ---- Context field caps (bytes, unless noted) -----------------------
    # Session-stable agent registry rendered before the visible conversation.
    # Rows are admitted whole so an exact agent id is never truncated into a
    # different, unusable identifier.
    cap_startup_agents: int = Field(default=512, alias="CAP_STARTUP_AGENTS")
    cap_startup_agent_bytes: int = Field(default=16 * 1024, alias="CAP_STARTUP_AGENT_BYTES")
    cap_project_instructions: int = Field(default=16 * 1024, alias="CAP_PROJECT_INSTRUCTIONS")
    cap_goal: int = Field(default=8 * 1024, alias="CAP_GOAL")
    cap_workflow_runs: int = Field(default=24 * 1024, alias="CAP_WORKFLOW_RUNS")
    cap_async_operations: int = Field(default=8 * 1024, alias="CAP_ASYNC_OPERATIONS")
    cap_pinned_memories: int = Field(default=16 * 1024, alias="CAP_PINNED_MEMORIES")
    cap_relevant_memories: int = Field(default=16 * 1024, alias="CAP_RELEVANT_MEMORIES")
    # Max open-terminal inventory rows rendered into the runtime context (issue #286).
    cap_open_terminals: int = Field(default=32, alias="CAP_OPEN_TERMINALS")
    cap_watchers_count: int = Field(default=20, alias="CAP_WATCHERS_COUNT")
    cap_watchers_bytes: int = Field(default=4 * 1024, alias="CAP_WATCHERS_BYTES")
    # Workflow-intelligence digests (turn.workflow_state): max digests rendered
    # per turn, and the total byte budget for the rendered block. Every digest
    # line is additionally collapsed/capped to a single 512-byte line.
    cap_workflow_digests: int = Field(default=5, alias="CAP_WORKFLOW_DIGESTS")
    cap_workflow_state_bytes: int = Field(default=16 * 1024, alias="CAP_WORKFLOW_STATE_BYTES")
    cap_message_text_part: int = Field(default=1024 * 1024, alias="CAP_MESSAGE_TEXT_PART")
    cap_message_text: int = Field(default=1024 * 1024, alias="CAP_MESSAGE_TEXT")

    # ---- Request SHAPE caps (counts, not bytes) -------------------------
    # The byte caps above bound how big one thing can be; these bound how MANY
    # things there can be, which the 8 MiB body ceiling does not: a request of
    # 100,000 two-byte messages is small on the wire and expensive everywhere
    # afterwards — sequence validation, sanitization, assembly, serialization —
    # and every one of those costs is paid before anything decides the prompt is
    # unusable. Rejecting pathological STRUCTURE at the boundary is cheaper than
    # discovering it downstream, and it makes the failure legible instead of
    # arriving as a timeout.
    #
    # Every default is far above real traffic on purpose. These are not tuning
    # knobs: crossing one means something is malformed, so the right value is the
    # one a healthy client can never reach. (Real numbers to compare against: the
    # CLI dispatches at most 6 tool calls in parallel, `max_active_runbooks` is 3,
    # and the OpenAI wire allows 4 stop sequences.)
    cap_messages: int = Field(default=2000, alias="CAP_MESSAGES")
    cap_content_parts: int = Field(default=64, alias="CAP_CONTENT_PARTS")
    cap_tool_calls_per_message: int = Field(default=64, alias="CAP_TOOL_CALLS_PER_MESSAGE")
    cap_stop_sequences: int = Field(default=8, alias="CAP_STOP_SEQUENCES")
    cap_stop_sequence_bytes: int = Field(default=256, alias="CAP_STOP_SEQUENCE_BYTES")
    cap_pinned_runbook_ids: int = Field(default=16, alias="CAP_PINNED_RUNBOOK_IDS")

    # ---- Images -----------------------------------------------------------
    # Nothing sends images today (`multimodal_images.data_urls` is advertised,
    # but the CLI has no production caller for its own image-part constructor),
    # so these bound a path before it has a producer rather than after. The
    # ENCODED cap is the one enforced: base64 expands by 4/3, so bounding the
    # string bounds the decoded image without decoding anything to find out.
    cap_images: int = Field(default=8, alias="CAP_IMAGES")
    cap_image_encoded_bytes: int = Field(default=6 * 1024 * 1024, alias="CAP_IMAGE_ENCODED_BYTES")

    # ---- CLI-provided integrations (MCP servers) ------------------------
    # NOT a tool index: client tool schemas already ride the function-calling
    # interface, so listing them again would only duplicate them.
    cap_mcp_servers: int = Field(default=32, alias="CAP_MCP_SERVERS")
    cap_mcp_block_bytes: int = Field(default=64 * 1024, alias="CAP_MCP_BLOCK_BYTES")

    # ---- Server-retrieved Daintree documentation ------------------------
    # The one MCP the SERVER calls (docs are public, stateless and identical for
    # every caller — see services/docs_mcp.py). Capped like every other body of
    # text that reaches the prompt from outside: first-party pages are still
    # pages, and this block rides the volatile tail where each byte is re-encoded
    # on the following turn.
    cap_docs_query: int = Field(default=256, alias="CAP_DOCS_QUERY")
    cap_docs_block_bytes: int = Field(default=16 * 1024, alias="CAP_DOCS_BLOCK_BYTES")

    # ---- Context compaction ---------------------------------------------
    # The largest compacted block worth delivering, in UTF-8 bytes. Generous
    # rather than tight: the block is what a long conversation collapses INTO, so
    # a mean cap would either lose the identifiers compaction exists to protect or
    # reject the very turns it was built for. It is a runaway guard — an oversized
    # block is discarded whole and the turn keeps its raw history, because
    # truncating reconciled state cuts exactly the agent ids and worktree paths
    # that make the block worth having.
    # The upper bound is not decoration: `CompactionBlock.content` carries its own
    # code-point limit, and a byte cap set above it would let a block clear this
    # check and then be rejected by the wire model — a failure mode reachable only
    # by configuration, which is the kind nobody finds until production.
    cap_compaction_block_bytes: int = Field(
        default=64 * 1024, ge=1, le=256 * 1024, alias="CAP_COMPACTION_BLOCK_BYTES"
    )

    # ---- Runbook learning (LOCAL DEVELOPMENT ONLY) ------------------------
    # A backend-internal harness that watches completed local workflows and, when
    # warranted, writes or improves runbook files so the catalog "learns". It is
    # entirely invisible to the CLI: no new wire fields, no feedback tool, and the
    # main assistant model is never told it exists — a created runbook simply appears
    # as a normal dynamic runbook load. It MUST be off in production: an enabled mode
    # ---- Derived helpers ------------------------------------------------

    @property
    def is_hardened(self) -> bool:
        """True where production hardening applies: no local-only features, weak
        secrets rejected, the upstream origin pinned, internal detail withheld
        from responses, and OAuth endpoints required to be https rather than
        loopback. (It does not decide the authentication POSTURE: that is
        `AUTH_MODE`, and it is read the same way in every environment.)

        The single place that decides "is this a real deployment?", so the checks
        below and ``/readyz`` cannot drift apart.
        """
        return self.environment in ("production", "staging")

    @property
    def serving_api_key(self) -> SecretStr:
        """The credential that funds every turn."""
        return self.baseten_api_key

    @property
    def serving_base_url(self) -> str:
        """The origin the serving credential is sent to."""
        return self.baseten_base_url

    def missing_secrets(self) -> list[str]:
        """Return env var names of required secrets that are empty.

        All three entries are SERVER-owned, which is the whole shape of the
        credential model: Daintree holds every upstream key and callers hold none
        (see auth.py). ``BASETEN_API_KEY`` is required for the server to answer
        anything at all, and its absence must fail READINESS rather than surface a
        turn later as an upstream 401 that reads like the caller's fault. There is
        still no INBOUND key to configure: account authentication is OAuth, and
        its settings are public configuration gated through ``auth_problems()``
        rather than secrets. The door's posture is ``AUTH_MODE``, not an absence.

        ``CEREBRAS_API_KEY`` joined them when the fast-response stage lost its off
        switch. It is a weaker requirement than the other two — the turn still
        answers without it — but not a soft one: the stage runs on every eligible
        turn, so a missing key is the feature failing open silently on all of them,
        which is indistinguishable from a model that chose not to answer. That is
        exactly the failure that never gets noticed in production.

        (Contrast OPENROUTER_API_KEY, deliberately absent: nothing on the serving
        path routes there, so a deployment without it is complete.)
        """
        missing: list[str] = []
        if not self.state_signing_key_current.get_secret_value():
            missing.append("STATE_SIGNING_KEY_CURRENT")
        if not self.serving_api_key.get_secret_value():
            missing.append(EXECUTOR_KEY_ENV)
        # CEREBRAS_API_KEY is deliberately NOT here any more. It was added when the
        # fast-response stage lost its off switch — a missing key then meant the
        # stage failed open on every eligible turn, indistinguishable from a model
        # choosing to say nothing. `FAST_RESPONSE_ENABLED` is False now, so the
        # stage never opens a call and the key funds nothing. Requiring a
        # credential for a code path that cannot execute fails readiness on a
        # deployment that is, in fact, complete.
        return missing

    def local_only_problems(self) -> list[str]:
        """Local-development-only features that must never run in production.

        These join ``missing_secrets()`` / ``weak_secrets()`` in readiness gating.
        Two entries: prompt dumping, which writes full prompts to disk, and the
        runbook overlay directory, which would let a production instance serve a
        catalog its image never shipped. (Runbook learning used to be here too,
        and was the reason this method exists — a production deploy that left it
        on would have mutated the runbook catalog from model output at runtime. It
        has been removed entirely. The overlay is a much smaller thing: it only
        reads files a developer deliberately put there. Gated anyway, because
        "which runbooks is production actually running?" must have one answer.)
        """
        problems: list[str] = []
        if self.is_hardened and self.dump_upstream_dir:
            problems.append(
                "DAINTREE_DUMP_UPSTREAM_DIR must be unset in production "
                "(prompt dumping is development-only)"
            )
        if self.is_hardened and self.runbooks_overlay_dir:
            problems.append(
                "DAINTREE_RUNBOOKS_OVERLAY_DIR must be unset in production "
                "(the runbook overlay is development-only)"
            )
        problems.extend(self.trace_problems())
        problems.extend(self.fast_response_problems())
        problems.extend(self.auth_problems())
        problems.extend(self.entitlement_problems())
        problems.extend(self.subject_gate_problems())
        return problems

    #: The values that must be present for a manifest to be publishable. Ordered
    #: as the manifest reads, so a completeness failure names fields in the order
    #: an operator filled them in.
    #:
    #: The callback is absent because it is a constant rather than a setting
    #: (``OAUTH_REDIRECT_URI``), and the two links are absent because the client
    #: treats them as OPTIONAL. Requiring them here would have reported an
    #: otherwise perfectly working OAuth deployment as unconfigured and withheld
    #: the entire manifest over a missing subscribe page — suppressing sign-in to
    #: enforce a nicety. They are still validated whenever they are set.
    _MANIFEST_FIELDS: ClassVar[tuple[str, ...]] = (
        "supabase_auth_issuer",
        "supabase_jwks_url",
        "supabase_oauth_authorize_url",
        "supabase_oauth_token_url",
        "supabase_oauth_client_id",
    )

    def auth_manifest_problems(self) -> list[str]:
        """Everything wrong with the OAuth values THEMSELVES, ignoring the mode.

        Split from :meth:`auth_problems` because "this manifest is unsafe to
        publish" and "this deployment is not allowed to run in that mode" are
        different questions with different consumers.
        ``/v1/daintree/auth/config`` deliberately answers while the service is
        unready, so it cannot use readiness as its filter: it asks THIS method
        whether the values may be served, and a mode it does not support must not
        be the reason a valid manifest is withheld.

        Run in every mode, including ``open``. A malformed value is not dormant
        under the open door — it is still published, and still rejected by every
        client that reads it.
        """
        problems: list[str] = []
        urls = (
            ("SUPABASE_AUTH_ISSUER", self.supabase_auth_issuer),
            ("SUPABASE_JWKS_URL", self.supabase_jwks_url),
            ("SUPABASE_OAUTH_AUTHORIZE_URL", self.supabase_oauth_authorize_url),
            ("SUPABASE_OAUTH_TOKEN_URL", self.supabase_oauth_token_url),
            ("DAINTREE_ACCOUNT_URL", self.daintree_account_url),
            ("DAINTREE_SUBSCRIBE_URL", self.daintree_subscribe_url),
        )
        links = {"DAINTREE_ACCOUNT_URL", "DAINTREE_SUBSCRIBE_URL"}
        for env_name, value in urls:
            if not value:
                continue
            problem = _auth_url_problem(
                value=value,
                env_name=env_name,
                hardened=self.is_hardened,
                allow_fragment=env_name in links,
            )
            if problem:
                problems.append(problem)
            # Percent-escapes in an OAuth endpoint path are refused outright, and
            # the reason is that every way of HANDLING them diverges from the
            # client. Decoding sees `%2f` as a segment boundary where the client's
            # parser refuses the URL entirely; not decoding lets `%2e%2e` past the
            # dot-segment rule; and a malformed `%zz` is untouched here and fatal
            # there. A real issuer, authorize, token or JWKS path never contains
            # one, so refusing is both strictly safer and costs nothing.
            elif env_name not in links and "%" in urlsplit(value).path:
                problems.append(
                    f"{env_name} must not percent-encode anything in its path "
                    "(an OAuth endpoint path never needs it, and clients disagree "
                    "about how to read one)"
                )

        # The issuer is compared for EQUALITY against a token's `iss`, so its path
        # is part of the credential check rather than decoration. Supabase serves
        # auth under `/auth/v1`, and the overwhelmingly common way to get this
        # wrong is to paste the bare project URL — which would then match no token
        # ever minted and present as "every login is rejected".
        issuer_parses = bool(self.supabase_auth_issuer) and _origin_of(self.supabase_auth_issuer)
        issuer_path = urlsplit(self.supabase_auth_issuer).path if issuer_parses else "/auth/v1"
        if not issuer_path.rstrip("/").endswith("/auth/v1"):
            problems.append(
                "SUPABASE_AUTH_ISSUER must end in /auth/v1 (it is compared for "
                "equality against the token's `iss`, so the bare project URL "
                "would match nothing)"
            )

        # The issuer is the root of trust, so it is pinned to Daintree's own
        # identity providers. Discovery supplies the project, the client id and the
        # endpoints; it must not be able to choose which company issues a user's
        # tokens. Loopback is exempt outside production because a local provider is,
        # by construction, one the developer is already running.
        issuer_origin = _origin_of(self.supabase_auth_issuer) if self.supabase_auth_issuer else None
        if issuer_origin is not None:
            issuer_host = issuer_origin[1]
            if not _host_allowed(issuer_host, _ALLOWED_ISSUER_SUFFIXES) and not (
                not self.is_hardened and issuer_host.lower() in _LOCAL_HOSTNAMES
            ):
                allowed = ", ".join(_ALLOWED_ISSUER_SUFFIXES)
                problems.append(
                    f"SUPABASE_AUTH_ISSUER names a host that is not a Daintree "
                    f"identity provider (expected {allowed} or a subdomain)"
                )

        # Same origin AND under the issuer's path, against the ISSUER rather than
        # against each other. A token endpoint on a host the issuer does not name is
        # where an authorization code would be exchanged with a stranger, and it is
        # the check that stops a half-edited configuration — three URLs updated for a
        # new project and one left behind — from pointing part of the login at the
        # old one. The path half matters for the same reason one origin down: a
        # sibling handler on the issuer's own host is still not the issuer.
        if issuer_origin is not None:
            # Compared as WRITTEN rather than as parsed, because that is how the
            # client compares them. `https://h` and `https://h:443` are the same
            # origin by any semantic reading and are two different strings to a
            # textual one — so a configuration mixing the two spellings passes a
            # semantic check here and is refused on the user's machine. Being the
            # stricter of the two implementations is the only way this endpoint's
            # promise holds.
            issuer_netloc = urlsplit(self.supabase_auth_issuer).netloc
            expected = f"{issuer_origin[0]}://{issuer_netloc}"
            issuer_path = urlsplit(self.supabase_auth_issuer).path
            for env_name, value in (
                ("SUPABASE_JWKS_URL", self.supabase_jwks_url),
                ("SUPABASE_OAUTH_AUTHORIZE_URL", self.supabase_oauth_authorize_url),
                ("SUPABASE_OAUTH_TOKEN_URL", self.supabase_oauth_token_url),
            ):
                if not value:
                    continue
                other = _origin_of(value)
                if other is None:
                    continue
                if urlsplit(value).netloc != issuer_netloc:
                    problems.append(
                        f"{env_name} must be written with exactly the same authority as "
                        f"SUPABASE_AUTH_ISSUER ({expected})"
                    )
                elif not _is_under_path(urlsplit(value).path, issuer_path):
                    problems.append(
                        f"{env_name} must be under the SUPABASE_AUTH_ISSUER path "
                        f"({issuer_path or '/'})"
                    )

        # Where the manifest may send a BROWSER. Pinned separately from the issuer
        # because these are ordinary web pages rather than a token endpoint, and for
        # the same reason: the assistant opens them under its own recommendation
        # ("manage your account", "buy a plan"), so a value that could be chosen
        # freely could point someone at a convincing phishing page with the product
        # vouching for it.
        for env_name, value in (
            ("DAINTREE_ACCOUNT_URL", self.daintree_account_url),
            ("DAINTREE_SUBSCRIBE_URL", self.daintree_subscribe_url),
        ):
            if not value:
                continue
            origin = _origin_of(value)
            if origin is None:
                continue
            host = origin[1]
            if not _host_allowed(host, _ALLOWED_LINK_SUFFIXES) and not (
                not self.is_hardened and host.lower() in _LOCAL_HOSTNAMES
            ):
                problems.append(
                    f"{env_name} must point at a Daintree site "
                    f"({_ALLOWED_LINK_SUFFIXES[0]} or a subdomain)"
                )

        problems.extend(self._client_id_problems())
        return problems

    def _client_id_problems(self) -> list[str]:
        """The OAuth client id, which is not a URL and needs its own rules.

        Surrounding whitespace is REJECTED rather than trimmed, and that is the
        rule worth explaining. The client trims what it receives and validates the
        trimmed copy — so a value with spaces would pass every check on both sides
        while this server published the raw one. Validating a value and then
        transmitting a different value is how `" client "` reaches a URL query
        string with its spaces intact.
        """
        value = self.supabase_oauth_client_id
        if not value:
            return []
        if value != value.strip():
            return ["SUPABASE_OAUTH_CLIENT_ID must not have leading or trailing whitespace"]
        if len(value) > _MAX_CLIENT_ID_LENGTH:
            return [
                f"SUPABASE_OAUTH_CLIENT_ID is {len(value)} characters, which is not a "
                f"plausible identifier (max {_MAX_CLIENT_ID_LENGTH})"
            ]
        if not set(value) <= _CLIENT_ID_CHARS:
            return [
                "SUPABASE_OAUTH_CLIENT_ID contains characters that are not valid in an "
                "OAuth client id (letters, digits, and - _ . ~)"
            ]
        return []

    def auth_problems(self) -> list[str]:
        """Account-authentication configuration that must fail readiness.

        Two rules, and the split between them is the point.

        **Whatever is set is validated in every mode** — see
        :meth:`auth_manifest_problems`.

        **Completeness is required only once the mode is not ``open``.** A
        deployment that is not doing account authentication has no reason to hold
        an OAuth client id, and demanding one would fail readiness on every
        instance running today's posture.

        A third rule lived here until verification existed and has been removed
        rather than left inert: while nothing read a token, a non-``open`` mode was
        refused outright, so a deployment could not report an authentication
        boundary it did not have. That gate did its job — ``auth.py`` verifies for
        real now — and keeping a guard whose condition can no longer be false is a
        branch every later reader has to reason about and no test can reach.

        :meth:`entitlement_problems` is the same shape one layer out, and is kept
        separate for the same reason the modes are: identity and entitlement fail
        differently, and a single method reporting both would make an operator
        read a billing misconfiguration as an authentication one.

        What is deliberately NOT here: a rule that ``open`` and ``observe`` are
        refused in production. The master spec asks for one, and it is right about
        the destination and wrong about today — ``open`` IS the deployed
        production posture, documented as such in ``docs/DAINTREE_API.md`` and
        asserted by ``infra/gcp/smoke.sh``. Adding that gate now would fail
        ``/readyz`` on the very next release and the revision would never take
        traffic. It belongs to the cutover, and ``TODO.md`` carries it with that
        trigger.
        """
        problems = self.auth_manifest_problems()

        if self.auth_mode == "open":
            return problems

        for name in self._MANIFEST_FIELDS:
            if not str(getattr(self, name)).strip():
                env_name = name.upper()
                problems.append(
                    f"AUTH_MODE={self.auth_mode} requires {env_name} "
                    "(a deployment checking tokens must also be able to tell a "
                    "client how to obtain one)"
                )
        return problems

    @property
    def auth_is_configured(self) -> bool:
        """Whether a complete and VALID OAuth manifest exists.

        Validity is half of it, and it is the half that is easy to leave out.
        ``/v1/daintree/auth/config`` deliberately answers while the service is
        unready, so a manifest that failed readiness is still reachable: without
        this check, a deployment whose token endpoint had been typo'd onto another
        host would fail to start AND publish that host to every client, which is
        the one shape of this bug that could send an authorization code to a
        stranger. Completeness alone is not a licence to publish.

        The mode is deliberately not consulted. Whether this build supports
        ``enforce`` has nothing to do with whether these values are safe to
        serve.
        """
        if self.auth_manifest_problems():
            return False
        return all(str(getattr(self, name)).strip() for name in self._MANIFEST_FIELDS)

    def entitlement_problems(self) -> list[str]:
        """Entitlement configuration that must fail readiness rather than degrade.

        Fails CLOSED, and never DOWNWARDS. A bad entitlement configuration is not
        quietly demoted to ``off``: an operator who asked for enforcement and got
        an open till would have no way to discover it, and the revision would take
        traffic looking exactly like a working one. Refusing readiness means the
        revision never gets that far.

        Two rules, and they are separate on purpose.

        **Whatever is set is validated in every mode**, including ``off``. A
        malformed URL is not dormant — it is the value the account endpoint will
        use the moment somebody configures a mode, and finding out then is finding
        out during a rollout.

        **A non-``off`` mode additionally requires enforced identity and a URL.**
        The identity half is the one worth stating: under ``open`` and ``observe``
        every request resolves to the anonymous principal, which has no subject to
        ask about and no bearer to forward. An entitlement mode above an
        unenforced door would therefore be unable to decide anything, on every
        request, and would record that as evidence.
        """
        problems = self._entitlement_url_problems()

        if self.entitlement_mode == "off":
            return problems

        if self.auth_mode != "enforce":
            problems.append(
                f"ENTITLEMENT_MODE={self.entitlement_mode} requires AUTH_MODE=enforce "
                "(under any other mode every request is the anonymous principal, "
                "which has no account to ask about)"
            )
        if not self.daintree_entitlement_url.strip():
            problems.append(
                f"ENTITLEMENT_MODE={self.entitlement_mode} requires "
                "DAINTREE_ENTITLEMENT_URL (there is no billing authority to ask)"
            )
        return problems

    def _entitlement_url_problems(self) -> list[str]:
        """Everything wrong with the entitlement URL ITSELF, ignoring the mode.

        Split from :meth:`entitlement_problems` for the same reason
        :meth:`auth_manifest_problems` is split from :meth:`auth_problems`: "this
        value is unusable" and "this deployment may not run in that mode" are
        different questions, and the account endpoint asks only the first.
        """
        problems: list[str] = []
        url = self.daintree_entitlement_url
        if url:
            problem = _auth_url_problem(
                value=url,
                env_name="DAINTREE_ENTITLEMENT_URL",
                hardened=self.is_hardened,
            )
            if problem:
                problems.append(problem)
            else:
                # A query string is refused rather than forwarded. This URL is the
                # whole request — the caller's bearer is the only other input —
                # so anything in a query is configuration deciding what we ask
                # about, which is the one thing the verified subject must decide.
                # (`_auth_url_problem` already refuses userinfo and a fragment.)
                # Tested on the WRITTEN string, not on the parsed components:
                # `urlsplit` reports an empty query for a URL ending in `?`, so a
                # component test passes a spelling the contract forbids. It
                # carries no data, and that is exactly why it should be corrected
                # in configuration rather than tolerated here.
                if "?" in url or "#" in url:
                    problems.append(
                        "DAINTREE_ENTITLEMENT_URL must not carry a query string or "
                        "fragment (the caller's verified subject is the only input "
                        "to this lookup)"
                    )
                origin = _origin_of(url)
                assert origin is not None  # _auth_url_problem returned None
                host = origin[1]
                approved = _APPROVED_ENTITLEMENT_ORIGINS.get(self.environment)
                if approved is not None:
                    # An enumerated origin, compared as an origin rather than as
                    # a hostname: the port is part of it, so a non-default one is
                    # refused here without a rule of its own. Both sides go
                    # through `_origin_of`, so `https://x` and `https://x:443`
                    # are the same approved value rather than one accepted
                    # spelling and one rejected synonym.
                    # Both sides normalised the way `_host_allowed` already
                    # normalises: lowercased by `urlsplit`, and ONE trailing dot
                    # stripped because `staging.daintree.org.` and
                    # `staging.daintree.org` are the same name to DNS. An
                    # enumerated rule that refused the dotted spelling would be
                    # stricter than the resolver and disagree with the suffix
                    # rule it replaces about the same host.
                    permitted = {
                        _normalised_origin(_origin_of(value)) for value in approved
                    }
                    if _normalised_origin(origin) not in permitted:
                        problems.append(
                            "DAINTREE_ENTITLEMENT_URL must be one of the approved "
                            f"{self.environment} website origins "
                            f"({', '.join(approved)}) — this call forwards the "
                            "caller's account credential, so every other host "
                            "under daintree.org is a place it could be delivered"
                        )
                elif not _host_allowed(host, _ALLOWED_LINK_SUFFIXES) and not (
                    not self.is_hardened and _is_loopback_literal(host)
                ):
                    # Development and test only. The suffix rule and the loopback
                    # carve-out are what make a locally-run website developable,
                    # and neither environment is a boundary anything is trusted
                    # across.
                    problems.append(
                        "DAINTREE_ENTITLEMENT_URL must point at a Daintree site "
                        f"({_ALLOWED_LINK_SUFFIXES[0]} or a subdomain) — this call "
                        "forwards the caller's account credential"
                    )
                # And the PATH, which the host rule above cannot stand in for. A
                # host answers "is this us"; only the path answers "is this the
                # route that is supposed to receive the caller's bearer". The
                # configured value IS the request line — `services/entitlement.py`
                # stores it verbatim and hands it to `build_request` with nothing
                # appended, joined or normalised — so whatever is written here is
                # exactly where the credential goes.
                #
                # Checked LAST so the earlier refusals keep naming the sharper
                # mistake: `https://daintree.com/api/assistant/entitlement` is a
                # wrong-site problem, not a path problem, and being told about the
                # path would send an operator looking in the wrong place.
                #
                # This rule also binds the loopback carve-out, deliberately. That
                # carve-out exists so a developer can run the website locally; it
                # relaxes the HOST and the SCHEME because there is no network to
                # intercept, and neither of those reasons says anything about which
                # route is being called. A local website must still serve this at
                # `http://127.0.0.1:<port>/api/assistant/entitlement`.
                path = urlsplit(url).path
                if "%" in path:
                    # Refused outright rather than decoded and compared, for the
                    # same reason the OAuth endpoint paths above refuse one: every
                    # way of HANDLING an escape here diverges from somebody. If we
                    # decode, `/api/%2f/../assistant/entitlement` can be argued into
                    # matching while the far end routes it somewhere else; if we do
                    # not, an escape that decodes to the right route is accepted as
                    # a spelling this contract never agreed to. There is no legal
                    # reason for one in a fixed, literal route, so refusing costs
                    # nothing and cannot be argued with.
                    problems.append(
                        "DAINTREE_ENTITLEMENT_URL must not percent-encode anything "
                        f"in its path (the route is the literal {_ENTITLEMENT_PATH})"
                    )
                elif path != _ENTITLEMENT_PATH:
                    # Exact equality, and every near-miss falls out of it without a
                    # rule of its own: a sibling route, a prefix or suffix glued to
                    # the last segment, a doubled slash, a different case, a
                    # trailing slash. Nothing here echoes the offending path — this
                    # string reaches the startup log and, outside production, a
                    # readiness response, and the value most likely to be malformed
                    # is one with a credential pasted into it.
                    problems.append(
                        "DAINTREE_ENTITLEMENT_URL must name exactly the path "
                        f"{_ENTITLEMENT_PATH} (no trailing slash) — pinning the "
                        "origin alone still lets a mistyped route receive the "
                        "caller's forwarded bearer"
                    )
        return problems

    @property
    def staging_subject_gate_enabled(self) -> bool:
        """Whether the private-staging subject allowlist actually decides anything.

        Two conditions, and both are load-bearing.

        ``ENVIRONMENT=staging`` because that is the only deployment the list
        describes. ``AUTH_MODE=enforce`` because below it there is no subject to
        test: ``open`` never reads the header and ``observe`` verifies and then
        discards, so every request is the anonymous principal. A gate above an
        unenforced door could only ever refuse everyone or nobody, and the honest
        spelling of that is "off".

        Production is deliberately absent, and it is not an oversight — see
        :meth:`subject_gate_problems`, which refuses to let a production
        deployment hold this value at all.
        """
        return self.environment == "staging" and self.auth_mode == "enforce"

    @property
    def published_gate_revision(self) -> str:
        """The membership label, but only once it is known to be well formed.

        ``/version`` answers while the service is UNREADY — deliberately, because
        the endpoint exists so an operator can see what a revision that failed to
        come up actually is. So "readiness refuses a malformed label" is not on
        its own a reason the label is safe to echo: the very deployment whose
        label is wrong is the one whose ``/version`` is being read. A value typed
        into the wrong variable — a digest, a subject id, a sentence — would
        otherwise be published verbatim on a public unauthenticated route, and
        logged beside every refusal, by exactly the mistake that made it unsafe.

        An unvalidated label reads as absent instead. That is the honest
        projection: the deployment has no usable membership name.
        """
        if _gate_revision_problem(self.staging_subject_gate_revision) is not None:
            return ""
        return self.staging_subject_gate_revision

    def allowed_subject_digests(self) -> frozenset[str]:
        """The gate's membership, or an empty set.

        Empty is a REFUSAL of everybody rather than a licence for anybody: the
        door tests membership, so an unparseable list admits no one. Readiness has
        already refused a staging deployment whose list does not parse, so the
        door should never see one — this is the second half of the same answer,
        placed where a wiring mistake still fails closed.

        Parsed per call rather than cached. The list is at most 64 short strings
        and ``Settings`` is frozen, so a cache would be a second copy of secret
        material bought to save a few microseconds on a path that is already about
        to verify an RSA signature.
        """
        entries, _ = parse_subject_digests(
            self.staging_allowed_subject_digests.get_secret_value()
        )
        return entries

    def subject_gate_problems(self) -> list[str]:
        """The private-staging subject gate, as a readiness verdict.

        Three rules, and the third is the one this brief had to decide.

        **Where the gate is enabled, it must be complete.** A missing, empty or
        malformed list, or a missing or malformed revision, fails readiness. It
        does NOT degrade to "no gate": an operator who asked for a private
        deployment and silently got a public one has no way to discover that, and
        the revision would take traffic looking exactly like a working one — the
        same reasoning :meth:`entitlement_problems` gives for never demoting to
        ``off``.

        **Whatever is set is validated everywhere**, including where the gate is
        inert. A malformed list in development is the value that will be promoted,
        and finding out at promotion is finding out during a rollout.

        **Production may not hold this value at all.** The doc that specified the
        gate scopes it to private staging and says no production default may
        silently inherit a staging list. Refusing readiness is the only reading of
        that with no silent half: production neither enforces a list it was handed
        (which would be a private-beta gate nobody decided to ship) nor ignores one
        (which would leave an operator believing production was gated when it was
        not). Both of those are decisions made by accident; this one has to be
        made on purpose, and when it is, it will be a different setting with its
        own name.
        """
        problems: list[str] = []
        configured = self.staging_allowed_subject_digests.get_secret_value()

        if self.environment == "production" and configured:
            problems.append(
                "STAGING_ALLOWED_SUBJECT_DIGESTS must be unset in production "
                "(the subject gate is a private-staging feature and production "
                "would neither enforce nor inherit it)"
            )
            return problems

        if configured:
            _, problem = parse_subject_digests(configured)
            if problem:
                problems.append(problem)

        if not self.staging_subject_gate_enabled:
            # The revision names a list. With no gate deciding anything there is
            # nothing for it to name, so an unset one is correct rather than
            # missing — but a value that IS set is still checked, because it is
            # published on `/version` in every environment.
            if self.staging_subject_gate_revision:
                revision_problem = _gate_revision_problem(self.staging_subject_gate_revision)
                if revision_problem:
                    problems.append(revision_problem)
            return problems

        if not configured:
            problems.append(
                "ENVIRONMENT=staging with AUTH_MODE=enforce requires "
                "STAGING_ALLOWED_SUBJECT_DIGESTS (a private staging deployment "
                "that admits every verified account is not private)"
            )
        revision_problem = _gate_revision_problem(self.staging_subject_gate_revision)
        if revision_problem:
            problems.append(revision_problem)
        return problems

    @property
    def entitlement_is_configured(self) -> bool:
        """Whether an entitlement lookup can be attempted at all.

        Deliberately independent of ``entitlement_mode``: ``GET
        /v1/daintree/account`` resolves whenever a usable URL exists, so the
        contract is exercisable — by a real CLI, against the real website — before
        it is able to refuse anybody's turn. A mode is what makes a verdict
        BINDING, not what makes it obtainable.
        """
        if not self.daintree_entitlement_url.strip():
            return False
        return not self._entitlement_url_problems()

    def fast_response_problems(self) -> list[str]:
        """Configuration that must fail readiness rather than degrade silently.

        The fast stage is best-effort BY DESIGN — a Cerebras outage costs a
        preamble, never a turn — and that is exactly why a misconfiguration has to
        be loud somewhere. The missing-credential half of that moved to
        `missing_secrets()` when the stage lost its off switch, since the key
        became simply one of the three credentials this server owns.

        What is left here is where that credential gets SENT, which is a different
        question and still belongs beside the executor's identical check.

        The KEY itself is not checked here — it moved to `missing_secrets()` when
        the stage lost its off switch, because it is now simply one of the three
        credentials the server owns. What is left is the origin check below, which
        is about where that credential gets sent rather than whether it exists.
        """
        problems: list[str] = []
        # The SAME check the executor's origin gets, through the same helper —
        # which exists for exactly this reason ("so both providers get the same
        # check rather than a copy each"). A hostname comparison alone is not it:
        # `http://api.cerebras.ai` and `https://api.cerebras.ai:8443` both pass a
        # hostname test while sending the server's credential somewhere else.
        if self.is_hardened and self.cerebras_base_url:
            problem = _origin_problem(
                value=self.cerebras_base_url,
                expected_host=FAST_RESPONSE_ORIGIN,
                env_name=FAST_RESPONSE_URL_ENV,
            )
            if problem:
                problems.append(problem)
        return problems

    def trace_problems(self) -> list[str]:
        """Trace configuration that must fail readiness rather than run.

        Every entry here fails CLOSED — a misconfigured trace plane records
        nothing rather than recording the wrong thing to the wrong place. The
        content gate is the one that matters most: there is no CONSENT record
        and therefore no consent to rely on, so a hardened deployment asking for
        full content capture is refused outright rather than trusted.
        """
        problems: list[str] = []
        if self.trace_capture_mode == "full_internal" and self.is_hardened:
            problems.append(
                "TRACE_CAPTURE_MODE=full_internal is refused in production/staging "
                "until account-level training eligibility exists (it would capture "
                "user content with nobody having consented)"
            )
        if self.trace_capture_mode != "off":
            # Required for EVERY enabled mode, not just full content capture.
            # `metadata` still writes pseudonymous session and turn keys, and an
            # empty key means HMAC-with-no-key — a value anyone holding the trace
            # can recompute, which makes the pseudonym decorative.
            if not self.trace_identity_key.get_secret_value():
                problems.append(
                    "TRACE_CAPTURE_MODE is enabled but TRACE_IDENTITY_KEY is empty "
                    "(session and turn ids would be pseudonymised with an empty key, "
                    "which anyone holding the trace could recompute)"
                )
            if self.trace_sink == "gcs" and not self.trace_gcs_bucket:
                problems.append("TRACE_SINK=gcs requires TRACE_GCS_BUCKET")
            if self.trace_sink == "filesystem":
                if not self.trace_filesystem_dir:
                    problems.append("TRACE_SINK=filesystem requires TRACE_FILESYSTEM_DIR")
                if self.is_hardened:
                    problems.append(
                        "TRACE_SINK=filesystem is forbidden in production/staging "
                        "(container-local storage for content-bearing traces)"
                    )
        return problems

    def weak_secrets(self) -> list[str]:
        """Production-only: secrets that are present but too weak/default.

        These join ``missing_secrets()`` to gate readiness, so a deploy with a
        default salt or a short signing key fails ``/readyz`` rather than running
        with forgeable state tokens. In development they are allowed (tests use
        short keys).
        """
        if not self.is_hardened:
            return []
        problems: list[str] = []
        # The upstream base URL receives THE SERVER's credential on every
        # request. A hardened deployment with a typo'd, plain-http or foreign
        # host would disclose the key that funds every Daintree turn while
        # staying green, so production pins the real origin. Development stays
        # free to point at proxies and mocks.
        problem = _origin_problem(
            value=self.serving_base_url,
            expected_host=EXECUTOR_ORIGIN,
            env_name=EXECUTOR_URL_ENV,
        )
        if problem is not None:
            problems.append(problem)
        if len(self.state_signing_key_current.get_secret_value()) < 32:
            problems.append("STATE_SIGNING_KEY_CURRENT is too short (<32 chars)")
        if self.state_salt.get_secret_value() == DEFAULT_STATE_SALT:
            problems.append("STATE_SALT is the default value")
        has_prev_key = bool(self.state_signing_key_previous.get_secret_value())
        has_prev_kid = bool(self.state_signing_kid_previous)
        if has_prev_key != has_prev_kid:
            problems.append(
                "STATE_SIGNING_KEY_PREVIOUS and STATE_SIGNING_KID_PREVIOUS must be set together"
            )
        return problems


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, parsed once."""
    return Settings()
