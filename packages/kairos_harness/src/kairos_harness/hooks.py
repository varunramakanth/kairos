"""Hook helpers: a no-op base to subclass and a chain to combine several."""

from collections.abc import Sequence

from kairos_domain.model import ModelCompleted, ModelRequest
from kairos_domain.run import RunState, ToolCallRecord
from kairos_domain.tools import ToolOutput, ToolSpec
from kairos_harness.protocols import Allow, Hooks, ToolDecision


class BaseHooks:
    """Implements every hook as a no-op. Subclass and override what you need."""

    async def on_run_start(self, state: RunState) -> None:
        return None

    async def before_model(self, state: RunState, request: ModelRequest) -> ModelRequest:
        return request

    async def after_model(
        self, state: RunState, request: ModelRequest, completed: ModelCompleted
    ) -> None:
        return None

    async def before_tool(
        self, state: RunState, call: ToolCallRecord, spec: ToolSpec
    ) -> ToolDecision:
        return Allow()

    async def after_tool(
        self, state: RunState, call: ToolCallRecord, output: ToolOutput, duration_ms: int
    ) -> None:
        return None

    async def on_error(self, state: RunState, error: BaseException) -> None:
        return None

    async def on_complete(self, state: RunState) -> None:
        return None


class HookChain(BaseHooks):
    """Runs several hook sets in order.

    ``before_model`` pipes the request through each. ``before_tool`` returns the
    first decision that is not ``Allow``, so any hook can block a call.
    """

    def __init__(self, hooks: Sequence[Hooks]) -> None:
        self._hooks = tuple(hooks)

    async def on_run_start(self, state: RunState) -> None:
        for hook in self._hooks:
            await hook.on_run_start(state)

    async def before_model(self, state: RunState, request: ModelRequest) -> ModelRequest:
        for hook in self._hooks:
            request = await hook.before_model(state, request)
        return request

    async def after_model(
        self, state: RunState, request: ModelRequest, completed: ModelCompleted
    ) -> None:
        for hook in self._hooks:
            await hook.after_model(state, request, completed)

    async def before_tool(
        self, state: RunState, call: ToolCallRecord, spec: ToolSpec
    ) -> ToolDecision:
        for hook in self._hooks:
            decision = await hook.before_tool(state, call, spec)
            if not isinstance(decision, Allow):
                return decision
        return Allow()

    async def after_tool(
        self, state: RunState, call: ToolCallRecord, output: ToolOutput, duration_ms: int
    ) -> None:
        for hook in self._hooks:
            await hook.after_tool(state, call, output, duration_ms)

    async def on_error(self, state: RunState, error: BaseException) -> None:
        for hook in self._hooks:
            await hook.on_error(state, error)

    async def on_complete(self, state: RunState) -> None:
        for hook in self._hooks:
            await hook.on_complete(state)
