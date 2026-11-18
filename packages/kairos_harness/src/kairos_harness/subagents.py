"""Subagents: a parent delegates a focused task to a child run.

The child is an ordinary ``AgentLoop`` run with:

- **isolated context:** it sees only the task text, never the parent's history, so a
  long research detour does not fill the parent's context window;
- **a restricted, read-only toolset:** a profile lists the tools it may use, and only
  tools with ``read`` risk are ever offered. A child therefore never needs an
  approval, cannot act on the user's behalf, and cannot delegate further;
- **its own budget:** steps, tool calls and dollars are capped per profile;
- **a compact result:** the parent gets the child's final answer (truncated), not
  its transcript.

The child's result is built from tool output that may include web content, so the
tool that exposes it marks it untrusted.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from kairos_domain.events import RunFinished, ToolCallResult
from kairos_domain.run import RunRequest, StopReason, ToolCallRecord
from kairos_domain.tools import ToolContext, ToolOutput, ToolRisk, ToolSpec
from kairos_domain.usage import Budget
from kairos_harness.checkpoint import InMemoryCheckpointer
from kairos_harness.loop import AgentLoop
from kairos_harness.protocols import Clock, ContentScreen, Hooks, Provider, ToolExecutor

RESULT_CHARS = 6_000

SUBAGENT_IDENTITY = (
    "You are a focused subagent working for Kairos, a personal agent. You have one task "
    "and a small set of read-only tools. Work efficiently, then reply with a compact, "
    "self-contained result the parent can use directly: the answer first, then key "
    "details, with a Markdown link for every web source you used. Do not ask "
    "questions; if something is missing, say what you assumed."
)


class SubagentProfile(BaseModel):
    """A kind of child the parent can start: what it is for and what it may use."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    """Shown to the parent model so it can choose a profile."""
    tools: frozenset[str]
    """Allowed tool names. Tools that are not ``read`` risk are dropped regardless."""
    instructions: str = ""
    budget: Budget = Budget(max_steps=12, max_tool_calls=16, max_cost_usd=0.30)
    model: str | None = None
    """Defaults to the ``Subagents`` model."""


class ReadOnlyTools:
    """A view of a tool executor that offers only the allowed, read-only tools."""

    def __init__(self, inner: ToolExecutor, allowed: frozenset[str]) -> None:
        self._inner = inner
        self._specs = [s for s in inner.specs() if s.name in allowed and s.risk is ToolRisk.READ]
        self._names = {s.name for s in self._specs}

    def specs(self) -> Sequence[ToolSpec]:
        return self._specs

    async def execute(self, call: ToolCallRecord, context: ToolContext) -> ToolOutput:
        if call.name not in self._names:
            return ToolOutput(content=f"tool {call.name} is not available here", is_error=True)
        return await self._inner.execute(call, context)


@dataclass(frozen=True)
class SubagentResult:
    run_id: str
    text: str
    stop_reason: StopReason
    cost_usd: float
    tool_calls: int

    def for_parent(self) -> str:
        """What the parent model reads."""
        body = self.text.strip() or "(the subagent returned no text)"
        if len(body) > RESULT_CHARS:
            body = body[:RESULT_CHARS] + "\n… (truncated)"
        note = ""
        if self.stop_reason is not StopReason.COMPLETED:
            note = f"\n\n(The subagent stopped early: {self.stop_reason.value}.)"
        usage = f"{self.tool_calls} tool calls, ${self.cost_usd:.4f}"
        return f"{body}{note}\n\n[subagent {self.run_id}: {usage}]"


class Subagents:
    """Starts child runs for the parent. One instance per process, shared by runs."""

    def __init__(
        self,
        *,
        provider: Provider,
        tools: ToolExecutor,
        profiles: Sequence[SubagentProfile],
        model: str,
        hooks: Hooks | None = None,
        clock: Clock | None = None,
        screen: ContentScreen | None = None,
    ) -> None:
        self._screen = screen
        self._provider = provider
        self._tools = tools
        self.profiles: Mapping[str, SubagentProfile] = {p.name: p for p in profiles}
        self._model = model
        self._hooks = hooks
        self._clock = clock

    async def run(self, profile: str, task: str, *, parent: ToolContext) -> SubagentResult:
        chosen = self.profiles.get(profile)
        if chosen is None:
            raise KeyError(f"unknown subagent profile {profile!r}")
        run_id = f"{parent.run_id}.sub.{parent.tool_call_id}"
        loop = AgentLoop(
            provider=self._provider,
            tools=ReadOnlyTools(self._tools, chosen.tools),
            checkpointer=InMemoryCheckpointer(),
            clock=self._clock,
            hooks=self._hooks,
            screen=self._screen,
        )
        system = SUBAGENT_IDENTITY + (f"\n\n{chosen.instructions}" if chosen.instructions else "")
        request = RunRequest(
            run_id=run_id,
            input=task,
            model=chosen.model or self._model,
            system_prompt=system,
            budget=chosen.budget,
            metadata={"parent_run_id": parent.run_id, "subagent": chosen.name},
        )
        finished: RunFinished | None = None
        tool_calls = 0
        async for event in loop.run(request):
            if isinstance(event, ToolCallResult):
                tool_calls += 1
            elif isinstance(event, RunFinished):
                finished = event
        if finished is None:  # pragma: no cover  # the loop always finishes a run
            raise RuntimeError(f"subagent {run_id} ended without finishing")
        return SubagentResult(
            run_id=run_id,
            text=finished.output_text or "",
            stop_reason=finished.stop_reason,
            cost_usd=finished.cost_usd,
            tool_calls=tool_calls,
        )
