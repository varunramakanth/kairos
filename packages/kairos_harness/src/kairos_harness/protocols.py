"""The seams of the harness (ADR-001).

The loop depends only on these Protocols. Each has an in-memory implementation for
tests and a real one elsewhere (``kairos_providers``, ``kairos_tools``,
``kairos_db``). Swapping implementations is how cassettes, the fake clock, seeded
IDs and the SDK comparison adapters work.
"""

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from kairos_domain.events import DurableEvent, RunEvent
from kairos_domain.model import ModelCompleted, ModelEvent, ModelRequest
from kairos_domain.run import RunRequest, RunState, ToolCallRecord
from kairos_domain.tools import ToolContext, ToolDecision, ToolOutput, ToolSpec


class Provider(Protocol):
    """A model backend. One call in, a stream of normalized events out."""

    @property
    def name(self) -> str:
        """OpenTelemetry ``gen_ai.provider.name`` value, e.g. ``anthropic``."""
        ...

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Stream one model call.

        The last event is always ``ModelCompleted``. Failures raise
        ``kairos_domain.errors.ProviderError``; nothing is yielded after an error.
        """
        ...


class ToolExecutor(Protocol):
    """Knows the available tools and how to run one."""

    def specs(self) -> Sequence[ToolSpec]:
        """Tool definitions in a stable order (order affects prompt caching)."""
        ...

    async def execute(self, call: ToolCallRecord, context: ToolContext) -> ToolOutput:
        """Run one call. May raise; the loop turns exceptions into error results."""
        ...


class Checkpointer(Protocol):
    """Durable storage for run state, events and oversized tool output."""

    async def load(self, run_id: str) -> RunState | None: ...

    async def save(self, state: RunState, events: Sequence[DurableEvent]) -> None:
        """Atomically store ``state`` and append ``events``.

        Raises ``LostLeaseError`` if this process no longer owns the run. The loop
        calls this before every batch of side effects, so it doubles as the lease
        guard.
        """
        ...

    async def put_blob(self, run_id: str, content: str) -> str:
        """Store full tool output; returns a handle for the "read more" path."""
        ...

    async def get_blob(self, blob_id: str) -> str | None: ...


class Clock(Protocol):
    """Injectable time, so tests and evals never wait on the real clock."""

    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin; only differences are meaningful."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class IdGenerator(Protocol):
    def new_id(self, prefix: str) -> str: ...


class Hooks(Protocol):
    """Observation and control points in the loop.

    Used for policy, telemetry, memory writes and guardrails. Hooks must not mutate
    ``state``; the two that influence the loop do so through their return value.
    """

    async def on_run_start(self, state: RunState) -> None:
        """A process started or resumed this run."""
        ...

    async def before_model(self, state: RunState, request: ModelRequest) -> ModelRequest:
        """May return a modified request (e.g. a different model)."""
        ...

    async def after_model(
        self, state: RunState, request: ModelRequest, completed: ModelCompleted
    ) -> None: ...

    async def before_tool(
        self, state: RunState, call: ToolCallRecord, spec: ToolSpec
    ) -> ToolDecision: ...

    async def after_tool(
        self, state: RunState, call: ToolCallRecord, output: ToolOutput, duration_ms: int
    ) -> None: ...

    async def on_error(self, state: RunState, error: BaseException) -> None: ...

    async def on_complete(self, state: RunState) -> None:
        """The loop stopped (finished or paused). ``state.stop_reason`` says why."""
        ...


@dataclass
class RunContext:
    """Per-invocation controls that are not part of the durable request."""

    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    """Set to stop the run at the next safe point with ``StopReason.CANCELLED``."""
    resolutions: Mapping[str, ToolOutput] = field(default_factory=dict[str, ToolOutput])
    """Human-supplied outcomes for ``uncertain`` tool calls, keyed by tool call id."""


class Harness(Protocol):
    """An agent runtime. ``AgentLoop`` is ours; SDK adapters implement the same shape."""

    def run(
        self, request: RunRequest, context: RunContext | None = None
    ) -> AsyncIterator[RunEvent]:
        """Start the run, or resume it if a checkpoint for ``request.run_id`` exists.

        Yields events as they happen. The final durable event is ``RunFinished``.
        """
        ...
