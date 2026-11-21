"""API settings: the worker's settings plus what only the HTTP service needs."""

from pydantic import Field

from kairos_worker.settings import Settings


class ApiSettings(Settings):
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    """Browser origins allowed to call the API (the Next.js dev server by default)."""

    embedded_worker: bool = False
    """Run a worker inside the API process. Convenient locally (one command); in
    production the worker runs as its own service."""

    rate_limit_per_minute: int = 20
    """Requests per minute, per client, to endpoints that start work (chat turns, job
    runs, test notifications). Beyond it the API answers 429 with Retry-After."""

    path_prefix: str = ""
    """Serve the API under this prefix (``/api`` behind CloudFront)."""
    auth_issuer: str | None = None
    """Cognito user pool issuer URL. Set (with ``auth_client_id``) to require sign-in."""
    auth_client_id: str | None = None
    auth_domain: str | None = None
    """Cognito Hosted UI base URL, e.g. https://kairos-xyz.auth.ca-central-1.amazoncognito.com."""
    auth_user_pool_id: str | None = None
    """The Cognito user pool, for invites from the Users page (ADR-018)."""
