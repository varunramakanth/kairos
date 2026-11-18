"""Errors shared across packages."""


class KairosError(Exception):
    """Base class for errors raised by Kairos code."""


class LostLeaseError(KairosError):
    """The worker no longer owns the run (paused, cancelled, or taken over).

    Raised by a lease-guarded write that matched no row. The worker must stop
    immediately and perform no further side effects for this run.
    """


class ProviderError(KairosError):
    """A model provider call failed.

    ``retryable`` tells the harness whether re-issuing the same model call can help
    (rate limits, 5xx, dropped connections) or not (bad request, auth).
    """

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
