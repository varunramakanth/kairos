import pytest

from kairos_domain.run import RunRequest, RunState, ToolCallRecord
from kairos_domain.tools import Allow, Deny, RequireApproval, ToolRisk, ToolSpec
from kairos_policy import Policy, PolicyHooks


def spec(name: str, risk: ToolRisk) -> ToolSpec:
    return ToolSpec(name=name, description="d", input_schema={"type": "object"}, risk=risk)


@pytest.mark.parametrize(
    ("tool", "enabled", "expected"),
    [
        (spec("web_search", ToolRisk.READ), True, Allow),
        (spec("web_search", ToolRisk.READ), False, Allow),
        (spec("schedule_create", ToolRisk.WRITE_SELF), True, Allow),
        (spec("schedule_set_status", ToolRisk.WRITE_SELF), True, RequireApproval),
        (spec("notify_me", ToolRisk.WRITE_EXTERNAL), True, RequireApproval),
        (spec("notify_me", ToolRisk.WRITE_EXTERNAL), False, Deny),
        (spec("schedule_create", ToolRisk.WRITE_SELF), False, Deny),
        (spec("buy", ToolRisk.FINANCIAL), True, Deny),
    ],
)
def test_strict_policy(tool: ToolSpec, enabled: bool, expected: type) -> None:
    assert isinstance(Policy().decide(tool, actions_enabled=enabled), expected)


async def test_the_hook_reads_the_kill_switch_only_for_side_effects() -> None:
    reads = 0

    async def switch() -> bool:
        nonlocal reads
        reads += 1
        return False

    hooks = PolicyHooks(actions_enabled=switch)
    state = RunState(request=RunRequest(run_id="r", input="x", model="m"))
    call = ToolCallRecord(id="t", name="x", input={})
    assert isinstance(await hooks.before_tool(state, call, spec("web_fetch", ToolRisk.READ)), Allow)
    assert reads == 0
    decision = await hooks.before_tool(state, call, spec("notify_me", ToolRisk.WRITE_EXTERNAL))
    assert isinstance(decision, Deny)
    assert "kill switch" in decision.reason
    assert reads == 1
