"""Endpoint-selection policy — the ``provider`` block on every upstream request.

OpenRouter is a router, not a model host: many independent endpoints serve the
same weights, and this block is what decides which one answers. Two of the
choices encoded here are *product* decisions rather than tuning knobs — how
private the routing is, and whether speed or price wins — so they live in one
explicit, typed object instead of a dict literal spread across the adapter.

**The privacy distinction is the reason this module exists.** OpenRouter models
"does not collect or train on your data" and "does not retain your data" as two
separate filters, and conflating them in user-facing copy is the kind of mistake
that is worse than a conservative promise:

* ``data_collection: "deny"`` filters to endpoints that do not COLLECT prompt
  data or train on it. An endpoint in that pool may still hold a request
  transiently, e.g. for abuse monitoring.
* ``zdr: true`` is the separate, stricter zero-data-retention filter. It narrows
  the pool substantially.

:class:`PrivacyMode` names that pair so the difference cannot be lost in a
boolean, and so the active mode can be reported to the client and asserted in
tests. See ``docs/PROVIDER_POLICY.md`` section 4 — the authority for the copy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

# Cap on a client-supplied endpoint allow/deny list. Generous — the model is
# served by a couple of dozen endpoints — but bounded, because these strings are
# forwarded to the upstream and echoed in nothing we control.
MAX_ENDPOINT_LIST = 24
MAX_ENDPOINT_SLUG_LENGTH = 64


class PrivacyMode(StrEnum):
    """How strictly routing is filtered for data handling.

    A :class:`StrEnum` so the value serializes straight into JSON responses
    (capabilities, diagnostics) without a conversion step.
    """

    #: Route only to endpoints that do not collect prompt data or train on it.
    #: A no-training guarantee. NOT a no-retention one.
    NO_TRAINING = "no_training"

    #: The above, plus OpenRouter's zero-data-retention filter. A much smaller
    #: pool, so an unusual parameter mix is materially more likely to find no
    #: route at all — which fails closed as ``upstream_no_compliant_provider``
    #: rather than silently relaxing the filter.
    ZDR = "zdr"

    @property
    def description(self) -> str:
        """One line of accurate, user-facing copy for this mode.

        Kept beside the enum so a client rendering the active mode cannot invent
        its own wording. Never says "does not store" for ``NO_TRAINING``.
        """
        if self is PrivacyMode.ZDR:
            return (
                "Requests route only to endpoints that enforce zero data retention "
                "and do not collect or train on request data."
            )
        return (
            "Requests route only to endpoints that do not collect or train on "
            "request data. Zero data retention is not enforced."
        )


class EndpointSort(StrEnum):
    """Which axis ranks the eligible endpoints once filtering is done."""

    #: Fastest measured tokens/second wins, explicitly at the cost of price.
    #: Correct for an interactive assistant: wall clock is dominated by
    #: generation, and this model is cheap enough that a price delta is noise
    #: next to a speed one.
    THROUGHPUT = "throughput"
    #: Cheapest wins. For a caller who is paying and not in a hurry.
    PRICE = "price"
    #: Lowest measured time-to-first-token wins.
    LATENCY = "latency"


# The tail bound applied to STRUCTURED calls — anything carrying
# ``response_format``. Those calls have the opposite cost shape to a chat turn:
# they emit a ~40-token JSON object, so ranking their (smaller, json-capable)
# pool by tokens/second optimises the wrong axis entirely. It elected endpoints
# with acceptable medians and catastrophic tails.
#
# ``preferred_max_latency`` DEPRIORITISES rather than excludes, so unlike a
# filter it can never empty the pool — the tail-happy endpoints stay reachable,
# they just stop winning.
STRUCTURED_LATENCY_BOUND: dict[str, float] = {"p90": 2.0, "p99": 5.0}

# The policy applied to CLASSIFIER calls — a structured call whose output is a
# handful of tokens and whose wall-clock is therefore almost entirely prefill.
# The skill selector is the case that matters: ~10.3k prompt tokens in, ~90
# completion tokens out (measured over 438 trials, `benchmarks/skill_selection`),
# sitting directly on the turn's first-byte path.
#
# ``sort: "latency"`` ranks the eligible endpoints by measured time-to-first-token.
# ``sort: "throughput"`` ranks by tokens/second — how fast the ~90 tokens arrive
# once prefill is done, which at ~100 tok/s is ~0.9s of that 2.044s. That is not
# nothing, and the case for re-ranking does not pretend it is: the point is that
# the OTHER ~55% is the half that moves. It is prefill over 10.3k tokens plus
# queueing, it scales with the catalog, and it is what an affinity key can drive
# toward zero.
#
# The tail bound is kept and tightened underneath, because the two answer different
# questions: the sort ranks on central tendency, the bound deprioritises an
# endpoint whose PERCENTILES are bad. The same run measured p99 9.878s against
# that p50 — a spread the sort alone would not have caught.
CLASSIFIER_LATENCY_BOUND: dict[str, float] = {"p50": 0.8, "p90": 2.0, "p99": 4.0}


@dataclass(frozen=True)
class RoutingPolicy:
    """A complete endpoint-selection policy, renderable to OpenRouter's wire block.

    Frozen because a policy is a decision, not mutable state: the request path
    derives a new one (:meth:`merged_with`) rather than editing the default in
    place, so one caller's preference can never leak into the next request.
    """

    privacy: PrivacyMode = PrivacyMode.NO_TRAINING
    sort: EndpointSort = EndpointSort.THROUGHPUT
    #: Drop endpoints that would silently ignore something we sent. Matters most
    #: for ``json_object`` calls: several otherwise-fast endpoints for this model
    #: do not implement ``response_format``, and without this they would answer
    #: with unconstrained prose that fails our parse.
    require_parameters: bool = True
    #: Keep the next-best compliant endpoint available when the winner is down.
    #: With filters this strict, turning it off makes any single outage a hard
    #: failure.
    allow_fallbacks: bool = True
    #: Optional endpoint allowlist / denylist (OpenRouter provider slugs).
    only: tuple[str, ...] = ()
    ignore: tuple[str, ...] = ()
    #: Percentile TTFT bound, applied to structured calls. Deprioritises only.
    preferred_max_latency: dict[str, float] | None = field(default=None)

    def to_wire(self) -> dict[str, Any]:
        """Render the OpenRouter ``provider`` block.

        Key order is fixed and insertion-ordered on purpose: this dict is
        serialized into the request body on every call, and the prompt cache keys
        on a byte-identical prefix. A dict whose key order wobbled between
        otherwise identical requests would be a silent cache miss.
        """
        wire: dict[str, Any] = {
            "sort": self.sort.value,
            "data_collection": "deny",
            "require_parameters": self.require_parameters,
            "allow_fallbacks": self.allow_fallbacks,
        }
        if self.privacy is PrivacyMode.ZDR:
            # Additive, never a replacement: ZDR is the retention filter and
            # `data_collection` is the training filter. Sending only `zdr` would
            # quietly drop the no-training floor.
            wire["zdr"] = True
        if self.only:
            wire["only"] = list(self.only)
        if self.ignore:
            wire["ignore"] = list(self.ignore)
        if self.preferred_max_latency:
            wire["preferred_max_latency"] = dict(self.preferred_max_latency)
        return wire

    def for_structured_call(self) -> RoutingPolicy:
        """This policy plus the structured-call tail bound."""
        if self.preferred_max_latency is not None:
            return self
        return replace_policy(self, preferred_max_latency=dict(STRUCTURED_LATENCY_BOUND))

    def for_classifier_call(self) -> RoutingPolicy:
        """This policy re-ranked for a tiny-output, prefill-bound call.

        Unlike :meth:`for_structured_call` this REPLACES the sort key, so it is
        applied BEFORE a caller preference rather than after (see
        ``OpenRouterProvider._build_body``): the latency ranking is the server's
        default for this call shape, and a caller who names their own ``sort``
        still overrides it. The bound is additive and survives either way.
        """
        return replace_policy(
            self,
            sort=EndpointSort.LATENCY,
            preferred_max_latency=(
                self.preferred_max_latency
                if self.preferred_max_latency is not None
                else dict(CLASSIFIER_LATENCY_BOUND)
            ),
        )

    def merged_with(self, preference: RoutingPreference | None) -> RoutingPolicy:
        """Apply a validated caller preference on top of this policy.

        A preference may only make routing **stricter or differently ranked** —
        it selects among modes the server defines. It cannot disable the
        no-training floor, because `data_collection: "deny"` is not derived from
        any field a caller controls (see :meth:`to_wire`).
        """
        if preference is None:
            return self
        return replace_policy(
            self,
            privacy=preference.privacy or self.privacy,
            sort=preference.sort or self.sort,
            only=preference.only or self.only,
            ignore=preference.ignore or self.ignore,
        )


@dataclass(frozen=True)
class RoutingPreference:
    """A caller's validated routing choice, normalized off the wire.

    Deliberately NOT a free-form dict forwarded to OpenRouter. A pass-through
    would let a client weaken the privacy floor, pin an endpoint that ignores
    ``response_format``, or send a key we have never tested — and the failure
    would surface as an inscrutable upstream error rather than a validation one.
    """

    privacy: PrivacyMode | None = None
    sort: EndpointSort | None = None
    only: tuple[str, ...] = ()
    ignore: tuple[str, ...] = ()


def replace_policy(policy: RoutingPolicy, **changes: Any) -> RoutingPolicy:
    """``dataclasses.replace`` under a name that reads at the call site."""
    from dataclasses import replace

    return replace(policy, **changes)


class _WireRouting(Protocol):
    """The shape the wire model presents.

    Structural rather than a concrete import so this module stays a leaf: the
    provider layer should not have to know the contracts package exists.
    """

    @property
    def privacy(self) -> str | None: ...

    @property
    def sort(self) -> str | None: ...

    @property
    def only(self) -> list[str]: ...

    @property
    def ignore(self) -> list[str]: ...


def routing_preference(wire: _WireRouting | None) -> RoutingPreference | None:
    """Normalize the validated wire block into the provider-layer type.

    The single conversion point between "what a client sent" and "what routing
    this call gets". Returns ``None`` for an absent block *and* for one whose
    every field is empty, so a client that sends ``{}`` takes the fast default
    path instead of re-rendering a byte-identical policy (see
    ``OpenRouterProvider._build_body``).
    """
    if wire is None:
        return None
    privacy = PrivacyMode(wire.privacy) if wire.privacy else None
    sort = EndpointSort(wire.sort) if wire.sort else None
    only = tuple(wire.only or ())
    ignore = tuple(wire.ignore or ())
    if privacy is None and sort is None and not only and not ignore:
        return None
    return RoutingPreference(privacy=privacy, sort=sort, only=only, ignore=ignore)


#: The shipped default. Hardcoded like the model ids — one optimum, no env
#: toggle. A caller may override the parts :class:`RoutingPreference` exposes.
DEFAULT_POLICY = RoutingPolicy()
