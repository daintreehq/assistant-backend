"""Settings, parsed once from the environment (and ``.env`` locally).

Deliberately small. The server holds no credential and calls no upstream, so
there is nothing secret to configure: what remains is where to bind, how to log,
which build this is, and one request-size ceiling.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        case_sensitive=False,
    )

    # A closed set, so a typo ("prod") fails to parse instead of silently
    # skipping the hardened behaviour below.
    environment: Literal["development", "test", "staging", "production"] = Field(
        default="development", alias="ENVIRONMENT"
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    build_sha: str = Field(default="", alias="BUILD_SHA")

    # Cloud Run injects PORT=8080 and the image sets HOST=0.0.0.0.
    host: str = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=8080, alias="PORT")

    # A search request is a query of at most 8,192 characters in a JSON-RPC
    # envelope. 64 KiB is generous for that and small enough that nobody can
    # make the JSON parser do real work for free.
    max_request_bytes: int = Field(default=64 * 1024, ge=1024, alias="MAX_REQUEST_BYTES")

    @property
    def is_hardened(self) -> bool:
        """True where internal detail (startup errors) is withheld from responses."""
        return self.environment in ("staging", "production")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
