"""Product operation modes — the one place a mode maps to a route.

Daintree exposes exactly two modes. A caller chooses a MODE; it never chooses a
provider or a model id::

    standard -> Together AI      / deepseek-ai/DeepSeek-V4-Flash-0731
    pro      -> Cerebras         / gemma-4-31b

That indirection is the whole design. Model ids stay pinned server-side, prompts
and skills stay server-owned, and the public response keeps saying
``daintree-assistant`` in both modes — the product promise is Standard or Pro,
not a permanent contract with Together, Cerebras, DeepSeek or Gemma. Swapping
Gemma for Qwen the day Cerebras serves it is editing :data:`PRO_MODEL`; no
caller, wire shape, or stored preference moves with it.

**Why a registry rather than two provider classes.** ``StandardProvider`` /
``ProProvider`` would duplicate transport logic and confuse a product tier with
a vendor. The adapters already exist and are already normalized behind
``ChatProvider``; a mode is a route that POINTS at one.

**Why modes are not profiles.** ``profile`` (assistant / subagent) chooses which
persona answers, which prompt is assembled and which skills are even eligible.
``mode`` chooses where the request is sent. They are orthogonal — a Pro
sub-agent is an ordinary combination — and collapsing them would mean a paid
tier could not delegate, or that delegating changed which model you paid for.

**There is no automatic cross-mode fallback, anywhere.** If Pro is unavailable
the turn fails as Pro. Silently answering a Pro request from Standard would
change expected quality, cost, latency, tool-call behaviour and cache lineage
under a label the user chose deliberately — and would do it invisibly, which is
the part that makes it indefensible rather than merely wrong. Retries stay
inside the resolved route; a mode change is a new request the user authorizes.

This module is deliberately dependency-light — it imports only the provider
Protocol — so contracts, config, auth, services and ``main`` can all reach it
without an import cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from .providers.base import ChatProvider


class OperationMode(StrEnum):
    """The closed set of product modes.

    One shared enum rather than a ``Literal`` repeated at each surface: the wire
    contract, the signed state token, capabilities, the doctor route and the
    task runner all name these values, and four hand-copied literals drift.
    """

    STANDARD = "standard"
    PRO = "pro"


#: The default when a request does not name a mode. See ``RespondRequest.mode``
#: for why the field is optional rather than required — briefly: it mirrors
#: ``profile``, the other execution dimension, and defaulting to the CHEAPER
#: route means the failure mode of an un-updated client is "gets what it always
#: got", never "spends the expensive tier by accident".
DEFAULT_MODE = OperationMode.STANDARD

#: Which adapter serves a mode. A closed set, like ``TaskProfile.provider_id``
#: beside it: this names the two vendors the product actually has routes to, and
#: the type is what stops it growing back into the general endpoint-selection
#: policy that was deleted in the move off OpenRouter.
ProviderId = Literal["together", "cerebras"]

# ---- Pinned product decisions ---------------------------------------------
# Hardcoded, not environment config, for the same reason every other model id
# here is: there is one right answer per mode and it is chosen by measurement.
# Changing one is a deliberate re-baseline (benchmarks/baseline.json), not a
# deployment knob.

#: Standard. The weights `context_compact` has always used; Together is the host
#: that made them fast (p50 1100ms against OpenRouter's 3414ms on the same 62
#: utility payloads — the host was the latency, not the model).
STANDARD_MODEL = "deepseek-ai/DeepSeek-V4-Flash-0731"

#: Pro. Measured p50 486ms on the same payloads — Cerebras is the reason Pro is
#: the FAST tier rather than the big-model tier. Gemma 4 31B is a preview model
#: Cerebras has said it will replace; when Qwen 3.8 lands there, this constant
#: is the change.
PRO_MODEL = "gemma-4-31b"

#: The origin each provider's base URL must resolve to in a hardened
#: environment. A server API key is sent there, so a base URL pointing anywhere
#: else is a credential leak with a plausible-looking cause.
PROVIDER_ORIGINS: Mapping[ProviderId, str] = {
    "together": "api.together.xyz",
    "cerebras": "api.cerebras.ai",
}

#: The environment variable naming each provider's credential. Both are
#: server-owned and both gate readiness — a mode the service ADVERTISES and
#: cannot serve is worse than one it does not offer, because the failure lands
#: on a user's first Pro turn as an upstream 401 that reads like their fault.
PROVIDER_KEY_ENV: Mapping[ProviderId, str] = {
    "together": "TOGETHER_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
}

#: The environment variable naming each provider's base URL.
PROVIDER_URL_ENV: Mapping[ProviderId, str] = {
    "together": "TOGETHER_BASE_URL",
    "cerebras": "CEREBRAS_BASE_URL",
}


@dataclass(frozen=True)
class ThinkingPolicy:
    """Whether a mode's model is asked to reason, and how hard.

    Per MODE rather than global. With one serving model a single
    ``INTERACTIVE_THINKING_ENABLED`` was honest; with two model families a
    global posture would silently mean different things on each route — Gemma 4
    31B cannot reason at all, DeepSeek V4 Flash reasons by default and has to be
    told not to.

    Both modes ship disabled for the first dual-mode release, deliberately. Pro
    changes the provider, the model, the first-token latency, the token budget
    and the tool-call behaviour all at once; adding reasoning to that would make
    a poor result impossible to attribute.
    """

    enabled: bool = False
    effort: str | None = None


#: The product posture, shared by both modes today. A named constant rather than
#: a default argument spelled out at each call site, so "what does a caller that
#: does not care get?" has one answer.
NO_THINKING = ThinkingPolicy(enabled=False)


@dataclass(frozen=True)
class ModeSpec:
    """Everything about a mode that is knowable without an HTTP client.

    Split from :class:`ResolvedMode` so the static facts — the ones docs-drift
    tests, the baseline capture, capabilities and the doctor route all read —
    are importable in a process that has built no providers.
    """

    mode: OperationMode
    provider_id: ProviderId
    upstream_model: str
    #: What the CLIENT is told. Identical across modes on purpose.
    public_model_id: str
    #: Prefix for the upstream cache-affinity key. Together and Cerebras keep
    #: separate caches anyway, so this buys nothing today — it is here because
    #: an unlabelled key is undebuggable from the far side, and because the day
    #: two modes share a vendor account it stops being cosmetic.
    cache_namespace: str
    thinking: ThinkingPolicy

    @property
    def key_env(self) -> str:
        return PROVIDER_KEY_ENV[self.provider_id]

    @property
    def url_env(self) -> str:
        return PROVIDER_URL_ENV[self.provider_id]

    @property
    def origin(self) -> str:
        return PROVIDER_ORIGINS[self.provider_id]


PUBLIC_MODEL_ID = "daintree-assistant"

STANDARD_SPEC = ModeSpec(
    mode=OperationMode.STANDARD,
    provider_id="together",
    upstream_model=STANDARD_MODEL,
    public_model_id=PUBLIC_MODEL_ID,
    cache_namespace="standard",
    thinking=ThinkingPolicy(enabled=False),
)

PRO_SPEC = ModeSpec(
    mode=OperationMode.PRO,
    provider_id="cerebras",
    upstream_model=PRO_MODEL,
    public_model_id=PUBLIC_MODEL_ID,
    cache_namespace="pro",
    thinking=ThinkingPolicy(enabled=False),
)

MODE_SPECS: Mapping[OperationMode, ModeSpec] = {
    OperationMode.STANDARD: STANDARD_SPEC,
    OperationMode.PRO: PRO_SPEC,
}


@dataclass(frozen=True)
class ResolvedMode:
    """One immutable route, resolved once per request and threaded through it.

    Passing this object — rather than re-reading a registry deeper down — is
    what makes it structurally impossible for a retry, a JSON repair or a
    bare-intent re-roll to land on a different provider than the attempt it is
    repairing.
    """

    spec: ModeSpec
    provider: ChatProvider

    @property
    def mode(self) -> OperationMode:
        return self.spec.mode

    @property
    def model(self) -> str:
        return self.spec.upstream_model


class ModeRegistry:
    """The server's fixed mode-to-route map, built once at startup."""

    def __init__(self, routes: Mapping[OperationMode, ResolvedMode]) -> None:
        expected = set(OperationMode)
        actual = set(routes)
        if actual != expected:
            missing = sorted(mode.value for mode in expected - actual)
            extra = sorted(str(mode) for mode in actual - expected)
            raise ValueError(f"invalid mode registry: missing={missing}, extra={extra}")
        for mode, route in routes.items():
            if route.spec.mode is not mode:
                raise ValueError(
                    f"mode registry key {mode.value!r} holds the {route.spec.mode.value!r} spec"
                )
        self._routes = dict(routes)

    def resolve(self, mode: OperationMode) -> ResolvedMode:
        return self._routes[mode]

    def specs(self) -> tuple[ModeSpec, ...]:
        """Every spec, in enum order — the order capabilities advertises."""
        return tuple(self._routes[mode].spec for mode in OperationMode)

    async def aclose(self) -> None:
        """Close each distinct provider exactly once.

        Deduplicated on identity even though today's two routes hold two
        objects: a future mode sharing an adapter with another would otherwise
        have its client closed twice, and the second close is the kind of error
        that only shows up during shutdown, where nobody is watching.

        Failures are isolated PER PROVIDER, not per registry. With one provider
        a raising `aclose` was the end of teardown either way; with two, letting
        the first exception escape strands the second one's connection pool —
        and the caller suppresses registry-level exceptions, so the leak is
        completely silent.
        """
        import contextlib

        seen: set[int] = set()
        for route in self._routes.values():
            identity = id(route.provider)
            if identity in seen:
                continue
            seen.add(identity)
            with contextlib.suppress(Exception):
                await route.provider.aclose()


def uniform_registry(provider: ChatProvider) -> ModeRegistry:
    """Every mode routed to ONE provider.

    For callers replacing the upstream wholesale — tests, deterministic
    benchmarks, the ``build_container(provider=...)`` override. Deliberately
    wrong for testing ROUTING: a suite asking "did the Pro request reach
    Cerebras?" needs one recording fake per mode and should build its own
    registry, because this one answers yes to everything.
    """
    return ModeRegistry({spec.mode: ResolvedMode(spec, provider) for spec in MODE_SPECS.values()})
