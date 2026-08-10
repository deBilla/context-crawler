"""Base configuration shared across all services.

Each service extends this with its own specific settings.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import LLMProvider


class BaseServiceConfig(BaseSettings):
    """Settings common to all services."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # PostgreSQL
    database_url: str = "postgresql+asyncpg://crawler:crawlerpass@localhost:5432/crawler"

    # MinIO
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    minio_bucket: str = "pages"

    # Logging
    log_level: str = "INFO"

    # OpenTelemetry
    otel_endpoint: str = "http://otel-collector:4317"
    otel_enabled: bool = True


class LLMConfig(BaseSettings):
    """LLM-specific settings for services that use the LLM."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_provider: LLMProvider = LLMProvider.OLLAMA
    llm_model: str = "llama3.1"
    llm_base_url: str = "http://localhost:11434"
    llm_api_key: str = ""
    llm_max_retries: int = 2
    llm_timeout: float = 60.0
    # claude_code provider only: the CLI to shell out to. Override when it is
    # not on PATH (a Docker service, for instance, has no login of its own).
    claude_bin: str = "claude"
