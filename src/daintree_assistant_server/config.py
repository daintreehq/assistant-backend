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

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Hardcoded model identities — a product decision, not deployment config. The
# main completion, the skill selector, the utility tasks, and the skill-learning
# gate all run on the small model (DEEPSEEK_MODEL). The skill-learning builder and
# improver run on OpenAI's GPT-5.5 reasoning model — a harder "write a whole skill"
# generation task, deliberately on a stronger, separate provider (see the OpenAI
# settings below and providers/openai.py). PUBLIC_MODEL_ID is the alias returned to
# clients. Only the provider API keys (and base URLs) come from the environment.
DEEPSEEK_MODEL = "deepseek-v4-flash"
SKILL_BUILDER_MODEL = "gpt-5.5"
SKILL_IMPROVER_MODEL = "gpt-5.5"
PUBLIC_MODEL_ID = "daintree-assistant"

# Thinking posture — hardcoded like the model ids, deliberately NOT env-config.
# There is one optimum and the server ships it: every DeepSeek call runs
# thinking-high EXCEPT (a) the skill selector — it gates the turn's first byte,
# and thinking there benchmarked strictly worse (median ~4-5s with a 13-20s
# timeout-retry tail vs a consistent 1.2-2.6s non-thinking) for no selection-
# quality gain once its prompt asked for reason-first JSON (task_type/reason as
# working notes BEFORE skill_ids) — and (b) a main call whose client pinned a
# forced tool_choice, which DeepSeek rejects under thinking, so that request
# degrades to non-thinking at runtime. "high" is DeepSeek's lightest tier
# (low/medium silently map up to it; the only step below is off). The
# skill-learning builder/improver on GPT-5.5 likewise pin "high" — its xhigh
# top tier is excluded on purpose: high already covers the "write a whole
# skill" task, and learning runs only in local dev.
DEEPSEEK_REASONING_EFFORT = "high"
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
    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    build_sha: str = Field(default="", alias="BUILD_SHA")

    # ---- Bind address (honored by `python -m daintree_assistant_server`) -
    # Cloud Run injects PORT=8080; local dev sets a fixed port in .env so the
    # CLI can always target it.
    host: str = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=8080, alias="PORT")

    # ---- Upstream provider (DeepSeek, OpenAI-compatible) ---------------
    # Models are hardcoded (see the constants above); only the key and base URL
    # come from the environment.
    deepseek_base_url: str = Field(default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")
    deepseek_api_key: SecretStr = Field(default=SecretStr(""), alias="DEEPSEEK_API_KEY")

    # DeepSeek V4 specifics, confirmed against the live endpoint:
    #  - thinking must be DISABLED for any forced tool_choice to work
    #    ("Thinking mode does not support this tool_choice"); the field is accepted.
    #  - prompt_cache_key is accepted and improves prefix-cache routing.
    # Defaults ON; flip off only for a provider/model that rejects the field. (The
    # provider's explicit per-call thinking directive is NOT configurable — DeepSeek
    # defaults reasoning ON when the field is absent, and the explicit disable is
    # what keeps the non-thinking selector actually non-thinking.)
    deepseek_send_prompt_cache_key: bool = Field(
        default=True, alias="DEEPSEEK_SEND_PROMPT_CACHE_KEY"
    )

    # ---- Skill-learning provider (OpenAI, GPT-5.5) ----------------------
    # The skill-learning builder/improver run on OpenAI's GPT-5.5 reasoning model
    # (SKILL_BUILDER_MODEL/SKILL_IMPROVER_MODEL above), NOT DeepSeek — a deliberately
    # stronger, separate provider for the hard "write a whole skill" generation task.
    # Only needed when skill learning is enabled (local dev); the key is then REQUIRED
    # at readiness (see local_only_problems). GPT-5.5 is a reasoning model: the OpenAI
    # adapter sends `reasoning_effort` + `max_completion_tokens` and omits temperature
    # (the model rejects any non-default value) — see providers/openai.py.
    openai_base_url: str = Field(
        default="https://api.openai.com/v1", alias="OPENAI_BASE_URL"
    )
    openai_api_key: SecretStr = Field(default=SecretStr(""), alias="OPENAI_API_KEY")

    # ---- DeepSeek thinking headroom --------------------------------------
    # The thinking POSTURE itself is hardcoded (see DEEPSEEK_REASONING_EFFORT at
    # the top of this module): main/utility/gate think-high, the selector is
    # non-thinking, and only a client-pinned forced tool_choice degrades the main
    # call off at runtime. reasoning_content round-trips through the wire contract
    # (an assistant tool-call turn that omits it 400s on DeepSeek), which the
    # assembler preserves. Thinking mode ignores temperature/top_p.
    #
    # Because DeepSeek's max_tokens is SHARED by reasoning + visible output, the
    # small-output thinking calls (utility tasks, skill-learning gate) add this
    # headroom to their tight output caps so the chain-of-thought can't starve the
    # answer into a finish_reason=length truncation. The main /respond call does
    # NOT use this: it runs on a single flat budget (MAIN_DEFAULT_MAX_TOKENS ==
    # MAIN_MAX_TOKENS_CEILING, already sized for reasoning + output and clamped
    # for runaway containment), and a client MAY request its own main max_tokens —
    # so headroom there would fight the ceiling rather than help.
    deepseek_thinking_headroom_tokens: int = Field(
        default=4000, ge=0, alias="DEEPSEEK_THINKING_HEADROOM_TOKENS"
    )

    # ---- Main-model output shape ---------------------------------------
    # The narrow Daintree terminal is ~56 columns wide; the response contract (the
    # final system message) and the output linter both reference this width.
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

    # ---- Authentication -------------------------------------------------
    daintree_api_key: SecretStr = Field(default=SecretStr(""), alias="DAINTREE_API_KEY")

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

    # ---- Task result_schema bounds (terminal_extract_json.v1 only) ------
    max_result_schema_bytes: int = Field(default=32 * 1024, alias="MAX_RESULT_SCHEMA_BYTES")
    max_result_schema_depth: int = Field(default=12, alias="MAX_RESULT_SCHEMA_DEPTH")
    max_result_schema_properties: int = Field(default=128, alias="MAX_RESULT_SCHEMA_PROPERTIES")
    max_result_schema_enum: int = Field(default=128, alias="MAX_RESULT_SCHEMA_ENUM")
    max_result_schema_string: int = Field(default=4096, alias="MAX_RESULT_SCHEMA_STRING")
    max_active_skills: int = Field(default=3, alias="MAX_ACTIVE_SKILLS")
    max_meta_header_bytes: int = Field(default=6 * 1024, alias="MAX_META_HEADER_BYTES")

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
    utility_timeout_seconds: float = Field(default=60.0, alias="UTILITY_TIMEOUT_SECONDS")
    upstream_connect_timeout_seconds: float = Field(
        default=10.0, alias="UPSTREAM_CONNECT_TIMEOUT_SECONDS"
    )

    # ---- Selector -------------------------------------------------------
    # The selector is HARDCODED non-thinking (see DEEPSEEK_REASONING_EFFORT at the
    # top of this module for the rationale); its prompt's reason-first JSON key
    # order is the inline reasoning step.
    selector_context_tokens: int = Field(default=64000, alias="SELECTOR_CONTEXT_TOKENS")
    # Sized with wide margin over the observed 120-190-token completions: the
    # reason-first key order puts skill_ids LAST in the JSON, so a length
    # truncation would now lose the critical key (parse fail -> repair -> possibly
    # degraded). The cap is runaway containment, not a spend target.
    selector_max_output_tokens: int = Field(default=800, alias="SELECTOR_MAX_OUTPUT_TOKENS")

    # ---- Utility tasks --------------------------------------------------
    # Named small-model tasks (checkpoint, summarize, classify, workflow_plan, …) run
    # thinking-high — hardcoded, like all thinking posture. Each TaskProfile's
    # max_output_tokens sizes its VISIBLE output; thinking headroom is added on top.
    # json tasks use response_format json_object (compatible with thinking); no
    # utility task uses a forced tool call.

    # ---- Retries --------------------------------------------------------
    # Applies to every upstream call (main completion, stream open,
    # selector, utility tasks): retries transient failures only, and only before
    # any public bytes are emitted. 3 retries = up to 4 attempts.
    max_upstream_retries: int = Field(default=3, ge=0, alias="MAX_UPSTREAM_RETRIES")
    retry_backoff_base_seconds: float = Field(
        default=0.25, ge=0.0, alias="RETRY_BACKOFF_BASE_SECONDS"
    )

    # ---- Policy ---------------------------------------------------------
    allow_remote_image_urls: bool = Field(default=False, alias="ALLOW_REMOTE_IMAGE_URLS")
    expose_debug_selector_reason: bool = Field(default=False, alias="EXPOSE_DEBUG_SELECTOR_REASON")

    # ---- Context field caps (bytes, unless noted) -----------------------
    cap_project_instructions: int = Field(default=32 * 1024, alias="CAP_PROJECT_INSTRUCTIONS")
    cap_goal: int = Field(default=8 * 1024, alias="CAP_GOAL")
    cap_workflow_runs: int = Field(default=24 * 1024, alias="CAP_WORKFLOW_RUNS")
    cap_async_operations: int = Field(default=8 * 1024, alias="CAP_ASYNC_OPERATIONS")
    cap_pinned_memories: int = Field(default=16 * 1024, alias="CAP_PINNED_MEMORIES")
    cap_relevant_memories: int = Field(default=16 * 1024, alias="CAP_RELEVANT_MEMORIES")
    cap_active_worktree: int = Field(default=512, alias="CAP_ACTIVE_WORKTREE")
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
    cap_client_metadata: int = Field(default=2 * 1024, alias="CAP_CLIENT_METADATA")
    cap_message_text_part: int = Field(default=1024 * 1024, alias="CAP_MESSAGE_TEXT_PART")
    cap_message_text: int = Field(default=1024 * 1024, alias="CAP_MESSAGE_TEXT")

    # ---- CLI-provided integrations (MCP servers + tool index) -----------
    cap_mcp_servers: int = Field(default=32, alias="CAP_MCP_SERVERS")
    cap_mcp_block_bytes: int = Field(default=64 * 1024, alias="CAP_MCP_BLOCK_BYTES")
    cap_tool_index_desc: int = Field(default=256, alias="CAP_TOOL_INDEX_DESC")

    # ---- Skill learning (LOCAL DEVELOPMENT ONLY) ------------------------
    # A backend-internal harness that watches completed local workflows and, when
    # warranted, writes or improves skill files so the catalog "learns". It is
    # entirely invisible to the CLI: no new wire fields, no feedback tool, and the
    # main assistant model is never told it exists — a created skill simply appears
    # as a normal dynamic skill load. It MUST be off in production: an enabled mode
    # fails readiness there (see local_only_problems). Modes:
    #   off     — disabled (default)
    #   propose — run the gate (DeepSeek) + OpenAI GPT-5.5 builder/improver and write the
    #             proposed skill markdown + an audit JSON under the audit dir's proposals/,
    #             but never touch the real skill files or the catalog
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
    # The builder/improver reasoning tier (GPT-5.5) and the gate's thinking-high
    # posture (DeepSeek, json_object verdict) are hardcoded — see
    # SKILL_LEARNING_REASONING_EFFORT / DEEPSEEK_REASONING_EFFORT at the top of
    # this module.
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
    # the diagnosis fields plus the builder's reasoning tokens. On GPT-5.5 this maps
    # to `max_completion_tokens`, which bounds reasoning AND visible output together,
    # so high-effort reasoning eats into this budget. Too small and the JSON
    # truncates: the parse fails, the repair retry truncates identically, and the
    # improve path can never land.
    skill_build_max_output_tokens: int = Field(
        default=24000, alias="SKILL_BUILD_MAX_OUTPUT_TOKENS"
    )

    # ---- Tracing --------------------------------------------------------
    otel_exporter_otlp_endpoint: str = Field(default="", alias="OTEL_EXPORTER_OTLP_ENDPOINT")

    # ---- Derived helpers ------------------------------------------------

    @property
    def skill_learning_enabled(self) -> bool:
        """True when skill learning runs at all (``propose`` or ``apply``)."""
        return self.skill_learning_mode != "off"

    def missing_secrets(self) -> list[str]:
        """Return env var names of required secrets that are empty."""
        missing: list[str] = []
        if not self.deepseek_api_key.get_secret_value():
            missing.append("DEEPSEEK_API_KEY")
        if not self.daintree_api_key.get_secret_value():
            missing.append("DAINTREE_API_KEY")
        if not self.state_signing_key_current.get_secret_value():
            missing.append("STATE_SIGNING_KEY_CURRENT")
        return missing

    def local_only_problems(self) -> list[str]:
        """Local-development-only features that must never run in production, plus
        their required config.

        These join ``missing_secrets()`` / ``weak_secrets()`` in readiness gating,
        so a production (or Cloud Run) deploy that leaves skill learning enabled
        fails ``/readyz`` rather than mutating the catalog from model output at
        runtime. Empty in development *unless* skill learning is enabled without the
        OpenAI key its builder/improver need — that surfaces here rather than as a
        silent fail-open the first time learning tries to run.
        """
        problems: list[str] = []
        if self.environment == "production" and self.skill_learning_enabled:
            problems.append("SKILL_LEARNING_MODE must be 'off' in production")
        if self.skill_learning_enabled and not self.openai_api_key.get_secret_value():
            problems.append("OPENAI_API_KEY is required when skill learning is enabled")
        return problems

    def weak_secrets(self) -> list[str]:
        """Production-only: secrets that are present but too weak/default.

        These join ``missing_secrets()`` to gate readiness, so a deploy with a
        default salt or a short signing key fails ``/readyz`` rather than running
        with forgeable state tokens. In development they are allowed (tests use
        short keys).
        """
        if self.environment != "production":
            return []
        problems: list[str] = []
        if len(self.daintree_api_key.get_secret_value()) < 16:
            problems.append("DAINTREE_API_KEY is too short (<16 chars)")
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
