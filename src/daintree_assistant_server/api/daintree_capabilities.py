"""The Daintree capabilities endpoint (section 13)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from .. import PROTOCOL_MAX, PROTOCOL_MIN, __version__
from ..auth import Principal, require_auth
from ..config import INTERACTIVE_THINKING_ENABLED, PUBLIC_MODEL_ID
from ..providers.policy import (
    DEFAULT_POLICY,
    MAX_ENDPOINT_LIST,
    EndpointSort,
    PrivacyMode,
)
from ..services.docs_events import DOCS_SEARCH_WIRE_NAME
from .deps import require_ready

router = APIRouter()


@router.get("/v1/daintree/capabilities")
async def capabilities(
    request: Request,
    _principal: Principal = Depends(require_auth),
    _ready: None = Depends(require_ready),
) -> dict[str, object]:
    container = request.app.state.container
    settings = request.app.state.settings
    return {
        "server_version": __version__,
        "protocol": {"min": PROTOCOL_MIN, "max": PROTOCOL_MAX},
        "respond": {
            "endpoint": "/v1/daintree/respond",
            "model": PUBLIC_MODEL_ID,
            "streaming": True,
            "stream_events": ["meta", "status", "delta", "done", "error"],
            # The main call's thinking posture is hardcoded OFF (a product decision,
            # like the model ids — see INTERACTIVE_THINKING_ENABLED in config.py), so
            # responses do not carry reasoning_content and the stream emits no
            # status:{phase:"thinking"} event. Read from the constant rather than
            # restated as a literal: this field is what the CLI gates its thinking UI
            # on, and a hand-maintained copy would drift the moment the posture moves.
            "thinking_enabled": INTERACTIVE_THINKING_ENABLED,
            "multimodal_images": {
                "data_urls": True,
                "remote_urls": settings.allow_remote_image_urls,
            },
            "system_messages_accepted": False,
            # This backend accepts `runtime.display` — the client's measured
            # terminal geometry — and shapes the `# Display context` tail block and
            # the output lint around its `content_width`. It is deliberately NOT in
            # the response contract: that block is cache-stable system text, and a
            # per-request width there re-encodes the tool schemas behind it (#38).
            #
            # A GATE, not a nicety. `runtime` is validated with extra="forbid", so
            # sending the block to a deployment that predates it 422s the WHOLE
            # turn before the model runs; the CLI fails closed and withholds the
            # geometry until it sees this flag. Which is why nothing breaks in
            # either rollout order.
            "display_context": True,
            "max_active_skills": settings.max_active_skills,
            "metadata_transport": "body+stream_meta_event",
            # Every upstream call is funded by the caller's own key, so what a
            # request cost is their money and is reported back to them.
            #
            # `total` sums every upstream call the request made that we could
            # observe — the skill selector and its repair, a losing speculative
            # generation, the main completion, a re-rolled round the user never
            # saw. It is the provider's own reported figure, not a token-price
            # estimate: OpenRouter knows which endpoint served the call and what
            # cache discount applied.
            #
            # Two rules a client must implement, not infer:
            #   * the block is ABSENT rather than zero when nothing was reported,
            #     so a missing block means "unknown", never "free";
            #   * `complete: false` means real spend could not be measured, so
            #     `total` is a floor rather than a sum.
            "cost_reporting": {
                "field": "cost",
                "currency": "USD",
                "components": ["total", "main", "selector", "complete"],
                "stream_event": "done",
                "absent_when_unknown": True,
                # False on `cost.complete` means real spend happened that could
                # not be measured, so `total` is a floor. A session accumulator
                # must render such a total as a lower bound rather than a sum.
                "total_may_be_incomplete": True,
            },
        },
        # The active endpoint-routing posture, and what a caller may change about
        # it. Surfaced because privacy is part of the tester's trust decision and
        # must be visible BEFORE the first turn — not discovered from a doc. The
        # description string is served from PrivacyMode itself so a client cannot
        # invent its own wording (and cannot say "does not store").
        "routing": {
            "privacy_mode": DEFAULT_POLICY.privacy.value,
            "privacy_description": DEFAULT_POLICY.privacy.description,
            "sort": DEFAULT_POLICY.sort.value,
            "client_selectable": {
                "field": "routing",
                "privacy": [mode.value for mode in PrivacyMode],
                "sort": [axis.value for axis in EndpointSort],
                "endpoint_lists": ["only", "ignore"],
                "max_endpoints": MAX_ENDPOINT_LIST,
            },
        },
        "skills": {
            "catalog_revision": container.catalog.revision,
            "manual_resolve": True,
        },
        # Daintree usage questions are answered HERE now: the server classifies
        # them on the selector call it already makes, searches the public docs
        # MCP itself, and hands the pages to the model before it opens. The
        # client no longer needs a docs tool family or a second MCP connection —
        # and MUST NOT keep offering one alongside this, or the model will have
        # two ways to answer the same question and use both.
        #
        # `prelude_tool` is what the search surfaces as: an ordinary entry in
        # `skills.prelude.tool_executions`, the same channel a skill load rides.
        #
        # NOT a claim that it is visible. The CLI decodes `prelude` and does not
        # render it — a newly-active skill reaches the model as body text in the
        # loaded-skills system message rather than as a replayed tool call, which
        # left the prelude vestigial on that side. So a server-side docs lookup is
        # currently INVISIBLE to the user; the field says where it would appear if
        # a client chose to show it, not that one does.
        #
        # Advertised so a client can gate its own docs tooling on it. Best-effort
        # by design: a lookup that fails leaves the turn to answer without
        # documentation, so a client seeing this flag is never promised an answer
        # — only that it does not have to fetch one.
        "docs_lookup": {
            "server_side": True,
            "source": "https://daintree.org",
            "prelude_tool": DOCS_SEARCH_WIRE_NAME,
            "best_effort": True,
        },
        "tasks": container.task_runner.task_ids(),
        # Workflow intelligence: the CLI owns the durable execution graphs; the
        # server contributes the stateless plan/reconcile/resume tasks plus the
        # turn.workflow_state footer rendering. The CLI gates the feature on
        # this block during rollout.
        "workflow_intelligence": {
            "turn_context_field": "workflow_state",
            "tasks": [
                "workflow_plan",
                "workflow_reconcile",
                "workflow_resume_digest",
            ],
            "max_workflow_digests": settings.cap_workflow_digests,
            "max_workflow_state_bytes": settings.cap_workflow_state_bytes,
        },
        # One authenticated call that answers "why isn't this working?" — build,
        # readiness, a live verdict on the caller's key, and catalog identity, in
        # a bundle safe to paste into a support channel. Advertised so the CLI's
        # doctor can use it when present and degrade when it is not.
        #
        # `available_when_not_ready` is the load-bearing part: it tells the CLI
        # this is the ONE endpoint worth calling after another route returns 503,
        # rather than assuming the whole server is unreachable.
        "doctor": {
            "endpoint": "/v1/daintree/doctor",
            "method": "POST",
            "available_when_not_ready": True,
            # NOT an unconditional promise. Every field is allowlisted, but in a
            # non-hardened (development) environment the startup-failure reason
            # is passed through verbatim, and that text can name environment
            # variables and filesystem paths — `/Users/alice/ClientProject/...`
            # is a real thing to leak into a support channel. A hardened
            # environment collapses it to "configuration", which is what makes
            # the bundle genuinely paste-safe there.
            #
            # Reported honestly so the CLI can decide: offer one-click copy when
            # true, and prompt the user to review first when false.
            "safe_to_share": settings.is_hardened,
        },
        "limits": {
            "request_bytes": settings.max_request_bytes,
            "tools": settings.max_tools,
        },
    }
