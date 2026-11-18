"""Tracing as a harness hook set.

One trace per invocation of the loop::

    invoke_agent kairos            the run (or one resumed segment of it)
    ├── chat claude-opus-5-5       one model call
    ├── execute_tool web_search    one tool call
    └── ...

Put ``TracingHooks`` last in a ``HookChain``. The chain stops at the first hook that
denies a tool call, so spans are only opened for calls that actually run.
"""

import json
import time
from dataclasses import dataclass, field

from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer, set_span_in_context

from kairos_domain.model import ModelCompleted, ModelRequest
from kairos_domain.run import RunState, ToolCallRecord
from kairos_domain.tools import Allow, ToolDecision, ToolOutput, ToolSpec
from kairos_telemetry import semconv as a

AGENT_NAME = "kairos"


@dataclass
class _RunSpans:
    root: Span
    model: Span | None = None
    tools: dict[str, tuple[Span, int]] = field(default_factory=dict[str, tuple[Span, int]])
    cost_usd: float = 0.0


class TracingHooks:
    """Emits OpenTelemetry spans following the GenAI semantic conventions.

    Args:
        tracer: Where spans go. Tests pass a tracer backed by an in-memory exporter.
        provider_name: ``gen_ai.provider.name`` (``anthropic`` or ``aws.bedrock``).
        harness_version: Recorded on every span so evals can slice by version.
        capture_content: Also record prompts, answers and tool output. Off by
            default: run content can include private email and documents.
    """

    def __init__(
        self,
        tracer: Tracer,
        *,
        provider_name: str,
        harness_version: str,
        capture_content: bool = False,
    ) -> None:
        self._tracer = tracer
        self._provider_name = provider_name
        self._harness_version = harness_version
        self._capture_content = capture_content
        self._runs: dict[str, _RunSpans] = {}

    def _common(self, state: RunState) -> dict[str, str | int]:
        attributes: dict[str, str | int] = {
            a.RUN_ID: state.run_id,
            a.HARNESS_VERSION: self._harness_version,
            a.AGENT_NAME: AGENT_NAME,
        }
        if thread_id := state.request.thread_id:
            attributes[a.THREAD_ID] = thread_id
            attributes[a.CONVERSATION_ID] = thread_id
            attributes[a.LANGFUSE_SESSION_ID] = thread_id
        if parent := state.request.metadata.get("parent_run_id"):
            attributes[a.PARENT_RUN_ID] = parent
        if job_id := state.request.metadata.get("job_id"):
            attributes[a.JOB_ID] = job_id
        return attributes

    async def on_run_start(self, state: RunState) -> None:
        root = self._tracer.start_span(
            f"{a.OP_INVOKE_AGENT} {AGENT_NAME}",
            kind=SpanKind.INTERNAL,
            attributes={
                **self._common(state),
                a.OPERATION_NAME: a.OP_INVOKE_AGENT,
                a.REQUEST_MODEL: state.request.model,
                a.ATTEMPT: state.attempt,
            },
        )
        if self._capture_content:
            root.set_attribute(a.INPUT_VALUE, state.request.input)
        self._runs[state.run_id] = _RunSpans(root=root, cost_usd=state.cost_usd)

    async def before_model(self, state: RunState, request: ModelRequest) -> ModelRequest:
        spans = self._runs.get(state.run_id)
        if spans is None:
            return request
        if spans.model is not None:
            # The previous attempt at this call failed and is being retried.
            _fail(spans.model, "model_call_failed")
        spans.model = self._tracer.start_span(
            f"{a.OP_CHAT} {request.model}",
            context=set_span_in_context(spans.root),
            kind=SpanKind.CLIENT,
            attributes={
                **self._common(state),
                a.OPERATION_NAME: a.OP_CHAT,
                a.PROVIDER_NAME: self._provider_name,
                a.REQUEST_MODEL: request.model,
                a.REQUEST_MAX_TOKENS: request.max_output_tokens,
                a.STEP: state.step,
            },
        )
        return request

    async def after_model(
        self, state: RunState, request: ModelRequest, completed: ModelCompleted
    ) -> None:
        spans = self._runs.get(state.run_id)
        if spans is None or spans.model is None:
            return
        span, usage = spans.model, completed.usage
        span.set_attributes(
            {
                a.RESPONSE_MODEL: completed.model,
                a.RESPONSE_FINISH_REASONS: [completed.stop_reason.value],
                a.USAGE_INPUT_TOKENS: usage.input_tokens
                + usage.cache_read_input_tokens
                + usage.cache_creation_input_tokens,
                a.USAGE_OUTPUT_TOKENS: usage.output_tokens,
                a.USAGE_CACHE_READ: usage.cache_read_input_tokens,
                a.USAGE_CACHE_CREATION: usage.cache_creation_input_tokens,
                a.LANGFUSE_COST: state.cost_usd - spans.cost_usd,
            }
        )
        if self._capture_content:
            if completed.message.text:
                span.set_attribute(a.OUTPUT_VALUE, completed.message.text)
            if uses := completed.message.tool_uses:
                requested = [{"id": u.id, "name": u.name, "arguments": u.input} for u in uses]
                span.set_attribute(a.REQUESTED_TOOL_CALLS, json.dumps(requested))
        spans.cost_usd = state.cost_usd
        span.end()
        spans.model = None

    async def before_tool(
        self, state: RunState, call: ToolCallRecord, spec: ToolSpec
    ) -> ToolDecision:
        spans = self._runs.get(state.run_id)
        if spans is not None:
            started_ns = time.time_ns()
            span = self._tracer.start_span(
                f"{a.OP_EXECUTE_TOOL} {call.name}",
                context=set_span_in_context(spans.root),
                kind=SpanKind.INTERNAL,
                start_time=started_ns,
                attributes={
                    **self._common(state),
                    a.OPERATION_NAME: a.OP_EXECUTE_TOOL,
                    a.TOOL_NAME: call.name,
                    a.TOOL_CALL_ID: call.id,
                    a.TOOL_TYPE: "function",
                    a.TOOL_RISK: spec.risk.value,
                    a.TOOL_PROVENANCE: spec.provenance.value,
                    a.STEP: state.step,
                },
            )
            if self._capture_content:
                span.set_attribute(a.TOOL_CALL_ARGUMENTS, json.dumps(call.input))
            spans.tools[call.id] = (span, started_ns)
        return Allow()

    async def after_tool(
        self, state: RunState, call: ToolCallRecord, output: ToolOutput, duration_ms: int
    ) -> None:
        spans = self._runs.get(state.run_id)
        entry = spans.tools.pop(call.id, None) if spans else None
        if entry is None:
            return  # denied, unknown or human-resolved: nothing was executed
        span, started_ns = entry
        span.set_attribute(a.TOOL_IS_ERROR, output.is_error)
        if output.suspected_injection:
            span.set_attribute(a.TOOL_SUSPECTED_INJECTION, True)
        if output.is_error:
            _fail(span, "tool_error", end=False)
        if self._capture_content:
            span.set_attribute(a.OUTPUT_VALUE, output.content)
        # Tools run in parallel and this hook fires after the whole batch, so the
        # span is closed at its measured duration rather than "now".
        span.end(end_time=started_ns + duration_ms * 1_000_000)

    async def on_error(self, state: RunState, error: BaseException) -> None:
        spans = self._runs.pop(state.run_id, None)
        if spans is None:
            return
        for span in [spans.model, *(s for s, _ in spans.tools.values())]:
            if span is not None:
                _fail(span, type(error).__name__)
        spans.root.record_exception(error)
        _fail(spans.root, type(error).__name__)

    async def on_complete(self, state: RunState) -> None:
        spans = self._runs.pop(state.run_id, None)
        if spans is None:
            return
        for span, _ in spans.tools.values():
            span.end()  # opened for a batch that then paused for approval
        root, usage = spans.root, state.usage
        root.set_attributes(
            {
                a.STOP_REASON: state.stop_reason.value if state.stop_reason else "",
                a.COST_USD: state.cost_usd,
                a.STEPS: state.step,
                a.TOOL_CALLS: state.tool_calls_made,
                a.USAGE_INPUT_TOKENS: usage.input_tokens
                + usage.cache_read_input_tokens
                + usage.cache_creation_input_tokens,
                a.USAGE_OUTPUT_TOKENS: usage.output_tokens,
            }
        )
        if self._capture_content and state.output_text:
            root.set_attribute(a.OUTPUT_VALUE, state.output_text)
        if state.error:
            root.set_status(Status(StatusCode.ERROR, state.error))
        root.end()


def _fail(span: Span, error_type: str, *, end: bool = True) -> None:
    span.set_attribute(a.ERROR_TYPE, error_type)
    span.set_status(Status(StatusCode.ERROR))
    if end:
        span.end()
