"""The executor's SECOND host: the same GLM-5.3-Flash weights, via OpenRouter.

This adapter exists for one job — answering a turn Baseten could not answer —
and everything unusual about it follows from that job rather than from OpenRouter
being a router.

**It is the only place in this server that sends a ``provider`` block, and that
is a deliberate exception to a rule the rest of the package keeps.** The parent
(:mod:`providers.openrouter`) sends none, and
``tests/unit/test_provider_openrouter.py::test_no_routing_or_affinity_field_is_ever_sent``
pins that by name so the retired routing apparatus cannot creep back into the
ordinary path. Nothing here reopens that: there is no privacy filter, no
output-quality denylist, no fallback pool the caller can shape, no
sticky-affinity key, and no caller-facing preference. What is sent is an
ORDERING — ``{"sort": "latency"}`` — over a pool that genuinely exists, on a path
that only runs when the preferred host has already failed.

The distinction that makes the exception coherent: the deleted block chose
between endpoints as POLICY on every ordinary turn, against a host that has only
one endpoint, so it could only ever narrow a pool of one into a pool of zero.
This one states a preference among twenty real endpoints for a request whose user
is already waiting out a failure. ``config.EXECUTOR_FALLBACK_SORT`` carries the
reasoning for latency specifically.

**Baseten is excluded from the pool.** OpenRouter serves this model from Baseten
among others, and that is the one endpoint that cannot help here: this adapter is
reached because Baseten just failed, so routing back to the same infrastructure
through an extra hop buys a slower copy of the failure that already happened.
``config.EXECUTOR_FALLBACK_IGNORED_PROVIDERS`` holds the slug.

**``require_parameters`` is on**, which is not tidiness. OpenRouter's default is
to DROP a parameter an endpoint does not support rather than to avoid that
endpoint, and this server sends parameters whose loss is silent and expensive:
``response_format`` on every JSON utility task (dropped, the model answers prose,
the task burns its one repair retry and then 502s) and ``stop`` on the paths that
set one. Requiring support turns "quietly served wrong" into "served by a
different endpoint", which with twenty candidates costs nothing worth having.

**The thinking posture is the Baseten adapter's, restated in OpenRouter's
spelling.** GLM-5.3-Flash cannot be run non-thinking on any host — reasoning is
always on, there is no ``none``, and an omitted effort means ``high`` — so the
parent's ``reasoning: {"enabled": false}``, which is exactly right for a model
that CAN be held down, is the expensive-or-undefined choice here. The floor goes
on the wire unconditionally, under OpenRouter's canonical ``reasoning.effort``
(which it normalizes to each endpoint's own ``reasoning_effort``). The effect is
that a turn answered here reasons the same amount as a turn answered on Baseten,
which is the property that makes the fallback a substitute rather than a
different product.

**What it does NOT inherit from the Baseten adapter is the ``reasoning_content``
un-rename**, and that asymmetry is the point of having two subclasses. Baseten
spells a replayed chain-of-thought ``reasoning_content`` in both directions;
OpenRouter's canonical field on an assistant row is ``reasoning``, which is
precisely what the parent's ``_rewrite_messages`` already produces. So the parent
is correct here and is left alone.

One capability difference is worth knowing when reading a turn's accounting:
**OpenRouter reports ``usage.cost`` and Baseten does not**. A failed-over turn is
therefore priced while an ordinary one is not. Nothing needed changing for that —
``ProviderUsage.cost`` has always been "not reported" rather than zero — but a
cost that appears only on the unhappy path is a real thing to remember before
reading a dashboard.
"""

from __future__ import annotations

from typing import Any

from ..config import (
    EXECUTOR_FALLBACK_IGNORED_PROVIDERS,
    EXECUTOR_FALLBACK_SORT,
    EXECUTOR_REASONING_EFFORT,
)
from .base import ProviderChatRequest
from .openrouter import OpenRouterProvider


class OpenRouterFallbackProvider(OpenRouterProvider):
    """The OpenAI-shape adapter, pointed at OpenRouter and sorted by latency.

    Everything the parent does — SSE parsing, tool-call accumulation, error
    mapping, credential redaction, the ``/key`` probe, the dev-only request dump
    — is inherited unchanged. Only the body differs, and only in two fields.
    """

    def __init__(
        self,
        *args: Any,
        reasoning_effort: str = EXECUTOR_REASONING_EFFORT,
        sort: str = EXECUTOR_FALLBACK_SORT,
        ignore_providers: tuple[str, ...] = EXECUTOR_FALLBACK_IGNORED_PROVIDERS,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        # Injected rather than read from `config` inside `_build_body`, matching
        # `BasetenProvider` and `CerebrasProvider`: the product decisions stay
        # beside the model id and this module keeps its one job. The defaults are
        # those same constants, so a provider built without arguments is built
        # with the shipped posture rather than an accidental one.
        self._reasoning_effort = reasoning_effort
        self._sort = sort
        self._ignore_providers = tuple(ignore_providers)

    def _build_body(self, request: ProviderChatRequest, *, stream: bool) -> dict[str, Any]:
        body = super()._build_body(request, stream=stream)

        # The floor, unconditionally — see the module docstring. `thinking_disabled`
        # is honoured as "the cheapest this model will do" rather than dropped,
        # exactly as `BasetenProvider` honours it: the callers that set it (the
        # forced single-object path, the assembler's non-thinking pins) are asking
        # for the least discursive call available, and on these weights omitting
        # the field asks for the MOST expensive one.
        if request.thinking_disabled or not request.reasoning_effort:
            body["reasoning"] = {"effort": self._reasoning_effort}
        else:
            body["reasoning"] = {"effort": request.reasoning_effort}

        # THE ROUTING BLOCK, and the only one this server sends. Built here rather
        # than held as a constant dict because a shared mutable would be handed to
        # `json=` on every request and a single stray mutation would be permanent.
        routing: dict[str, Any] = {
            "sort": self._sort,
            # OpenRouter's default, stated rather than assumed: if the
            # lowest-latency endpoint refuses the call, try the next one. Turning
            # this off would make the fallback itself a single point of failure,
            # which is the exact shape of the problem it exists to fix.
            "allow_fallbacks": True,
            # Never silently drop a parameter — see the module docstring.
            "require_parameters": True,
        }
        if self._ignore_providers:
            routing["ignore"] = list(self._ignore_providers)
        body["provider"] = routing
        return body
