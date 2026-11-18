"""Long-term memory: what Kairos knows about the user across conversations.

The agent can only *propose* a memory. A proposal is stored but never recalled until
the user confirms it, the same way agent-created jobs wait for confirmation. This
keeps a planted instruction on a web page from quietly becoming a "fact" the agent
believes in every future conversation (memory poisoning). Memories the user writes
themselves are active at once.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from kairos_domain.base import DomainModel


class MemoryKind(StrEnum):
    PROFILE = "profile"
    """Facts about the user: home city, family, work."""
    PREFERENCE = "preference"
    """How they like things: aisle seats, metric units, brief answers."""
    EPISODIC = "episodic"
    """Something that happened: "booked the Lisbon trip on 3 March"."""
    TASK = "task"
    """Ongoing goals and their state: "looking for a 27-inch 4K monitor under $400"."""


class MemoryStatus(StrEnum):
    PROPOSED = "proposed"
    """Suggested by the agent; not recalled until the user confirms it."""
    ACTIVE = "active"


class MemorySource(StrEnum):
    USER = "user"
    AGENT = "agent"
    """Proposed with the ``memory_remember`` tool during a run."""
    REFLECTION = "reflection"
    """Proposed by the after-run reflection hook."""


class Memory(DomainModel):
    id: str
    kind: MemoryKind
    content: str
    status: MemoryStatus
    source: MemorySource
    run_id: str | None
    created_at: datetime
    updated_at: datetime


class MemoryCreate(DomainModel):
    """A memory written by the user in the UI: active at once."""

    kind: MemoryKind = MemoryKind.PROFILE
    content: str = Field(min_length=1, max_length=1_000)


class MemoryUpdate(DomainModel):
    """Edit, re-classify or confirm a memory. Unset fields stay as they are."""

    kind: MemoryKind | None = None
    content: str | None = Field(default=None, min_length=1, max_length=1_000)
    status: MemoryStatus | None = None
