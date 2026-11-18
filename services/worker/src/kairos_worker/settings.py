"""Runtime configuration, read from the environment (see ``.env.example``)."""

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from kairos_domain.governance import ChannelName
from kairos_tools.mcp import McpServerConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KAIROS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://kairos:kairos@localhost:5432/kairos"

    provider: Literal["anthropic", "bedrock", "replay"] = "anthropic"
    """``replay`` serves model calls from ``replay_cassette``: no network, no cost."""
    replay_cassette: Path | None = None
    model: str = "claude-opus-5-5"
    job_model: str = "claude-haiku-5-5"
    """Model for scheduled tasks and watches unless a job names its own."""
    timezone: str = "America/Toronto"
    """The user's timezone: the default for schedules and how times are shown."""
    scheduler_poll_seconds: float = 5.0
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
    serpapi_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("SERPAPI_API_KEY", "KAIROS_SERPAPI_API_KEY")
    )
    """Enables ``flights_search`` (Google Flights via SerpApi). Free plan: ~250 searches
    a month; one watch checking every 6 hours uses ~120."""

    # --- hardening (M7) -------------------------------------------------------------
    injection_screening: Literal["model", "patterns", "off"] = "model"
    """Screen untrusted tool output: patterns always, and with ``model`` the job model
    decides pattern matches (a fraction of a cent each). ``patterns`` flags every match."""

    # --- subagents and memory (M6) ----------------------------------------------------
    subagent_model: str = "claude-sonnet-5-5"
    """Model for ``delegate`` children (focused, read-only research)."""
    memory_embeddings: Literal["local", "none"] = "local"
    """``local``: semantic recall with a small model on this machine (downloaded once,
    about 67 MB, into ~/.cache/kairos/models). ``none``: keyword recall only."""
    memory_reflection: bool = False
    """After each completed chat turn, ask ``job_model`` to propose memories from it
    (one small model call per turn). Proposals wait for the user's confirmation."""

    # --- browser and MCP (M6) -------------------------------------------------------
    browser: bool = False
    """Offer the isolated browser (``browser_*`` tools) through its MCP server. Needs
    Chromium: ``uv run playwright install chromium``."""
    mcp_servers: list[McpServerConfig] = []
    """Extra MCP servers, as JSON. Their tools need approval and their output is treated
    as untrusted unless a server's ``tools`` entry says otherwise."""

    # --- notifications (M4) ---------------------------------------------------------
    email_provider: Literal["ses", "smtp"] | None = None
    """How email is sent. ``ses`` (recommended, send-only credentials) or ``smtp``.
    Unset means no email."""
    email_to: str | None = None
    """The owner's address. Email can only ever go here."""
    email_from: str | None = None
    """SES: the verified sender identity. Defaults to ``email_to``."""
    ses_region: str = "ca-central-1"
    ses_profile: str | None = None
    """AWS profile holding the send-only SES credentials (kept apart from other AWS use)."""
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    """For Gmail: an app password (Google Account > Security > App passwords)."""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    vapid_public_key: str | None = None
    vapid_private_key: SecretStr | None = None
    """Web Push keys; generate them with ``kairos vapid-keys``."""
    web_base_url: str = "http://localhost:3000"
    """Where links in notifications point."""

    @field_validator(
        "tavily_api_key",
        "serpapi_api_key",
        "email_provider",
        "email_to",
        "email_from",
        "ses_profile",
        "smtp_username",
        "smtp_password",
        "vapid_public_key",
        "vapid_private_key",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # `KEY=` in a .env file means "not configured", not an empty value.
        return None if value == "" else value

    @property
    def email_configured(self) -> bool:
        if self.email_provider == "ses":
            return bool(self.email_to)
        if self.email_provider == "smtp":
            return bool(self.email_to and self.smtp_username and self.smtp_password)
        return False

    @property
    def push_configured(self) -> bool:
        return bool(self.vapid_public_key and self.vapid_private_key)

    @property
    def channels(self) -> list[ChannelName]:
        """Configured delivery channels; the console log is always one."""
        names: list[ChannelName] = ["console"]
        if self.email_configured:
            names.append("email")
        if self.push_configured:
            names.append("push")
        return names
