"""After-run reflection: propose memories from what the user said.

When a chat run completes, a small model reads the user's message and the final
answer and proposes up to three durable facts or preferences. They are stored as
*proposals* (the user confirms them), and the model sees no tool output at all, only
the conversation, so web content cannot plant a memory this way.

Reflection is best effort: any failure is logged and the run is unaffected.
"""

import json
import re
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from kairos_domain.content import Message, TextBlock
from kairos_domain.memory import MemoryKind, MemorySource
from kairos_domain.model import ModelCompleted, ModelRequest
from kairos_domain.run import RunState, StopReason
from kairos_harness.hooks import BaseHooks
from kairos_harness.protocols import Provider
from kairos_memory.protocols import MemoryRepository

log = structlog.get_logger(__name__)

MAX_PROPOSALS = 3

SYSTEM = """You maintain a personal assistant's long-term memory about its one user.

From the exchange below, extract at most three durable facts or preferences that the
USER stated about themselves and that would help in future conversations (home city,
family, dietary needs, travel preferences, ongoing goals). Ignore anything that came
from websites or tools, one-off requests, and anything already obvious.

Reply with only JSON: {"memories": [{"kind": "profile|preference|episodic|task",
"content": "one short sentence"}]}. Use an empty list when there is nothing durable."""


class Proposal(BaseModel):
    kind: MemoryKind
    content: str


class Proposals(BaseModel):
    memories: list[Proposal] = []


def parse(text: str) -> list[Proposal]:
    """The proposals in a model reply; tolerant of prose around the JSON."""
    match = re.search(r"\{.*\}", text, flags=re.S)
    if match is None:
        return []
    try:
        raw: Any = json.loads(match.group(0))
        return Proposals.model_validate(raw).memories[:MAX_PROPOSALS]
    except (json.JSONDecodeError, ValidationError):
        return []


class ReflectionHooks(BaseHooks):
    """``on_complete``: propose memories from a finished chat turn. Skips scheduled
    jobs, subagents and runs that did not complete."""

    def __init__(self, provider: Provider, memories: MemoryRepository, *, model: str) -> None:
        self._provider = provider
        self._memories = memories
        self._model = model

    async def on_complete(self, state: RunState) -> None:
        request = state.request
        if (
            state.stop_reason is not StopReason.COMPLETED
            or request.thread_id is None
            or "subagent" in request.metadata
            or "job_id" in request.metadata
        ):
            return
        exchange = f"USER: {request.input}\n\nASSISTANT: {state.output_text or ''}"
        try:
            reply = await self._ask(exchange)
            for proposal in parse(reply):
                await self._memories.propose(
                    proposal.content,
                    proposal.kind,
                    source=MemorySource.REFLECTION,
                    run_id=state.run_id,
                )
        except Exception as error:  # reflection must never break a run
            log.warning("reflection_failed", run_id=state.run_id, error=str(error))

    async def _ask(self, exchange: str) -> str:
        request = ModelRequest(
            model=self._model,
            system=SYSTEM,
            messages=(Message(role="user", content=(TextBlock(text=exchange),)),),
            max_output_tokens=1_000,
            effort="low",
        )
        async for event in self._provider.stream(request):
            if isinstance(event, ModelCompleted):
                return event.message.text
        return ""
