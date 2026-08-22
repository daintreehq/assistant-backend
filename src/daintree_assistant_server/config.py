"""Service configuration.

Settings are parsed once at startup into an immutable object. Secret values
use :class:`~pydantic.SecretStr` so they never appear in logs, tracebacks, or
``repr`` output. Required secrets default to empty strings here and are
validated by the readiness path (:meth:`Settings.missing_secrets`) so the
process fails *readiness* — not import — when a secret is absent.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# THE model identity — singular, and a product decision rather than deployment
# config. The main completion and every utility task run on SERVING_MODEL, at the
# host SERVING_HOST names (both a few lines down). Either host is a model HOST
# rather than a router, so one endpoint serves the weights and there is nothing to
# select between. Skill selection makes no upstream call at all — it is the local
# ONNX classifier in skills/local/.
#
# There is exactly ONE model now. The second one existed for skill learning,
# which has been removed: a background pass that spent a large thinking budget
# rewriting the catalog from model output, for a catalog a human curates anyway.
#
# Gemma 4 31B is a PREVIEW model and Cerebras has said it will be replaced. When
# that happens this constant is the whole code change: the prompts, budgets and
# benchmarks are calibrated against it, so the swap is a deliberate re-baseline
# (benchmarks/baseline.json), not a floating alias that re-points underneath us.
#
# PUBLIC_MODEL_ID is the alias returned to clients. Only the provider API key and
# base URL come from the environment.
CEREBRAS_MODEL = "gemma-4-31b"

# The Together-hosted alternative, and THE serving model today. DeepSeek V4 Flash
# is the same weights `context_compact` has always used; what changed is the host.
# OpenRouter is a router and this server sends it no routing preferences (there is
# no `provider` block and policy.py is gone), so its endpoint choice is whatever
# the default is that minute. Replaying the 62 real utility-task payloads:
# Together p50 1100ms / p90 1702ms against OpenRouter's 3414ms / 6013ms — same
# model, same prompts, 3.1x. The host, not the model, was the latency.
TOGETHER_MODEL = "deepseek-ai/DeepSeek-V4-Flash-0731"

# ---- THE serving identity — one host, one model, both hardcoded ------------
# Switching hosts is these two lines and nothing else: the adapters are picked
# from SERVING_HOST in main.py, readiness gates the matching credential, and
# every model call (main completion and all 13 utility tasks) uses SERVING_MODEL.
# Set them back to ("cerebras", CEREBRAS_MODEL) to return to Gemma 4 31B, which
# is the move to make when a Cerebras model worth having lands.
SERVING_HOST: Literal["together", "cerebras"] = "together"
SERVING_MODEL = TOGETHER_MODEL

#: Per-host names for the serving credential, its base URL, and the origin that
#: URL must resolve to in production. Keyed by SERVING_HOST so readiness reports
#: the variable the operator actually has to set.
_SERVING_KEY_ENV = {"together": "TOGETHER_API_KEY", "cerebras": "CEREBRAS_API_KEY"}
_SERVING_URL_ENV = {"together": "TOGETHER_BASE_URL", "cerebras": "CEREBRAS_BASE_URL"}
_SERVING_ORIGIN = {"together": "api.together.xyz", "cerebras": "api.cerebras.ai"}

#: Resolved for the current host. Readiness messages and the tests that pin the
#: credential model both read these rather than a vendor name, so switching
#: SERVING_HOST moves the whole posture instead of leaving assertions behind
#: that still describe the host we left.
SERVING_KEY_ENV = _SERVING_KEY_ENV[SERVING_HOST]
SERVING_URL_ENV = _SERVING_URL_ENV[SERVING_HOST]
SERVING_ORIGIN = _SERVING_ORIGIN[SERVING_HOST]

PUBLIC_MODEL_ID = "daintree-assistant"

# The runbook loaded whenever a turn produces a `docs_query` — i.e. whenever the
# server is about to search the documentation MCP. SERVER-PINNED, not classified:
# by the time this matters the selector has ALREADY answered "is this a
# documentation question", so routing the matching runbook through a SECOND
# judgement over the same evidence is a coin flip about a fact already in hand.
# The same argument as foundation pairing, one step earlier.
#
# Because it is pinned, it is also HIDDEN FROM THE SELECTOR (`Catalog.selector_metadata`
# omits it): a skill the server always supplies is one the classifier should never
# spend prompt bytes reading or risk mis-picking on an operational turn. That
# hiding is what keeps the two decisions from disagreeing.
#
# An id the catalog does not contain is a silent no-op — `resolve_active_skills`
# filters unknown ids — so deleting the skill file degrades to the previous
# behaviour rather than breaking the turn.
DOCS_SKILL_ID = "daintree.docs.answer-usage-question"

# Thinking posture — hardcoded like the model ids, deliberately NOT env-config.
# There is one optimum and the server ships it.
#
# NOTHING IN THIS SERVER THINKS, and on Cerebras that is now a property of the
# model rather than a flag held down. Gemma 4 31B does not reason at all
# (measured `reasoning_tokens: 0` on every probe), and the wire has no field that
# could ask it to — Cerebras takes a bare `reasoning_effort` string with no "off"
# value, and rejects OpenRouter's `reasoning` object outright. So a main or
# utility call simply sends nothing, and cannot regress into spending 366
# reasoning tokens and ~3.5s of an 11.5s wall-clock deciding how to answer
# "Hello!" (measured 2026-08-08, turn_2c1bbc09, on the previous stack).
#
# Skill learning used to be the one exception — it ran fire-and-forget after the
# response, so its latency never reached a user. It has been removed, so there is
# no thinking surface left at all.
INTERACTIVE_THINKING_ENABLED = False
# The tier the interactive surface would send if that posture were ever flipped
# back on. Kept beside the flag rather than inlined at the one call site that
# reads it (services/task_runner.py) so the pair stays legible as a single
# decision — and so flipping the flag does not also require choosing a tier.
INTERACTIVE_REASONING_EFFORT = "high"

# The development default salt; rejected in production by weak_secrets().
DEFAULT_STATE_SALT = "daintree-state-salt"


#: The three tool-projection postures. Declared HERE rather than beside the
#: projector because config sits below skills in the import graph — the
#: catalog reads DOCS_SKILL_ID from this module, so the arrow cannot reverse.
ToolProjectionMode = Literal["disabled", "shadow", "enforced"]


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
    # the one direction that matters. (Authentication itself no longer varies by
    # environment: every request needs a caller key everywhere — see auth.py.)
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

    # ---- Upstream provider (Cerebras, OpenAI-compatible) -----------------
    # Models are hardcoded (see the constants above). Only the key and base URL
    # come from the environment.
    cerebras_base_url: str = Field(default="https://api.cerebras.ai/v1", alias="CEREBRAS_BASE_URL")
    # THE server's upstream credential, and the only one there is. Daintree funds
    # every turn from this key: callers no longer supply their own (see auth.py),
    # so this is required for the server to answer anything and is gated by
    # ``missing_secrets``. Local dev reads it from .env; Cloud Run injects it from
    # Secret Manager.
    cerebras_api_key: SecretStr = Field(default=SecretStr(""), alias="CEREBRAS_API_KEY")

    # ---- Second provider (OpenRouter) — OPTIONAL, and deliberately not gating.
    # Funds the ONE utility task that does not run on Cerebras: context
    # compaction, which routes to a model an order of magnitude cheaper per input
    # token than the serving model precisely because it re-reads conversation
    # history (see OPENROUTER_COMPACTION_MODEL in services/task_runner.py).
    #
    # It is absent from ``missing_secrets`` ON PURPOSE, and that is the whole
    # difference between the two credentials. Without CEREBRAS_API_KEY nothing
    # can be answered at all, so its absence must fail readiness. Without this
    # one everything still works except a single task that nothing on the serving
    # path calls yet — so a deployment that never sets it is a correct
    # deployment, not a broken one. Gating readiness on it would take the whole
    # server down over a feature it is not using.
    #
    # There is no OPENROUTER_BASE_URL to match ``cerebras_base_url``: that one
    # exists so tests and local proxies can redirect the credential-bearing
    # serving path, and ``weak_secrets`` pins it back to the real origin in
    # production. This provider is constructed against its module default and
    # nothing has asked to move it.
    openrouter_api_key: SecretStr = Field(default=SecretStr(""), alias="OPENROUTER_API_KEY")

    # ---- Together AI — the SERVING host today (see SERVING_HOST above) ----
    # Same shape as the Cerebras pair above and for the same reason: this key
    # funds every turn, so `missing_secrets` gates readiness on it whenever
    # Together is the serving host, and `weak_secrets` pins the base URL back to
    # the real origin in production because the key is sent there.
    together_base_url: str = Field(
        default="https://api.together.xyz/v1", alias="TOGETHER_BASE_URL"
    )
    together_api_key: SecretStr = Field(default=SecretStr(""), alias="TOGETHER_API_KEY")

    # ---- Thinking headroom -----------------------------------------------
    # The thinking POSTURE itself is hardcoded (see INTERACTIVE_THINKING_ENABLED at
    # the top of this module): main/utility/selector are non-thinking, only the
    # skill-learning gate and builder/improver still think. reasoning_content
    # round-trips through the wire contract so a tool-call turn can replay the
    # chain-of-thought that produced its calls (the provider adapter renames it to
    # the wire's `reasoning` on the way out), which the assembler preserves —
    # that path stays live because the posture is a flag, not a deletion. Thinking
    # mode ignores temperature/top_p.
    #
    # Because max_tokens is SHARED by reasoning + visible output, a small-output
    # THINKING call adds this headroom to its tight output cap so the
    # chain-of-thought can't starve the answer into a finish_reason=length
    # truncation. Under the current posture the skill-learning gate is the only
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
    # Floored at 2 deliberately: every skill resolves to a domain foundation, and the
    # orchestrator admits a specific and its foundation as one atomic unit. A budget of 1
    # can never fit that unit, so a cap of 1 would silently load NOTHING rather than
    # loading less. This subsumes the earlier ge=0 guard against a cap of 0 admitting one
    # id anyway; max_meta_header_bytes was pruned on main as dead.
    max_active_skills: int = Field(default=3, ge=2, alias="MAX_ACTIVE_SKILLS")

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
    # reason-first key order puts skill_ids LAST in the JSON, so a length
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
    # ONE HOP ONLY: backend -> Cerebras. Applies to every upstream call (main
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

    # ---- Skill-aware tool projection ------------------------------------
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

    # ---- Skill overlay directory (LOCAL DEVELOPMENT ONLY) ----------------
    # A second directory of skill files merged OVER the packaged catalog: a new
    # id is added, a colliding id replaces the packaged skill. It exists so a
    # skill can be authored in a scratch directory against a running server —
    # POST /v1/daintree/skills/reload rebuilds the catalog without a restart.
    # The path is read once, at startup; only its CONTENTS are re-read on
    # reload. A set value in production fails readiness (see
    # local_only_problems) and the reload route is not mounted there at all: a
    # deploy must serve the catalog its image shipped, not whatever happens to
    # be on the instance's disk.
    skills_overlay_dir: str | None = Field(default=None, alias="DAINTREE_SKILLS_OVERLAY_DIR")

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

    # ---- Skill learning (LOCAL DEVELOPMENT ONLY) ------------------------
    # A backend-internal harness that watches completed local workflows and, when
    # warranted, writes or improves skill files so the catalog "learns". It is
    # entirely invisible to the CLI: no new wire fields, no feedback tool, and the
    # main assistant model is never told it exists — a created skill simply appears
    # as a normal dynamic skill load. It MUST be off in production: an enabled mode
    # ---- Derived helpers ------------------------------------------------

    @property
    def is_hardened(self) -> bool:
        """True where production hardening applies: no local-only features, weak
        secrets rejected, the upstream origin pinned, internal detail withheld
        from responses. (Authentication is NOT environment-dependent: every
        request needs a caller key everywhere — see auth.py.)

        The single place that decides "is this a real deployment?", so the checks
        below and ``/readyz`` cannot drift apart.
        """
        return self.environment in ("production", "staging")

    @property
    def serving_api_key(self) -> SecretStr:
        """The credential that funds every turn, per SERVING_HOST."""
        return (
            self.together_api_key if SERVING_HOST == "together" else self.cerebras_api_key
        )

    @property
    def serving_base_url(self) -> str:
        """The origin the serving credential is sent to, per SERVING_HOST."""
        return (
            self.together_base_url if SERVING_HOST == "together" else self.cerebras_base_url
        )

    def missing_secrets(self) -> list[str]:
        """Return env var names of required secrets that are empty.

        Both entries are server-owned, and that is the whole shape of the current
        credential model: Daintree holds the upstream key and callers hold none
        (see auth.py). ``CEREBRAS_API_KEY`` is therefore required for the server
        to answer anything at all, and its absence must fail READINESS rather than
        surface a turn later as an upstream 401 that reads like the caller's
        fault. There is still no INBOUND key to configure — the door is open by
        design until account auth lands.
        """
        missing: list[str] = []
        if not self.state_signing_key_current.get_secret_value():
            missing.append("STATE_SIGNING_KEY_CURRENT")
        if not self.serving_api_key.get_secret_value():
            missing.append(SERVING_KEY_ENV)
        return missing

    def local_only_problems(self) -> list[str]:
        """Local-development-only features that must never run in production.

        These join ``missing_secrets()`` / ``weak_secrets()`` in readiness gating.
        Two entries: prompt dumping, which writes full prompts to disk, and the
        skill overlay directory, which would let a production instance serve a
        catalog its image never shipped. (Skill learning used to be here too,
        and was the reason this method exists — a production deploy that left it
        on would have mutated the skill catalog from model output at runtime. It
        has been removed entirely. The overlay is a much smaller thing: it only
        reads files a developer deliberately put there. Gated anyway, because
        "which skills is production actually running?" must have one answer.)
        """
        problems: list[str] = []
        if self.is_hardened and self.dump_upstream_dir:
            problems.append(
                "DAINTREE_DUMP_UPSTREAM_DIR must be unset in production "
                "(prompt dumping is development-only)"
            )
        if self.is_hardened and self.skills_overlay_dir:
            problems.append(
                "DAINTREE_SKILLS_OVERLAY_DIR must be unset in production "
                "(the skill overlay is development-only)"
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
        # The upstream base URL receives THE SERVER's credential on every request.
        # A hardened deployment with a typo'd, plain-http, or foreign host would
        # disclose the key that funds every Daintree turn while staying green, so
        # production pins the real Cerebras origin. Development stays free to
        # point at proxies/mocks.
        try:
            parsed = urlsplit(self.serving_base_url)
            origin_ok = (
                parsed.scheme == "https"
                and parsed.hostname == SERVING_ORIGIN
                # Default port only: a non-443 port is a different origin, and a
                # malformed one should fail readiness, not the first request.
                and parsed.port in (None, 443)
                # No userinfo — `https://x@api.cerebras.ai` still reaches Cerebras,
                # but credentials do not belong in this URL in any form.
                and parsed.username is None
            )
        except ValueError:  # e.g. a non-numeric port
            origin_ok = False
        if not origin_ok:
            problems.append(
                f"{SERVING_URL_ENV} must be the "
                f"https://{SERVING_ORIGIN} origin in production "
                "(the server API key is sent to it)"
            )
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
