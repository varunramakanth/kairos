"""The policy engine: which tool calls run, which wait for a human, which never run.

Rules, in order (the first that applies wins):

1. ``FINANCIAL`` risk is always denied. Purchases and money movement are never
   autonomous, approved or not.
2. With the kill switch on, every action with a side effect is denied.
3. ``READ`` runs.
4. Tools whose only effect is a proposal the user confirms elsewhere (for example
   ``schedule_create``, which creates an inactive job) run.
5. Everything else waits for approval. This is the owner's *strict* setting: email
   to yourself and job changes included.

The rules are pure; ``PolicyHooks`` plugs them into the harness as a
``before_tool`` hook and reads the kill switch for each decision.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from kairos_domain.run import RunState, ToolCallRecord
from kairos_domain.tools import Allow, Deny, RequireApproval, ToolDecision, ToolRisk, ToolSpec
from kairos_harness import BaseHooks

PROPOSAL_TOOLS = frozenset({"schedule_create", "memory_remember"})
"""Tools that only propose something the user must confirm before it takes effect."""


@dataclass(frozen=True)
class Policy:
    proposal_tools: frozenset[str] = field(default=PROPOSAL_TOOLS)

    def decide(self, spec: ToolSpec, *, actions_enabled: bool) -> ToolDecision:
        if spec.risk is ToolRisk.FINANCIAL:
            return Deny(reason="actions involving money are never automatic")
        if spec.risk is ToolRisk.READ:
            return Allow()
        if not actions_enabled:
            return Deny(reason="agent actions are switched off (kill switch)")
        if spec.name in self.proposal_tools:
            return Allow()
        return RequireApproval(reason=_reason(spec))


def _reason(spec: ToolSpec) -> str:
    if spec.risk is ToolRisk.WRITE_EXTERNAL:
        return f"{spec.name} acts outside Kairos"
    return f"{spec.name} changes your data"


async def _always_enabled() -> bool:
    return True


class PolicyHooks(BaseHooks):
    """A ``before_tool`` hook (combine with others in a ``HookChain``)."""

    def __init__(
        self,
        policy: Policy | None = None,
        *,
        actions_enabled: Callable[[], Awaitable[bool]] = _always_enabled,
    ) -> None:
        self._policy = policy or Policy()
        self._enabled = actions_enabled

    async def before_tool(
        self, state: RunState, call: ToolCallRecord, spec: ToolSpec
    ) -> ToolDecision:
        enabled = True if spec.risk is ToolRisk.READ else await self._enabled()
        return self._policy.decide(spec, actions_enabled=enabled)
