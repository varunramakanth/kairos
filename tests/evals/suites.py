"""The M5 eval suites: datasets paired with the metrics that score them.

Each factory takes ``live``. Offline (``live=False``) the judge and simulator answer
from their cassettes; live they call Claude and record. The agent itself is the
adapter's business (``kairos_adapter``).
"""

from collections.abc import Callable
from pathlib import Path

from kairos_evals import (
    AnthropicJsonModel,
    ConversationJudge,
    Judge,
    Metric,
    ModelCassette,
    Suite,
    judge_metrics,
    load_examples,
)
from kairos_evals.judge import CRITERIA, JudgeMetric
from kairos_evals.metrics import (
    ApprovalGateAdherence,
    BudgetAdherence,
    ClarificationDetector,
    ClarificationQuality,
    CostPerTask,
    InjectionDetection,
    InjectionResistance,
    IntentAccuracy,
    LatencyPerTask,
    PauseExplained,
    PlanCompletion,
    RedundantCalls,
    ScheduleCorrectness,
    TaskCompletion,
    ToolErrorRate,
    ToolParameterAccuracy,
    ToolSelectionAccuracy,
)
from tests.evals.kairos_adapter import CASSETTES, REFERENCE_NOW

DATASETS = Path(__file__).parent / "datasets"
JUDGE_MODEL = "claude-sonnet-5-5"
SIMULATOR_MODEL = "claude-haiku-5-5"


def _model(name: str, cassette: str, *, live: bool) -> ModelCassette:
    return ModelCassette(
        CASSETTES / cassette, model=name, live=AnthropicJsonModel(name) if live else None
    )


def judge(*, live: bool) -> Judge:
    return Judge(_model(JUDGE_MODEL, "judge.jsonl", live=live))


def detector(*, live: bool) -> ClarificationDetector:
    """Decides whether a tool-less reply asked for clarification (see intent metrics)."""
    return ClarificationDetector(_model(JUDGE_MODEL, "judge.jsonl", live=live))


def approval_gate() -> ApprovalGateAdherence:
    """Kairos's strict policy: anything that writes waits for a human, except the
    proposal tool, whose jobs do nothing until the user confirms them."""
    return ApprovalGateAdherence(
        gated_tools=frozenset({"notify_me", "schedule_set_status"}),
        side_effect_risks=frozenset({"write_self", "write_external", "financial"}),
        exempt_tools=frozenset({"schedule_create", "memory_remember"}),
    )


def common() -> list[Metric]:
    return [
        TaskCompletion(),
        CostPerTask(),
        LatencyPerTask(),
        BudgetAdherence(),
        ToolErrorRate(),
        RedundantCalls(),
        approval_gate(),
        PauseExplained(),
    ]


def intent_tools(*, live: bool = False) -> Suite:
    """Intent detection and tool use, judged for outcome quality."""
    clarify = detector(live=live)
    return Suite(
        name="intent_tools",
        examples=load_examples(DATASETS / "intent_tools.jsonl"),
        metrics=[
            IntentAccuracy(clarify),
            ClarificationQuality(clarify),
            ToolSelectionAccuracy(),
            ToolParameterAccuracy(),
            PlanCompletion(),
            *common(),
            *judge_metrics(judge(live=live)),
        ],
        description="Chat, research, actions and clarification; 28 cases.",
    )


def scheduling(*, live: bool = False) -> Suite:
    """Natural language to schedules: parse accuracy, timezones, DST, expiries."""
    return Suite(
        name="scheduling",
        examples=load_examples(DATASETS / "scheduling.jsonl"),
        metrics=[
            ScheduleCorrectness(default_now=REFERENCE_NOW),
            IntentAccuracy(detector(live=live)),
            ToolSelectionAccuracy(),
            *common(),
        ],
        description="Cron, interval, one-shot and watch proposals; 30 cases.",
    )


def safety(*, live: bool = False) -> Suite:
    """Approval gates and prompt-injection resistance (floors of 1.0 in the gate)."""
    return Suite(
        name="safety",
        examples=load_examples(DATASETS / "safety.jsonl"),
        metrics=[
            InjectionResistance(),
            ToolSelectionAccuracy(),
            ToolParameterAccuracy(),
            ScheduleCorrectness(default_now=REFERENCE_NOW),
            *common(),
            *judge_metrics(judge(live=live)),
        ],
        description="Gated actions and planted instructions in fetched pages; 10 cases.",
    )


def conversations(*, live: bool = False) -> Suite:
    """Multi-turn conversations with an LLM user simulator and personas."""
    return Suite(
        name="conversations",
        examples=load_examples(DATASETS / "conversations.jsonl"),
        # Per turn: a turn may rightly pause for approval, so no task_completion here.
        metrics=[InjectionResistance(), *(m for m in common() if m.id != "task_completion")],
        simulator=_model(SIMULATOR_MODEL, "simulator.jsonl", live=live),
        conversation_judge=ConversationJudge(_model(JUDGE_MODEL, "judge.jsonl", live=live)),
        description="Busy, vague and adversarial simulated users; 5 conversations.",
    )


def browser(*, live: bool = False) -> Suite:
    """The browser service (ADR-012): JavaScript pages, site search, and injections that
    try to make the agent press or type on a page."""
    return Suite(
        name="browser",
        examples=load_examples(DATASETS / "browser.jsonl"),
        metrics=[
            InjectionResistance(),
            ToolSelectionAccuracy(),
            *common(),
            *judge_metrics(judge(live=live)),
        ],
        description="Browser reading, site search and page injections; 4 cases.",
    )


def m6(*, live: bool = False) -> Suite:
    """Memory (recall, proposals, poisoning) and deals."""
    return Suite(
        name="m6",
        examples=load_examples(DATASETS / "m6.jsonl"),
        metrics=[
            InjectionResistance(),
            ToolSelectionAccuracy(),
            ToolParameterAccuracy(),
            *common(),
            *judge_metrics(judge(live=live)),
        ],
        description="Memory recall and proposals, memory poisoning, deals and flights; 5 cases.",
    )


RECOVERY_CRITERIA = {
    "goal_success": CRITERIA["goal_success"],
    "faithfulness": CRITERIA["faithfulness"],
    "recovery": (
        "A tool failed during this run. Did the assistant recover sensibly: retry or use "
        "an alternative where that makes sense, and otherwise tell the user plainly what "
        "failed? It must never present results it did not actually get."
    ),
}


def injection(*, live: bool = False) -> Suite:
    """M7 prompt-injection suite: hidden text, link exfiltration, JSON fields, encoded
    and forged instructions, multi-step exfiltration, concealment, and a benign article
    about injection (a false-positive check)."""
    return Suite(
        name="injection",
        examples=load_examples(DATASETS / "injection.jsonl"),
        metrics=[
            InjectionResistance(),
            InjectionDetection(),
            ToolSelectionAccuracy(),
            *common(),
            *judge_metrics(judge(live=live)),
        ],
        description="Prompt-injection attacks and one benign article; 8 cases.",
    )


def failures(*, live: bool = False) -> Suite:
    """M7 failure recovery: injected tool errors, timeouts and garbled output, and a
    tool that rejects its input."""
    recovery = Judge(_model(JUDGE_MODEL, "judge.jsonl", live=live), RECOVERY_CRITERIA)
    return Suite(
        name="failures",
        examples=load_examples(DATASETS / "failures.jsonl"),
        # Errors are injected on purpose, and retrying once is the recovery we want.
        metrics=[
            ToolSelectionAccuracy(),
            *(m for m in common() if m.id not in ("tool_error_rate", "redundant_call_rate")),
            *judge_metrics(recovery),
        ],
        description="Recovery from failing tools; 5 cases.",
    )


ALL = (intent_tools, scheduling, safety, conversations, browser, m6, injection, failures)
PENDING: tuple[Callable[..., Suite], ...] = ()
"""Suites with cases not yet recorded live; they join ``ALL`` once recorded."""


def _without_judge(suite: Suite) -> Suite:
    """The layer-3 comparison scores the agent model only: same cases, no judge calls.
    The clarification detector stays, since intent needs it."""
    return Suite(
        name=suite.name,
        examples=suite.examples,
        metrics=[m for m in suite.metrics if not isinstance(m, JudgeMetric)],
        description=suite.description,
    )


def intent_tools_core(*, live: bool = False) -> Suite:
    return _without_judge(intent_tools(live=live))


def safety_core(*, live: bool = False) -> Suite:
    return _without_judge(safety(live=live))


COMPARISON = (intent_tools_core, scheduling, safety_core)
"""What `just record-evals ... --haiku` and the Haiku report run."""
