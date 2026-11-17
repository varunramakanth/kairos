"""The agent loop (ADR-001).

A run alternates between two phases until something stops it::

    model phase   call the model, stream its output, record the assistant message
    tool phase    decide, checkpoint, run the requested tools in parallel, record results

A *step* is one model phase plus the tool phase it triggered. State is checkpointed
after the model phase, before the tool batch runs and after it finishes, so a run
can be continued by any process from its last checkpoint.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from kairos_domain.content import Message, Provenance, ToolResultBlock, user_text
from kairos_domain.errors import LostLeaseError, ProviderError
from kairos_domain.events import (
    ApprovalRequested,
    BudgetUpdated,
    CheckpointWritten,
    ContextCompacted,
    DurableEvent,
    ModelCallFinished,
    ModelStreamReset,
    RunEvent,
    RunFinished,
    RunResumed,
    RunStarted,
    StepFinished,
    StepStarted,
    TextMessageDelta,
    TextMessageEnd,
    TextMessageStart,
    ToolCallArgs,
    ToolCallEnd,
    ToolCallResult,
    ToolCallStart,
    ToolCallUncertain,
)
from kairos_domain.model import (
    ModelCompleted,
    ModelRequest,
    ModelStopReason,
    ModelTextDelta,
    ModelToolCallStart,
)
from kairos_domain.run import (
    RunRequest,
    RunState,
    RunStatus,
    StopReason,
    ToolCallRecord,
    ToolCallStatus,
)
from kairos_domain.tools import Deny, RequireApproval, ToolContext, ToolOutput, ToolSpec
from kairos_harness.budget import PriceTable, exceeded
from kairos_harness.clock import SystemClock, UuidIdGenerator
from kairos_harness.compaction import estimate_tokens, new_epoch, summary_request_messages
from kairos_harness.context import build_system_prompt, present_tool_output, truncate
from kairos_harness.hooks import BaseHooks
from kairos_harness.protocols import (
    Checkpointer,
    Clock,
    Hooks,
    IdGenerator,
    Provider,
    RunContext,
    ToolExecutor,
)


@dataclass(frozen=True)
class LoopConfig:
    max_tool_result_chars: int = 20_000
    """Longer tool output is truncated for the model and stored in full as a blob."""
    model_retries: int = 2
    """Extra attempts for a model call that failed with a retryable error."""
    retry_backoff_seconds: float = 1.0
    prices: PriceTable = field(default_factory=PriceTable)
    compact_at_prompt_tokens: int | None = 150_000
    """Compact the context before a model call whose prompt would exceed this many
    tokens. ``None`` disables compaction."""


class _Session:
    """Mutable bookkeeping for one invocation of :meth:`AgentLoop.run`."""

    def __init__(self, state: RunState, context: RunContext, clock: Clock) -> None:
        self.state = state
        self.context = context
        self.clock = clock
        self.buffer: list[DurableEvent] = []
        self._active_base = state.active_seconds
        self._segment_start = clock.monotonic()

    def durable[E: DurableEvent](self, cls: type[E], **fields: Any) -> E:
        """Create a durable event with the next ``seq`` and queue it for the next save."""
        event = cls(
            run_id=self.state.run_id, seq=self.state.next_seq, ts=self.clock.now(), **fields
        )
        self.state.next_seq += 1
        self.buffer.append(event)
        return event

    def touch_clock(self) -> None:
        self.state.active_seconds = self._active_base + (
            self.clock.monotonic() - self._segment_start
        )


class AgentLoop:
    """Our implementation of the ``Harness`` protocol."""

    def __init__(
        self,
        *,
        provider: Provider,
        tools: ToolExecutor,
        checkpointer: Checkpointer,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        hooks: Hooks | None = None,
        config: LoopConfig | None = None,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._checkpointer = checkpointer
        self._clock: Clock = clock or SystemClock()
        self._ids: IdGenerator = ids or UuidIdGenerator()
        self._hooks: Hooks = hooks or BaseHooks()
        self._config = config or LoopConfig()

    async def run(
        self, request: RunRequest, context: RunContext | None = None
    ) -> AsyncIterator[RunEvent]:
        context = context or RunContext()
        state = await self._checkpointer.load(request.run_id)
        if state is not None and state.status is RunStatus.FINISHED:
            return  # Nothing to do: running a finished run again is a no-op.

        if state is None:
            messages = [*request.history, user_text(request.input)]
            state = RunState(request=request, messages=messages, attempt=1)
            session = _Session(state, context, self._clock)
            yield session.durable(RunStarted, model=request.model, input=request.input)
        else:
            state.attempt += 1
            state.status = RunStatus.RUNNING
            state.stop_reason = None
            session = _Session(state, context, self._clock)
            yield session.durable(RunResumed, attempt=state.attempt, from_step=state.step)
            for event in self._recover_interrupted_calls(session):
                yield event

        await self._hooks.on_run_start(state)
        yield await self._checkpoint(session)

        try:
            while state.stop_reason is None:
                session.touch_clock()
                if context.cancel.is_set():
                    async for event in self._finish(session, StopReason.CANCELLED):
                        yield event
                    break
                phase = self._tool_phase if state.pending_tool_calls else self._model_phase
                async for event in phase(session):
                    yield event
        except LostLeaseError as error:
            # Another process owns the run now. Write nothing more.
            await self._hooks.on_error(state, error)
            raise
        except Exception as error:
            await self._hooks.on_error(state, error)
            message = f"{type(error).__name__}: {error}"
            async for event in self._finish(session, StopReason.FAILED, error=message):
                yield event

    # --- phases -------------------------------------------------------------------

    async def _model_phase(self, session: _Session) -> AsyncIterator[RunEvent]:
        state = session.state
        if reason := exceeded(state.request.budget, state):
            async for event in self._finish(session, StopReason.BUDGET_EXCEEDED, error=reason):
                yield event
            return

        if self._needs_compaction(state):
            async for event in self._compact(session):
                yield event

        state.step += 1
        yield session.durable(StepStarted, step=state.step)

        request = ModelRequest(
            model=state.request.model,
            system=build_system_prompt(state.request),
            messages=tuple(state.messages),
            tools=tuple(self._tools.specs()),
            max_output_tokens=state.request.max_output_tokens,
            effort=state.request.effort,
        )
        request = await self._hooks.before_model(state, request)

        completed: ModelCompleted | None = None
        async for item in self._stream_model(session, request):
            if isinstance(item, ModelCompleted):
                completed = item
            else:
                yield item
        if completed is None:
            raise ProviderError("model stream ended without a completion", retryable=False)

        text = completed.message.text
        if text:
            yield session.durable(TextMessageEnd, message_id=self._ids.new_id("msg"), text=text)
        for block in completed.message.tool_uses:
            yield session.durable(
                ToolCallEnd, tool_call_id=block.id, name=block.name, input=block.input
            )

        cost = self._config.prices.cost(completed.model, completed.usage)
        state.messages.append(completed.message)
        state.usage = state.usage + completed.usage
        state.cost_usd += cost
        state.last_input_tokens = completed.usage.prompt_tokens
        session.touch_clock()
        await self._hooks.after_model(state, request, completed)
        yield session.durable(
            ModelCallFinished,
            step=state.step,
            model=completed.model,
            stop_reason=completed.stop_reason,
            usage=completed.usage,
            cost_usd=cost,
        )
        yield session.durable(BudgetUpdated, spent=state.spent)

        tool_uses = completed.message.tool_uses
        if completed.stop_reason is ModelStopReason.REFUSAL:
            detail = completed.refusal_category or "no category given"
            async for event in self._finish(
                session, StopReason.REFUSED, error=f"model declined ({detail})", output=text
            ):
                yield event
        elif completed.stop_reason is ModelStopReason.MAX_TOKENS:
            # A truncated tool input can parse as a valid partial object. Never run it.
            async for event in self._finish(
                session, StopReason.FAILED, error="model output hit max_output_tokens", output=text
            ):
                yield event
        elif tool_uses:
            state.pending_tool_calls = [
                ToolCallRecord(id=b.id, name=b.name, input=b.input) for b in tool_uses
            ]
            yield await self._checkpoint(session)
        else:
            yield session.durable(StepFinished, step=state.step)
            async for event in self._finish(session, StopReason.COMPLETED, output=text):
                yield event

    async def _stream_model(
        self, session: _Session, request: ModelRequest, *, live: bool = True
    ) -> AsyncIterator[RunEvent | ModelCompleted]:
        """Stream one model call, retrying retryable failures.

        Only ephemeral events are produced while streaming, so a failed attempt
        leaves nothing durable behind. The ``ModelCompleted`` is yielded last.
        With ``live=False`` (internal calls such as compaction) no stream events
        are produced at all.
        """
        step = session.state.step
        for attempt in range(self._config.model_retries + 1):
            message_id = self._ids.new_id("msg")
            text_started = False
            now = session.clock.now
            run_id = session.state.run_id
            try:
                async for event in self._provider.stream(request):
                    if isinstance(event, ModelCompleted):
                        yield event
                        return
                    if not live:
                        continue
                    if isinstance(event, ModelTextDelta):
                        if not text_started:
                            text_started = True
                            yield TextMessageStart(run_id=run_id, ts=now(), message_id=message_id)
                        yield TextMessageDelta(
                            run_id=run_id, ts=now(), message_id=message_id, delta=event.text
                        )
                    elif isinstance(event, ModelToolCallStart):
                        yield ToolCallStart(
                            run_id=run_id,
                            ts=now(),
                            tool_call_id=event.tool_call_id,
                            name=event.name,
                        )
                    else:
                        yield ToolCallArgs(
                            run_id=run_id,
                            ts=now(),
                            tool_call_id=event.tool_call_id,
                            delta=event.partial_json,
                        )
                raise ProviderError("model stream ended without a completion", retryable=True)
            except ProviderError as error:
                if not error.retryable or attempt == self._config.model_retries:
                    raise
                yield ModelStreamReset(run_id=run_id, ts=now(), step=step, reason=str(error))
                await session.clock.sleep(self._config.retry_backoff_seconds * 2**attempt)

    async def _tool_phase(self, session: _Session) -> AsyncIterator[RunEvent]:
        state = session.state
        calls = list(state.pending_tool_calls)
        specs = {spec.name: spec for spec in self._tools.specs()}

        # 1. Calls whose outcome is unknown block the batch until a human resolves them.
        unresolved = [
            c
            for c in calls
            if c.status is ToolCallStatus.UNCERTAIN and c.id not in session.context.resolutions
        ]
        if unresolved:
            names = ", ".join(c.name for c in unresolved)
            async for event in self._finish(
                session, StopReason.AWAITING_INPUT, error=f"outcome unknown for: {names}"
            ):
                yield event
            return

        # 2. Decide every call before running any, so a batch is all-or-nothing.
        settled: dict[str, ToolOutput] = {}
        runnable: list[tuple[ToolCallRecord, ToolSpec]] = []
        approvals: list[tuple[ToolCallRecord, str]] = []
        for call in calls:
            if call.id in session.context.resolutions:
                settled[call.id] = session.context.resolutions[call.id]
                continue
            spec = specs.get(call.name)
            if spec is None:
                settled[call.id] = ToolOutput(content=f"unknown tool: {call.name}", is_error=True)
                continue
            decision = await self._hooks.before_tool(state, call, spec)
            if isinstance(decision, Deny):
                settled[call.id] = ToolOutput(
                    content=f"not allowed: {decision.reason}", is_error=True
                )
            elif isinstance(decision, RequireApproval):
                approvals.append((call, decision.reason))
            else:
                runnable.append((call, spec))

        if approvals:
            for call, reason in approvals:
                yield session.durable(
                    ApprovalRequested, tool_call_id=call.id, name=call.name, reason=reason
                )
            async for event in self._finish(session, StopReason.AWAITING_APPROVAL):
                yield event
            return

        if reason := exceeded(state.request.budget, state, next_tool_calls=len(runnable)):
            async for event in self._finish(session, StopReason.BUDGET_EXCEEDED, error=reason):
                yield event
            return

        # 3. Record intent, then checkpoint. This save is also the lease guard: if
        #    another worker owns the run it raises before any side effect happens.
        running_ids = {call.id for call, _ in runnable}
        state.pending_tool_calls = [
            c.model_copy(update={"status": ToolCallStatus.RUNNING}) if c.id in running_ids else c
            for c in calls
        ]
        yield await self._checkpoint(session)

        # 4. Run in parallel. A failing tool becomes an error result; it never
        #    cancels its siblings.
        durations: dict[str, int] = dict.fromkeys(settled, 0)
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(self._execute(session, call)) for call, _ in runnable]
        for (call, _), task in zip(runnable, tasks, strict=True):
            settled[call.id], durations[call.id] = task.result()

        # 5. All results go back in one user message, in the order the model asked.
        blocks: list[ToolResultBlock] = []
        for call in calls:
            output = settled[call.id]
            spec = specs.get(call.name)
            block, event_fields = await self._present(state, call, spec, output)
            blocks.append(block)
            yield session.durable(ToolCallResult, duration_ms=durations[call.id], **event_fields)
            await self._hooks.after_tool(state, call, output, durations[call.id])

        state.messages.append(Message(role="user", content=tuple(blocks)))
        state.pending_tool_calls = []
        state.tool_calls_made += len(runnable)
        session.touch_clock()
        yield session.durable(BudgetUpdated, spent=state.spent)
        yield session.durable(StepFinished, step=state.step)
        yield await self._checkpoint(session)

    # --- compaction -------------------------------------------------------------

    def _needs_compaction(self, state: RunState) -> bool:
        limit = self._config.compact_at_prompt_tokens
        if limit is None or len(state.messages) < 2:
            return False
        return max(state.last_input_tokens, estimate_tokens(state.messages)) > limit

    async def _compact(self, session: _Session) -> AsyncIterator[RunEvent]:
        """Summarize the context and start a new epoch (see ``compaction.py``)."""
        state = session.state
        before = max(state.last_input_tokens, estimate_tokens(state.messages))
        request = ModelRequest(
            model=state.request.model,
            system=build_system_prompt(state.request),
            messages=summary_request_messages(state.messages),
            tools=tuple(self._tools.specs()),
            max_output_tokens=state.request.max_output_tokens,
            effort=state.request.effort,
        )
        completed: ModelCompleted | None = None
        async for item in self._stream_model(session, request, live=False):
            if isinstance(item, ModelCompleted):
                completed = item
        if completed is None:
            raise ProviderError("compaction stream ended without a completion", retryable=False)
        summary = completed.message.text.strip()
        if not summary:
            raise ProviderError("compaction returned no summary", retryable=False)

        archive = json.dumps([m.model_dump(mode="json") for m in state.messages])
        blob_id = await self._checkpointer.put_blob(state.run_id, archive)
        cost = self._config.prices.cost(completed.model, completed.usage)
        summarized = len(state.messages)
        state.epoch += 1
        state.messages = new_epoch(state.messages, summary, epoch=state.epoch)
        state.usage = state.usage + completed.usage
        state.cost_usd += cost
        state.last_input_tokens = 0
        yield session.durable(
            ContextCompacted,
            epoch=state.epoch,
            messages_summarized=summarized,
            input_tokens_before=before,
            summary=summary,
            archive_blob_id=blob_id,
            usage=completed.usage,
            cost_usd=cost,
        )
        yield session.durable(BudgetUpdated, spent=state.spent)
        yield await self._checkpoint(session)

    # --- helpers ------------------------------------------------------------------

    def _recover_interrupted_calls(self, session: _Session) -> Sequence[RunEvent]:
        """Apply the resume rule to calls that were running when the last process died."""
        state = session.state
        specs = {spec.name: spec for spec in self._tools.specs()}
        events: list[RunEvent] = []
        recovered: list[ToolCallRecord] = []
        for call in state.pending_tool_calls:
            if call.status is not ToolCallStatus.RUNNING:
                recovered.append(call)
                continue
            spec = specs.get(call.name)
            if spec is not None and spec.idempotent:
                # Safe to run again with the same idempotency key.
                recovered.append(call.model_copy(update={"status": ToolCallStatus.PENDING}))
            else:
                # No hidden retry of a side effect whose outcome we cannot know.
                recovered.append(call.model_copy(update={"status": ToolCallStatus.UNCERTAIN}))
                events.append(
                    session.durable(ToolCallUncertain, tool_call_id=call.id, name=call.name)
                )
        state.pending_tool_calls = recovered
        return events

    async def _execute(self, session: _Session, call: ToolCallRecord) -> tuple[ToolOutput, int]:
        started = session.clock.monotonic()
        context = ToolContext(
            run_id=session.state.run_id, tool_call_id=call.id, idempotency_key=call.idempotency_key
        )
        try:
            output = await self._tools.execute(call, context)
        except Exception as error:
            output = ToolOutput(content=f"{type(error).__name__}: {error}", is_error=True)
        return output, round((session.clock.monotonic() - started) * 1000)

    async def _present(
        self, state: RunState, call: ToolCallRecord, spec: ToolSpec | None, output: ToolOutput
    ) -> tuple[ToolResultBlock, dict[str, Any]]:
        """Turn raw tool output into the model's block and the UI's event fields."""
        provenance = spec.provenance if spec else Provenance.TRUSTED
        content = output.content
        blob_id: str | None = None
        limit = self._config.max_tool_result_chars
        if len(content) > limit:
            blob_id = await self._checkpointer.put_blob(state.run_id, content)
            content = truncate(content, limit=limit, blob_id=blob_id)
        block = ToolResultBlock(
            tool_use_id=call.id,
            content=present_tool_output(content, provenance=provenance, source=call.name),
            is_error=output.is_error,
            provenance=provenance,
        )
        fields: dict[str, Any] = {
            "tool_call_id": call.id,
            "name": call.name,
            "output": content,
            "is_error": output.is_error,
            "provenance": provenance,
            "truncated": blob_id is not None,
            "blob_id": blob_id,
        }
        return block, fields

    async def _checkpoint(self, session: _Session) -> CheckpointWritten:
        session.touch_clock()
        state = session.state
        await self._checkpointer.save(state, session.buffer)
        session.buffer.clear()
        return CheckpointWritten(
            run_id=state.run_id,
            ts=session.clock.now(),
            step=state.step,
            through_seq=state.next_seq - 1,
        )

    async def _finish(
        self,
        session: _Session,
        reason: StopReason,
        *,
        error: str | None = None,
        output: str | None = None,
    ) -> AsyncIterator[RunEvent]:
        state = session.state
        session.touch_clock()
        state.stop_reason = reason
        state.status = RunStatus.PAUSED if reason.is_pause else RunStatus.FINISHED
        state.error = error
        if output:
            state.output_text = output
        yield session.durable(
            RunFinished,
            stop_reason=reason,
            output_text=state.output_text,
            error=error,
            usage=state.usage,
            cost_usd=state.cost_usd,
        )
        yield await self._checkpoint(session)
        await self._hooks.on_complete(state)
