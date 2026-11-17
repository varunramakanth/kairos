"""A scripted demo conversation for running the UI with no API key and no cost.

``python -m tests.e2e.demo`` records ``tests/e2e/cassettes/demo.jsonl`` against the
app's real tool set and system prompt. Serve it with ``just api-demo``.

The model turns below are hand-written, not real model output.
"""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from kairos_domain.jobs import Job
from kairos_domain.run import RunRequest
from kairos_harness import AgentLoop, InMemoryCheckpointer
from kairos_harness.cassette import RecordingProvider
from kairos_harness.testing import ScriptedProvider, Turn, text_turn, tool_turn
from kairos_worker.settings import Settings
from kairos_worker.wiring import build_tools, http_client

CASSETTE = Path(__file__).parent / "cassettes" / "demo.jsonl"
MODEL = "claude-opus-5-5"
PROMPT = "Plan a three-step comparison of Postgres queue libraries."

PLAN: list[JsonValue] = [
    {"step": "List the candidate libraries", "status": "completed"},
    {"step": "Compare durability and locking", "status": "in_progress"},
    {"step": "Recommend one", "status": "pending"},
]
ANSWER = """Here is the comparison plan, already started:

1. **Candidates**: [procrastinate](https://procrastinate.readthedocs.io/), \
[pgqueuer](https://github.com/janbjorge/pgqueuer), and a hand-written \
`FOR UPDATE SKIP LOCKED` queue like the one in Kairos.
2. **Durability and locking**: all three rely on row locks with `SKIP LOCKED`; \
the differences are retries, scheduling and how a lost worker is detected.
3. **Recommendation**: pick after step 2, weighing features against code you own.

*(This is a scripted demo answer.)*"""

SCRIPT: list[Turn] = [
    tool_turn(
        ("toolu_demo_plan", "update_plan", {"items": PLAN}), text="Let me plan this.", model=MODEL
    ),
    text_turn(ANSWER, model=MODEL),
]


class _SpecsOnly:
    """Stands in for the job store: only the tool schemas matter for the recording."""

    async def create(self, *args: Any, **kwargs: Any) -> Job:
        raise NotImplementedError

    async def get(self, job_id: str) -> Job | None:
        raise NotImplementedError

    async def list_jobs(self, *, include_deleted: bool = False) -> Sequence[Job]:
        raise NotImplementedError

    async def set_status(self, *args: Any, **kwargs: Any) -> Job | None:
        raise NotImplementedError


async def record() -> None:
    """Record with the same tool set the API offers (schedule tools included)."""
    CASSETTE.unlink(missing_ok=True)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]  # pydantic-settings option
    async with http_client() as http:
        loop = AgentLoop(
            provider=RecordingProvider(ScriptedProvider(SCRIPT, name="anthropic"), CASSETTE),
            tools=build_tools(settings, http, jobs=_SpecsOnly()),
            checkpointer=InMemoryCheckpointer(),
        )
        request = RunRequest(run_id="demo", input=PROMPT, model=MODEL)
        events = [event async for event in loop.run(request)]
    print(f"recorded {CASSETTE} ({events[-2].type})")


if __name__ == "__main__":
    asyncio.run(record())
