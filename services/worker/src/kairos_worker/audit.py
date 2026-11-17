"""Audit hooks: every executed tool call with a side effect goes to the audit log."""

from collections.abc import Sequence

from kairos_db.governance import AuditStore
from kairos_domain.run import RunState, ToolCallRecord
from kairos_domain.tools import ToolOutput, ToolRisk, ToolSpec
from kairos_harness import BaseHooks


class AuditHooks(BaseHooks):
    def __init__(self, store: AuditStore, specs: Sequence[ToolSpec]) -> None:
        self._store = store
        self._risk = {spec.name: spec.risk for spec in specs}

    async def after_tool(
        self, state: RunState, call: ToolCallRecord, output: ToolOutput, duration_ms: int
    ) -> None:
        risk = self._risk.get(call.name, ToolRisk.READ)
        if risk is ToolRisk.READ:
            return
        await self._store.record(
            actor="agent",
            action="tool.failed" if output.is_error else "tool.executed",
            subject=call.name,
            run_id=state.run_id,
            detail={
                "tool_call_id": call.id,
                "risk": risk.value,
                "input": call.input,
                "output": output.content[:2000],
            },
        )
