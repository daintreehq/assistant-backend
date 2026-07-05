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
# main completion, the skill selector, and the utility tasks all run on the small
# model. The skill-learning gate also uses the small model; its builder/improver
# use the stronger GLM model (a harder generation task). PUBLIC_MODEL_ID is the
# alias returned to clients. Only the Fireworks API key (and base URL) come from
# the environment. Fireworks speaks the OpenAI wire shape.
FIREWORKS_SMALL_MODEL = "accounts/fireworks/models/deepseek-v4-flash"
SKILL_BUILDER_MODEL = "accounts/fireworks/models/glm-5p2"
SKILL_IMPROVER_MODEL = "accounts/fireworks/models/glm-5p2"
PUBLIC_MODEL_ID = "daintree-assistant"

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

    # ---- Upstream provider (Fireworks, OpenAI-compatible) --------------
    # Models are hardcoded (see the constants above); only the key and base URL
    # come from the environment.
    fireworks_base_url: str = Field(
        default="https://api.fireworks.ai/inference/v1", alias="FIREWORKS_BASE_URL"
    )
    fireworks_api_key: SecretStr = Field(default=SecretStr(""), alias="FIREWORKS_API_KEY")

    # Fireworks controls reasoning through `reasoning_effort` (always sent), not a
    # `thinking` object: "none" disables thinking (DeepSeek V4 defaults reasoning ON
    # when it is absent, and the selector's forced tool_choice requires non-thinking,
    # so "none" must be sent explicitly); "high"/"max" are the GLM thinking tiers used
    # by the skill builder/improver. prompt_cache_key improves prefix-cache routing and
    # defaults ON; flip off only for a provider/model that rejects it.
    fireworks_send_prompt_cache_key: bool = Field(
        default=True, alias="FIREWORKS_SEND_PROMPT_CACHE_KEY"
    )

    # ---- Main-model thinking -------------------------------------------
    # The selector and utility tasks always run NON-thinking — the selector's
    # forced tool_choice is incompatible with thinking mode. The main /respond
    # call is the only one that can use it. Default OFF (reverted 2026-07-05): thinking
    # ON was observed to DESTABILIZE multi-agent orchestration — the model reasons out
    # the whole multi-round plan, then dumps it as one giant speculative tool batch
    # (60+ calls in round 0: spawn + read + await + CLOSE all at once, against terminals
    # and artifacts that don't exist yet, so most fail). Thinking OFF the model steps
    # measuredly (check → spawn → wait → read → judge), which is the validated behavior.
    # The tradeoff of OFF: with reasoning_effort "none" deepseek-v4-flash can leak
    # chain-of-thought into visible content on complex tool turns (fabricated context
    # recaps, stray </think>); when CoT routes to reasoning_content (ON) it round-trips
    # through the wire contract (a tool-call turn missing it 400s on DeepSeek). That
    # leakage is a cosmetic issue to solve separately (content sanitizing / prompt) —
    # far less costly than the broken orchestration ON produced. Set
    # MAIN_THINKING_ENABLED=true to re-enable. Thinking mode ignores temperature/top_p.
    main_thinking_enabled: bool = Field(default=False, alias="MAIN_THINKING_ENABLED")
    main_reasoning_effort: Literal["high", "max"] = Field(
        default="high", alias="MAIN_REASONING_EFFORT"
    )

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
    selector_timeout_seconds: float = Field(default=20.0, alias="SELECTOR_TIMEOUT_SECONDS")
    main_timeout_seconds: float = Field(default=840.0, alias="MAIN_TIMEOUT_SECONDS")
    utility_timeout_seconds: float = Field(default=60.0, alias="UTILITY_TIMEOUT_SECONDS")
    upstream_connect_timeout_seconds: float = Field(
        default=10.0, alias="UPSTREAM_CONNECT_TIMEOUT_SECONDS"
    )

    # ---- Selector -------------------------------------------------------
    selector_context_tokens: int = Field(default=64000, alias="SELECTOR_CONTEXT_TOKENS")
    selector_max_output_tokens: int = Field(default=500, alias="SELECTOR_MAX_OUTPUT_TOKENS")

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
    cap_pinned_memories: int = Field(default=16 * 1024, alias="CAP_PINNED_MEMORIES")
    cap_relevant_memories: int = Field(default=16 * 1024, alias="CAP_RELEVANT_MEMORIES")
    cap_active_worktree: int = Field(default=512, alias="CAP_ACTIVE_WORKTREE")
    # Max open-terminal inventory rows rendered into the runtime context (issue #286).
    cap_open_terminals: int = Field(default=32, alias="CAP_OPEN_TERMINALS")
    cap_watchers_count: int = Field(default=20, alias="CAP_WATCHERS_COUNT")
    cap_watchers_bytes: int = Field(default=4 * 1024, alias="CAP_WATCHERS_BYTES")
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
    #   propose — run the gate + GLM builder/improver and write the proposed skill
    #             markdown + an audit JSON under the audit dir's proposals/, but never
    #             touch the real skill files or the catalog
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
    # GLM builder/improver thinking tier ("high" or "max"). The gate stays non-thinking.
    skill_learning_reasoning_effort: Literal["high", "max"] = Field(
        default="high", alias="SKILL_LEARNING_REASONING_EFFORT"
    )
    # Gate verdicts below this confidence never write.
    skill_learning_min_confidence: float = Field(
        default=0.75, ge=0.0, le=1.0, alias="SKILL_LEARNING_MIN_CONFIDENCE"
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
    # the diagnosis fields plus GLM's reasoning tokens, which count against
    # max_tokens. Too small and the JSON truncates: the parse fails, the repair
    # retry truncates identically, and the improve path can never land.
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
        if not self.fireworks_api_key.get_secret_value():
            missing.append("FIREWORKS_API_KEY")
        if not self.daintree_api_key.get_secret_value():
            missing.append("DAINTREE_API_KEY")
        if not self.state_signing_key_current.get_secret_value():
            missing.append("STATE_SIGNING_KEY_CURRENT")
        return missing

    def local_only_problems(self) -> list[str]:
        """Local-development-only features that must never run in production.

        These join ``missing_secrets()`` / ``weak_secrets()`` in readiness gating,
        so a production (or Cloud Run) deploy that leaves skill learning enabled
        fails ``/readyz`` rather than mutating the catalog from model output at
        runtime. Empty in development.
        """
        problems: list[str] = []
        if self.environment == "production" and self.skill_learning_enabled:
            problems.append("SKILL_LEARNING_MODE must be 'off' in production")
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
