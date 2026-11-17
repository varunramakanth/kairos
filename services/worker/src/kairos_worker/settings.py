"""Runtime configuration, read from the environment (see ``.env.example``)."""

from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KAIROS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://kairos:kairos@localhost:5432/kairos"

    provider: Literal["anthropic", "bedrock"] = "anthropic"
    model: str = "claude-opus-5-5"
    bedrock_region: str | None = None
    bedrock_model_prefix: str = "anthropic."

    lease_ttl_seconds: float = 60.0
    poll_seconds: float = 1.0
    max_attempts: int = 5

    otlp_endpoint: str | None = None
    """OTLP/HTTP base endpoint for traces, e.g. Langfuse's ``/api/public/otel``."""
    otlp_headers: str = ""
    """Comma-separated ``key=value`` pairs sent with every export."""

    compact_at_prompt_tokens: int | None = 150_000
    """Compact a run's context when its prompt would exceed this many tokens."""

    tavily_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("TAVILY_API_KEY", "KAIROS_TAVILY_API_KEY")
    )
    """Enables ``web_search``. Without it the tool is not offered to the model."""
