"""The executor's SECOND host: the same gpt-6-luna weights, via OpenRouter.

This adapter exists for one job — answering a turn OpenAI could not answer —
and everything unusual about it follows from that job rather than from OpenRouter
being a router.

**It is the only place in this server that sends a ``provider`` block, and that
is a deliberate exception to a rule the rest of the package keeps.** The parent
(:mod:`providers.openrouter`) sends none, and
``tests/unit/test_provider_openrouter.py::test_no_routing_or_affinity_field_is_ever_sent``
pins that by name so the retired routing apparatus cannot creep back into the
ordinary path. What is sent here is an ORDERING — ``{"sort": "latency"}`` — over
a pool that genuinely exists, on a path that only runs when the preferred host
has already failed. ``config.EXECUTOR_FALLBACK_SORT`` carries the reasoning for
latency specifically.

**OpenAI's own endpoints are excluded from the pool.** OpenRouter serves this
model from OpenAI, Azure and Amazon Bedrock; this adapter is reached because
OpenAI just failed, so routing back to it through an extra hop buys a slower
copy of the failure that already happened. ``config.EXECUTOR_FALLBACK_IGNORED_PROVIDERS``
holds the slug.

**``require_parameters`` is on**, which is not tidiness. OpenRouter's default is
to DROP a parameter an endpoint does not support rather than to avoid that
endpoint, and ``response_format`` on every JSON utility task is a parameter
whose loss is silent and expensive. The flip side is that anything the MODEL
does not support must never be sent at all, or the pool filters to empty: this
model refuses ``temperature`` and ``stop`` (see ``providers/openai.py``), so
both are dropped here exactly as the primary drops them.

**The reasoning posture is the primary's**, under OpenRouter's canonical
``reasoning.effort``: a turn answered here reasons the same amount as a turn
answered by OpenAI, which is what makes the fallback a substitute rather than a
different product. OpenRouter handles tools + reasoning for this model itself.

One capability difference is worth knowing when reading a turn's accounting:
**OpenRouter reports ``usage.cost`` and OpenAI does not**. A failed-over turn is
therefore priced while an ordinary one is not.
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
        # `OpenAIProvider` and `CerebrasProvider`: the product decisions stay
        # beside the model id and this module keeps its one job. The defaults are
        # those same constants, so a provider built without arguments is built
        # with the shipped posture rather than an accidental one.
        self._reasoning_effort = reasoning_effort
        self._sort = sort
        self._ignore_providers = tuple(ignore_providers)

    def _build_body(self, request: ProviderChatRequest, *, stream: bool) -> dict[str, Any]:
        body = super()._build_body(request, stream=stream)

        # Unsupported by this model on every endpoint; under `require_parameters`
        # sending either would leave no endpoint to serve the call.
        body.pop("temperature", None)
        body.pop("stop", None)

        # The primary's one posture, unconditionally — see the module docstring.
        body["reasoning"] = {"effort": self._reasoning_effort}

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
