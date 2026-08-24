"""The Daintree capabilities endpoint (section 13)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from .. import PROTOCOL_MAX, PROTOCOL_MIN, __version__
from ..auth import PERMISSION_RUNBOOKS_READ, Principal, require_permission
from ..config import INTERACTIVE_THINKING_ENABLED, PUBLIC_MODEL_ID
from ..contracts.daintree_api import COMPACTION_BLOCK_NAME
from ..services.docs_events import DOCS_SEARCH_WIRE_NAME
from .deps import require_ready

router = APIRouter()


@router.get("/v1/daintree/capabilities")
async def capabilities(
    request: Request,
    _principal: Principal = Depends(require_permission(PERMISSION_RUNBOOKS_READ)),
    _ready: None = Depends(require_ready),
) -> dict[str, object]:
    container = request.app.state.container
    settings = request.app.state.settings
    # One snapshot, deliberately. `Container.catalog` reads live through
    # `CatalogManager`, which supports atomic replacement, and the revision
    # below is what keys the list beside it — so reading the property twice
    # would be reading a pair that is only meaningful if it comes from one
    # generation. NOT a live race today: nothing in production calls
    # `reload()`, and this handler never awaits, so the async reload's
    # rebind cannot interleave with it. A defensive invariant held cheaply,
    # so that whenever runtime reload does arrive it is already correct.
    catalog = container.catalog
    return {
        "server_version": __version__,
        "protocol": {"min": PROTOCOL_MIN, "max": PROTOCOL_MAX},
        "respond": {
            "endpoint": "/v1/daintree/respond",
            "model": PUBLIC_MODEL_ID,
            "streaming": True,
            # The full event vocabulary a client's parser must handle. `compaction`
            # is listed even where no producer is configured: it is OPTIONAL on
            # every turn anyway (most turns emit none), so a parser that handles
            # it is correct against every deployment, and one that does not would
            # break the day a producer is switched on. Read `context_compaction`
            # below for whether this instance can actually emit it.
            "stream_events": [
                "meta",
                "status",
                "preamble",
                "delta",
                "compaction",
                "done",
                "error",
            ],
            # The fast-response stage. Advertised so a client knows the event
            # exists and what its commit semantics are — NOT which provider or
            # model writes it, which stays internal. `enabled` reports what THIS
            # instance will actually do, because a client that renders a
            # provisional bubble it never receives is worse off than one that
            # never expected it.
            "fast_response": {
                "enabled": settings.fast_response_mode == "visible",
                "streaming_only": True,
                "event": "preamble",
                # The client renders it immediately but must not commit it to
                # conversation history until the stream terminates successfully.
                # On `error` it commits nothing, which is what keeps a failed turn
                # out of canonical history.
                "provisional_until": "done",
                # Preamble and executor output become ONE assistant message,
                # joined by this separator. Fixed rather than negotiated so the
                # committed text is identical everywhere.
                "same_assistant_message": True,
                "join_separator": "\n\n",
                # A preamble is server-generated, idempotent intent text, so
                # seeing one does NOT end the replay-safe window the way a visible
                # executor delta does.
                "retry_safe_before_executor_delta": True,
            },
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
            "max_active_runbooks": settings.max_active_runbooks,
            "metadata_transport": "body+stream_meta_event",
            # What a request cost, when the upstream says. `total` sums every
            # upstream call the request made that we could observe — the main
            # completion, a re-rolled round the user never saw, a utility call.
            # It is the provider's own reported figure, never a token-price
            # estimate computed here.
            #
            # Whether the host reports a cost is unverified, so this block may be absent.
            # That is not a regression to paper over: the two rules below were
            # written for exactly this case and a client that implemented them
            # already behaves correctly. Advertising the capability with
            # `absent_when_unknown` keeps the contract honest and needs no change
            # if a provider that does report cost returns.
            #
            # Two rules a client must implement, not infer:
            #   * the block is ABSENT rather than zero when nothing was reported,
            #     so a missing block means "unknown", never "free";
            #   * `complete: false` means real spend could not be measured, so
            #     `total` is a floor rather than a sum.
            "cost_reporting": {
                "field": "cost",
                "currency": "USD",
                "components": ["total", "main", "fast_response", "selector", "complete"],
                "stream_event": "done",
                "absent_when_unknown": True,
                # False on `cost.complete` means real spend happened that could
                # not be measured, so `total` is a floor. A session accumulator
                # must render such a total as a lower bound rather than a sum.
                "total_may_be_incomplete": True,
            },
        },
        "runbooks": {
            "catalog_revision": catalog.revision,
            "manual_resolve": True,
            # `selection.pinned_runbook_ids` — runbooks the caller requires this turn,
            # admitted ahead of the classifier's own picks. A GATE for the same
            # reason as `display_context` above: `Selection` is extra="forbid", so
            # sending the field to a deployment that predates it 422s the whole turn
            # before the model runs. The CLI withholds it until it sees this.
            "pinned_runbook_ids": True,
            # Every runbook the catalog holds, as the minimal {id, title} reference.
            # A client naming a runbook — a CLI flag, a manual `resolve` load — can
            # now check the id BEFORE it spends a turn, and offer near misses. The
            # server is not silent about a bad id (`load` warns
            # `unknown_runbook_id_ignored`), but that arrives after the turn ran, in
            # a response field, with no idea what the user meant; preflight is a
            # different thing from a post-hoc warning.
            #
            # Cache it against `catalog_revision` above — same snapshot. That is
            # CONSERVATIVE rather than exact: the revision hashes every runbook's
            # metadata AND body, so a body-only edit moves it while this list stays
            # byte-identical. Re-fetching more often than strictly needed is the
            # right side to err on for a ~3.5KB array, and a second revision
            # covering only ids and titles would be a version to maintain.
            #
            # Minimal on purpose. `/v1/daintree/runbooks` already serves the full
            # metadata (summary, when_to_use, risk, required_tools) with an ETag;
            # repeating ~53KB of it here would make every capabilities call pay for
            # what validation does not read. Ordering is deterministic for free —
            # `Catalog.runbooks` is sorted by id at construction.
            #
            # The CANONICAL catalog, not a menu: capabilities carries neither a
            # profile nor a tool inventory, so it cannot honestly narrow to what a
            # sub-agent could execute. Server-pinned runbooks are included because
            # manual `load` accepts them (`filter_known`, not `selectable_ids`) —
            # a listing that hid them would lie about what the client may name.
            "catalog": [runbook.ref() for runbook in catalog.runbooks],
        },
        # Daintree usage questions are answered HERE now: the server classifies
        # them on the selector call it already makes, searches the public docs
        # MCP itself, and hands the pages to the model before it opens. The
        # client no longer needs a docs tool family or a second MCP connection —
        # and MUST NOT keep offering one alongside this, or the model will have
        # two ways to answer the same question and use both.
        #
        # `prelude_tool` is what the search surfaces as: an ordinary entry in
        # `runbooks.prelude.tool_executions`, the same channel a runbook load rides.
        #
        # NOT a claim that it is visible. The CLI decodes `prelude` and does not
        # render it — a newly-active runbook reaches the model as body text in the
        # loaded-runbooks block rather than as a replayed tool call, which
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
        # Context compaction: the model declares on its first line whether the
        # reply ends the turn, the server strips that line before any byte reaches
        # the client, and on a final reply it reconciles settled history into one
        # block while the rest of the message is still streaming.
        #
        # `enabled` is the honest answer for THIS instance, read from the compactor
        # the container actually holds — false while no producer is configured,
        # which is every deployment until the second provider lands. The stripping
        # happens either way; only the block does not.
        #
        # `delivery` is the load-bearing field. The event arrives BEFORE `done`,
        # never after: `done` stays terminal so a reader that stops on it loses
        # nothing. A client applies the splice only when `turn_id` matches the turn
        # it asked about, and applies it exactly as
        # `messages[:start_index] + [block] + messages[end_index:]`.
        #
        # Best-effort, like the docs lookup: a client seeing this block is never
        # promised a block on any given turn, only told what to do with one.
        "context_compaction": {
            "enabled": container.compactor.available,
            "stream_event": "compaction",
            "delivery": "before_done",
            "at_most_once": True,
            # Streaming only. The whole design is a call overlapped with
            # generation, and a non-streamed turn is one await with no generation
            # left to overlap — so that path strips the declaration and stops.
            "streaming_only": True,
            "best_effort": True,
            "append_only": True,
            # The reserved `name` the spliced message carries. Sending it back
            # verbatim is what lets the next turn recognise already-frozen history
            # without any server-side state — and what stops it being compacted a
            # second time.
            "block_message_name": COMPACTION_BLOCK_NAME,
            "span": {
                "collection": "input.messages",
                "index_base": 0,
                "end_exclusive": True,
                # The assistant reply currently streaming is not in that array; it
                # is appended after the splice, as usual.
                "excludes_current_reply": True,
            },
            # Apply the splice only when this matches the turn asked about. A block
            # is a statement about one conversation prefix at one moment.
            "turn_id_match_required": True,
            # `block.content` specifically, in UTF-8 bytes — not the block, and not
            # the event. Named for what it measures because a client sizing the
            # wrong thing rejects blocks it should have applied.
            "max_block_content_bytes": settings.cap_compaction_block_bytes,
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
