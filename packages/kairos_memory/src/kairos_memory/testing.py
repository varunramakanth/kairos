"""An in-memory ``MemoryRepository`` for tests and offline evals."""

from collections.abc import Sequence
from datetime import UTC, datetime

from kairos_domain.memory import Memory, MemoryKind, MemorySource, MemoryStatus, MemoryUpdate
from kairos_domain.users import LOCAL_USER
from kairos_memory.protocols import normalize


class MemoryMemories:
    """Search is a plain word-overlap score, enough to test the tools.

    ``items`` given up front belong to ``owner``."""

    def __init__(self, items: Sequence[Memory] = (), *, owner: str = LOCAL_USER) -> None:
        self.items: dict[str, Memory] = {m.id: m for m in items}
        self.owners: dict[str, str] = {m.id: owner for m in items}
        self._next = len(self.items)

    def _new(
        self,
        content: str,
        kind: MemoryKind,
        source: MemorySource,
        status: MemoryStatus,
        run_id: str | None,
        owner: str,
    ) -> Memory:
        self._next += 1
        now = datetime.now(UTC)
        memory = Memory(
            id=f"mem_{self._next:04d}",
            kind=kind,
            content=content.strip(),
            status=status,
            source=source,
            run_id=run_id,
            created_at=now,
            updated_at=now,
        )
        self.items[memory.id] = memory
        self.owners[memory.id] = owner
        return memory

    def _mine(self, owner: str) -> list[Memory]:
        return [m for m in self.items.values() if self.owners.get(m.id) == owner]

    async def propose(
        self,
        content: str,
        kind: MemoryKind,
        *,
        owner: str,
        source: MemorySource,
        run_id: str | None,
    ) -> Memory | None:
        if any(normalize(m.content) == normalize(content) for m in self._mine(owner)):
            return None
        return self._new(content, kind, source, MemoryStatus.PROPOSED, run_id, owner)

    async def create(
        self,
        content: str,
        kind: MemoryKind,
        *,
        owner: str,
        source: MemorySource,
        status: MemoryStatus,
    ) -> Memory:
        return self._new(content, kind, source, status, None, owner)

    async def search(self, query: str, *, owner: str, limit: int = 5) -> Sequence[Memory]:
        words = set(normalize(query).split())
        scored = [
            (len(words & set(normalize(m.content).split())), m)
            for m in self._mine(owner)
            if m.status is MemoryStatus.ACTIVE
        ]
        return [m for score, m in sorted(scored, key=lambda s: -s[0]) if score > 0][:limit]

    async def recent(
        self, *, owner: str, status: MemoryStatus | None = None, limit: int = 200
    ) -> Sequence[Memory]:
        return [m for m in self._mine(owner) if status is None or m.status is status][:limit]

    async def get(self, memory_id: str, *, owner: str) -> Memory | None:
        return self.items.get(memory_id) if self.owners.get(memory_id) == owner else None

    async def update(self, memory_id: str, change: MemoryUpdate, *, owner: str) -> Memory | None:
        current = await self.get(memory_id, owner=owner)
        if current is None:
            return None
        updated = current.model_copy(update=change.model_dump(exclude_none=True))
        self.items[memory_id] = updated
        return updated

    async def delete(self, memory_id: str, *, owner: str) -> bool:
        if await self.get(memory_id, owner=owner) is None:
            return False
        del self.items[memory_id]
        del self.owners[memory_id]
        return True
