"""Read models for conversations: threads and the runs inside them.

These are what the API returns and what the generated TypeScript client types.
"""

from datetime import datetime

from kairos_domain.base import DomainModel
from kairos_domain.events import RunEvent
from kairos_domain.run import RunStatus, StopReason


class Thread(DomainModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class RunSummary(DomainModel):
    """One turn: the user's input and how the run that answered it ended."""

    id: str
    thread_id: str | None
    input: str
    model: str
    status: RunStatus
    stop_reason: StopReason | None
    output_text: str | None
    error: str | None
    cost_usd: float
    created_at: datetime


class RunDetail(DomainModel):
    """A run with its durable event log: enough to render the turn or its trace."""

    run: RunSummary
    events: list[RunEvent]


class ThreadDetail(DomainModel):
    thread: Thread
    runs: list[RunDetail]
