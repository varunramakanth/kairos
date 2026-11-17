"""API settings: the worker's settings plus what only the HTTP service needs."""

from pydantic import Field

from kairos_worker.settings import Settings


class ApiSettings(Settings):
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    """Browser origins allowed to call the API (the Next.js dev server by default)."""

    embedded_worker: bool = False
    """Run a worker inside the API process. Convenient locally (one command); in
    production the worker runs as its own service."""
