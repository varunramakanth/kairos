"""The storage seam for memory. ``kairos_db.MemoryStore`` implements it on Postgres;
``kairos_memory.testing.MemoryMemories`` in a list, for tests.

Every method acts for one ``owner`` (ADR-018): another user's memories are invisible,
and changing one by id behaves as if it did not exist.
"""

from collections.abc import Sequence
from typing import Protocol

from kairos_domain.memory import Memory, MemoryKind, MemorySource, MemoryStatus, MemoryUpdate


class MemoryRepository(Protocol):
    async def propose(
        self,
        content: str,
        kind: MemoryKind,
        *,
        owner: str,
        source: MemorySource,
        run_id: str | None,
    ) -> Memory | None:
        """Store a proposal the user must confirm. ``None`` when an equal memory (same
        text, ignoring case and spacing) of the same owner already exists, active or
        proposed."""
        ...

    async def create(
        self,
        content: str,
        kind: MemoryKind,
        *,
        owner: str,
        source: MemorySource,
        status: MemoryStatus,
    ) -> Memory: ...

    async def search(self, query: str, *, owner: str, limit: int = 5) -> Sequence[Memory]:
        """Active memories matching ``query``, best first: keyword relevance, weighted
        towards recently updated memories."""
        ...

    async def recent(
        self, *, owner: str, status: MemoryStatus | None = None, limit: int = 200
    ) -> Sequence[Memory]: ...

    async def get(self, memory_id: str, *, owner: str) -> Memory | None: ...

    async def update(
        self, memory_id: str, change: MemoryUpdate, *, owner: str
    ) -> Memory | None: ...

    async def delete(self, memory_id: str, *, owner: str) -> bool: ...


def normalize(content: str) -> str:
    """The form two memories are compared in for duplicates."""
    return " ".join(content.lower().split())
