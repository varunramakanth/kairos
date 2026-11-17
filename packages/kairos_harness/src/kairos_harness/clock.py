"""Clocks and ID generators: real ones for production, deterministic ones for tests."""

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class FakeClock:
    """A clock that only moves when told to. ``sleep`` advances it and returns at once."""

    def __init__(self, start: datetime | None = None) -> None:
        self._start = start or datetime(2026, 1, 1, tzinfo=UTC)
        self._elapsed = 0.0

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._elapsed)

    def monotonic(self) -> float:
        return self._elapsed

    def advance(self, seconds: float) -> None:
        self._elapsed += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)


class UuidIdGenerator:
    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"


class SeededIdGenerator:
    """Deterministic IDs: ``{prefix}_{seed}_{counter}``."""

    def __init__(self, seed: str = "test") -> None:
        self._seed = seed
        self._counter = 0

    def new_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{self._seed}_{self._counter:04d}"
