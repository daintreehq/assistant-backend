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

# Hardcoded model identities — a product decision, not deployment config. The
# main completion, the skill selector, the utility tasks, and the skill-learning
# gate all run on the small model (OPENROUTER_MODEL) — DeepSeek V4 Flash, reached
# through OpenRouter rather than DeepSeek directly, so the fastest compliant
# endpoint serving those weights answers each call (see providers/openrouter.py).
# The date-pinned slug is deliberate: the floating alias would silently re-point
# at a differently-tuned revision, and every prompt and benchmark here is
# calibrated against this one. The skill-learning builder and improver run on a
# deliberately STRONGER model — GPT-5.6 Sol — for the harder "write a whole
# skill" generation task; routing everything through OpenRouter is what makes
# that a one-line model swap on the same provider and the same key, instead of a
# second SDK and a second credential. PUBLIC_MODEL_ID is the alias returned to
# clients. Only the provider API key (and base URL) comes from the environment.
OPENROUTER_MODEL = "deepseek/deepseek-v4-flash-0731"
SKILL_BUILDER_MODEL = "openai/gpt-5.6-sol"
SKILL_IMPROVER_MODEL = "openai/gpt-5.6-sol"
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
# THE INTERACTIVE SURFACE IS NON-THINKING. The main completion, every utility
# task, and the skill selector all run with reasoning explicitly disabled. This
# workload does not need it: the main model's job is to route a short request
# onto a skill runbook and emit tool calls, and the utility tasks are bounded
# extractions and classifications against text the client already gathered. A
# measured greeting turn (2026-08-08, turn_2c1bbc09) spent 366 reasoning tokens
# and ~3.5s of a 11.5s wall-clock thinking about "Hello!".
#
# There is NO middle setting to retreat to. DeepSeek V4 Flash collapses the
# effort tiers: measured over 5 samples each on a fixed prompt, "low" and "high"
# produced statistically identical chain-of-thought (mean 662 vs 656 reasoning
# tokens, fully overlapping ranges). "low" buys nothing, so the only real step
# below "high" is off — which is what this posture takes.
#
# Two constraints outlive the posture and stay enforced independently, so that
# flipping the flag back on cannot reintroduce a known failure:
#   * A main call whose client pinned a forced tool_choice must stay non-thinking
#     regardless — DeepSeek 400s on thinking + forced tool_choice.
#   * The selector is separately hardcoded off (skills/selector.py): it gates the
#     turn's first byte, and thinking there benchmarked strictly worse (median
#     ~4-5s with a 13-20s timeout-retry tail vs a consistent 1.2-2.6s) for no
#     selection-quality gain once its prompt asked for reason-first JSON.
#
# SKILL LEARNING STILL THINKS. It is local-dev-only and runs fire-and-forget
# AFTER the response is sent, so its latency never reaches a user, and "read a
# whole transcript and write a skill" is the one genuinely hard task here. The
# gate runs thinking-high on the small model; the builder/improver on GPT-5.6 Sol
# likewise pin "high" — its xhigh top tier is excluded on purpose, high already
# covers the task.
INTERACTIVE_THINKING_ENABLED = False
# The effort sent whenever this server DOES think on the small model. With the
# posture above that is the skill-learning gate only.
OPENROUTER_REASONING_EFFORT = "high"
SKILL_LEARNING_REASONING_EFFORT = "high"

# The development default salt; rejected in production by weak_secrets().
DEFAULT_STATE_SALT = "daintree-state-salt"


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

    # ---- Upstream provider (OpenRouter, OpenAI-compatible) ---------------
    # Models are hardcoded (see the constants above) and so is the endpoint-
    # selection policy (throughput-sorted, no-data-collection — see
    # ``_PROVIDER_ROUTING`` in providers/openrouter.py); only the key and base URL
    # come from the environment.
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )
    # Benchmark / live-test tooling ONLY. The serving path holds no upstream
    # credential at all: every request's provider calls use the caller's own key
    # (their bearer token — see auth.py), and main.py builds the server provider
    # with api_key=None so a propagation bug fails loudly instead of quietly
    # billing a shared account. Standalone tooling (benchmarks/, tests/live)
    # still constructs providers directly from this value.
    openrouter_api_key: SecretStr = Field(default=SecretStr(""), alias="OPENROUTER_API_KEY")

    # ---- Thinking headroom -----------------------------------------------
    # The thinking POSTURE itself is hardcoded (see INTERACTIVE_THINKING_ENABLED at
    # the top of this module): main/utility/selector are non-thinking, only the
    # skill-learning gate and builder/improver still think. reasoning_content
    # round-trips through the wire contract so a tool-call turn can replay the
    # chain-of-thought that produced its calls (the provider adapter renames it to
    # OpenRouter's `reasoning` on the way out), which the assembler preserves —
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
    thinking_headroom_tokens: int = Field(
        default=4000, ge=0, alias="THINKING_HEADROOM_TOKENS"
    )

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
    # ONE HOP ONLY: backend -> OpenRouter. Applies to every upstream call (main
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

    # ---- Skill-selection speculation -----------------------------------
    # On a streamed new instruction with a verified, catalog-current, non-empty
    # active skill set, the main stream can start privately alongside the
    # authoritative selector. If the selector preserves the exact ordered set,
    # that stream becomes canonical; otherwise it is closed and discarded. A
    # zero concurrency cap is the deployment kill switch. The private producer
    # applies both chunk and serialized-byte backpressure while selection is
    # pending; an oversized indivisible event abandons the private attempt.
    skill_speculation_max_concurrency: int = Field(
        default=8, ge=0, alias="SKILL_SPECULATION_MAX_CONCURRENCY"
    )
    skill_speculation_buffer_chunks: int = Field(
        default=128, ge=1, alias="SKILL_SPECULATION_BUFFER_CHUNKS"
    )
    skill_speculation_buffer_bytes: int = Field(
        default=1024 * 1024, ge=1024, alias="SKILL_SPECULATION_BUFFER_BYTES"
    )
    # ``complete()`` starts speculation before it returns the public async
    # iterator. If that iterator is never consumed (disconnect before body
    # iteration), this deadline transfers ownership back to the server and closes
    # the otherwise-orphaned upstream stream. A late consumer opens canonically.
    skill_speculation_claim_timeout_seconds: float = Field(
        default=5.0, gt=0.0, alias="SKILL_SPECULATION_CLAIM_TIMEOUT_SECONDS"
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

    # ---- Context field caps (bytes, unless noted) -----------------------
    # Session-stable agent registry rendered before the visible conversation.
    # Rows are admitted whole so an exact agent id is never truncated into a
    # different, unusable identifier.
    cap_startup_agents: int = Field(default=512, alias="CAP_STARTUP_AGENTS")
    cap_startup_agent_bytes: int = Field(
        default=16 * 1024, alias="CAP_STARTUP_AGENT_BYTES"
    )
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
    cap_workflow_state_bytes: int = Field(
        default=16 * 1024, alias="CAP_WORKFLOW_STATE_BYTES"
    )
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

    # ---- Skill learning (LOCAL DEVELOPMENT ONLY) ------------------------
    # A backend-internal harness that watches completed local workflows and, when
    # warranted, writes or improves skill files so the catalog "learns". It is
    # entirely invisible to the CLI: no new wire fields, no feedback tool, and the
    # main assistant model is never told it exists — a created skill simply appears
    # as a normal dynamic skill load. It MUST be off in production: an enabled mode
    # fails readiness there (see local_only_problems). Modes:
    #   off     — disabled (default)
    #   propose — run the gate (small model) + the gpt-5.6-sol builder/improver and write
    #             the proposed skill markdown + an audit JSON under the audit dir's
    #             proposals/, but never touch the real skill files or the catalog
    #   apply   — also write/rewrite the skill file and reload the catalog in place
    skill_learning_mode: Literal["off", "propose", "apply"] = Field(
        default="off", alias="SKILL_LEARNING_MODE"
    )
    # Where created/improved skills are written. Empty → the packaged skills dir
    # (edit-in-place: improvements show up in `git diff`, ready to commit). Set to a
    # separate directory to keep the source tree untouched — the catalog then loads
    # packaged skills PLUS this overlay, and overlay skills win on an id collision.
    skill_learning_dir: str = Field(default="", alias="SKILL_LEARNING_DIR")
    # Per-change audit JSON records (old/new hash, gate verdict, diff) land here.
    skill_learning_audit_dir: str = Field(
        default=".daintree/skill-learning", alias="SKILL_LEARNING_AUDIT_DIR"
    )
    # The builder/improver reasoning tier (GPT-5.6 Sol) and the gate's thinking-high
    # posture (small model, json_object verdict) are hardcoded — see
    # SKILL_LEARNING_REASONING_EFFORT / OPENROUTER_REASONING_EFFORT at the top of
    # this module. Learning is the one surface that still thinks: it runs
    # fire-and-forget after the response, so its latency never reaches a user.
    # Gate verdicts below this confidence never write.
    skill_learning_min_confidence: float = Field(
        default=0.75, ge=0.0, le=1.0, alias="SKILL_LEARNING_MIN_CONFIDENCE"
    )
    # Whether the learner may MINT a new foundation skill or PROMOTE a specific one to
    # foundation. Off by default: a foundation is near-always paired into selection, so
    # tiering one wrong is costly — promotion is a human curation decision. The learner
    # can always IMPROVE (and grow) an EXISTING foundation regardless of this flag; this
    # only gates turning `foundation` ON where it was off. See skill_learning.py._validate.
    skill_learning_allow_foundation_create: bool = Field(
        default=False, alias="SKILL_LEARNING_ALLOW_FOUNDATION_CREATE"
    )
    # Learning is only considered once the conversation has at least this many
    # messages — there must be prior work to learn from before the gate runs.
    skill_learning_min_messages: int = Field(
        default=3, ge=1, alias="SKILL_LEARNING_MIN_MESSAGES"
    )
    # Transcript token budget the gate/builder see. Learning runs full detection over
    # the whole (newest-biased) message history — cost is not a concern in this local
    # harness — so this is much larger than the selector's context budget.
    skill_learning_context_tokens: int = Field(
        default=200000, alias="SKILL_LEARNING_CONTEXT_TOKENS"
    )
    # Per-call timeout for the learning model calls. The build/improve call thinks
    # at high effort and must emit a COMPLETE skill body — minutes, not seconds —
    # and it runs off the response path, so a generous timeout costs nothing.
    skill_learning_timeout_seconds: float = Field(
        default=300.0, alias="SKILL_LEARNING_TIMEOUT_SECONDS"
    )
    skill_gate_max_output_tokens: int = Field(
        default=700, alias="SKILL_GATE_MAX_OUTPUT_TOKENS"
    )
    # Must cover the improver's REQUIRED output — the complete replacement body
    # (the largest packaged skill is ~24 KB ≈ 6k tokens before JSON escaping) plus
    # the diagnosis fields plus the builder's reasoning tokens. It bounds reasoning
    # AND visible output together (OpenRouter maps it to `max_completion_tokens` for
    # endpoints that need that spelling), so high-effort reasoning eats into this
    # budget. Too small and the JSON
    # truncates: the parse fails, the repair retry truncates identically, and the
    # improve path can never land.
    skill_build_max_output_tokens: int = Field(
        default=24000, alias="SKILL_BUILD_MAX_OUTPUT_TOKENS"
    )

    # ---- Derived helpers ------------------------------------------------

    @property
    def skill_learning_enabled(self) -> bool:
        """True when skill learning runs at all (``propose`` or ``apply``)."""
        return self.skill_learning_mode != "off"

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

    def missing_secrets(self) -> list[str]:
        """Return env var names of required secrets that are empty.

        Only server-owned material is required. Neither upstream nor inbound API
        keys appear here: every request supplies its own caller key, which is
        both the auth credential and the upstream credential, so there is
        nothing key-related for the server to validate at startup.
        """
        missing: list[str] = []
        if not self.state_signing_key_current.get_secret_value():
            missing.append("STATE_SIGNING_KEY_CURRENT")
        return missing

    def local_only_problems(self) -> list[str]:
        """Local-development-only features that must never run in production, plus
        their required config.

        These join ``missing_secrets()`` / ``weak_secrets()`` in readiness gating,
        so a production (or Cloud Run) deploy that leaves skill learning enabled
        fails ``/readyz`` rather than mutating the catalog from model output at
        runtime. Empty in development: the builder/improver run on the SAME
        OpenRouter key as everything else, so enabling learning needs no extra
        credential of its own.
        """
        problems: list[str] = []
        if self.is_hardened and self.skill_learning_enabled:
            problems.append("SKILL_LEARNING_MODE must be 'off' in production")
        if self.is_hardened and self.dump_upstream_dir:
            problems.append(
                "DAINTREE_DUMP_UPSTREAM_DIR must be unset in production "
                "(prompt dumping is development-only)"
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
        # The upstream base URL now receives CALLER credential material on every
        # request (each turn's bearer key rides the Authorization header to this
        # origin). A hardened deployment with a typo'd, plain-http, or foreign
        # host would silently disclose every caller key while staying green, so
        # production pins the real OpenRouter origin. Development stays free to
        # point at proxies/mocks.
        try:
            parsed = urlsplit(self.openrouter_base_url)
            origin_ok = (
                parsed.scheme == "https"
                and parsed.hostname == "openrouter.ai"
                # Default port only: a non-443 port is a different origin, and a
                # malformed one should fail readiness, not the first request.
                and parsed.port in (None, 443)
                # No userinfo — `https://x@openrouter.ai` still reaches OpenRouter,
                # but credentials do not belong in this URL in any form.
                and parsed.username is None
            )
        except ValueError:  # e.g. a non-numeric port
            origin_ok = False
        if not origin_ok:
            problems.append(
                "OPENROUTER_BASE_URL must be the https://openrouter.ai origin in production "
                "(caller API keys are sent to it)"
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
