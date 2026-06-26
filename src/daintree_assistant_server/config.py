"""Service configuration.

Settings are parsed once at startup into an immutable object. Secret values
use :class:`~pydantic.SecretStr` so they never appear in logs, tracebacks, or
``repr`` output. Required secrets default to empty strings here and are
validated by the readiness path (:meth:`Settings.missing_secrets`) so the
process fails *readiness* — not import — when a secret is absent.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Hardcoded models — a product decision, not deployment config. Every DeepSeek
# query (main completion, skill selector, and utility tasks) uses DEEPSEEK_MODEL;
# PUBLIC_MODEL_ID is the alias returned to clients. Only the DeepSeek API key
# (and base URL) come from the environment.
DEEPSEEK_MODEL = "deepseek-v4-flash"
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

    # ---- Upstream provider ---------------------------------------------
    # The model is hardcoded (see DEEPSEEK_MODEL above); only the key and base
    # URL come from the environment.
    deepseek_base_url: str = Field(default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")
    deepseek_api_key: SecretStr = Field(default=SecretStr(""), alias="DEEPSEEK_API_KEY")

    # DeepSeek V4 Flash specifics, confirmed against the live endpoint:
    #  - thinking must be DISABLED for the selector's forced tool_choice to work
    #    ("Thinking mode does not support this tool_choice"); the field is accepted.
    #  - prompt_cache_key is accepted and improves prefix-cache routing.
    # Both default ON; flip off only for a provider/model that rejects them.
    deepseek_send_prompt_cache_key: bool = Field(
        default=True, alias="DEEPSEEK_SEND_PROMPT_CACHE_KEY"
    )
    deepseek_send_thinking: bool = Field(default=True, alias="DEEPSEEK_SEND_THINKING")

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
    max_upstream_retries: int = Field(default=2, alias="MAX_UPSTREAM_RETRIES")
    retry_backoff_base_seconds: float = Field(default=0.25, alias="RETRY_BACKOFF_BASE_SECONDS")

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
    cap_watchers_count: int = Field(default=20, alias="CAP_WATCHERS_COUNT")
    cap_watchers_bytes: int = Field(default=4 * 1024, alias="CAP_WATCHERS_BYTES")
    cap_client_metadata: int = Field(default=2 * 1024, alias="CAP_CLIENT_METADATA")
    cap_message_text_part: int = Field(default=1024 * 1024, alias="CAP_MESSAGE_TEXT_PART")
    cap_message_text: int = Field(default=1024 * 1024, alias="CAP_MESSAGE_TEXT")

    # ---- CLI-provided integrations (MCP servers + tool index) -----------
    cap_mcp_servers: int = Field(default=32, alias="CAP_MCP_SERVERS")
    cap_mcp_block_bytes: int = Field(default=64 * 1024, alias="CAP_MCP_BLOCK_BYTES")
    cap_tool_index_desc: int = Field(default=256, alias="CAP_TOOL_INDEX_DESC")

    # ---- Tracing --------------------------------------------------------
    otel_exporter_otlp_endpoint: str = Field(default="", alias="OTEL_EXPORTER_OTLP_ENDPOINT")

    # ---- Derived helpers ------------------------------------------------

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
