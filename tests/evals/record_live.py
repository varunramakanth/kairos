"""Record the live golden cases: real model calls and real tool calls.

Costs money and reaches the network. Run it with ``just record-live``, which loads
the API keys from ``.env`` and sets ``LIVE=1``.
"""

import asyncio
import os
import sys

from kairos_domain.events import RunFinished
from kairos_domain.run import RunRequest
from kairos_evals import load_examples
from kairos_harness import AgentLoop, InMemoryCheckpointer
from kairos_harness.cassette import RecordingProvider, RecordingToolExecutor
from kairos_providers import anthropic_provider
from kairos_worker.settings import Settings
from kairos_worker.wiring import build_tools, http_client
from tests.evals.live_cases import DATASET, MODEL, model_cassette, tool_cassette


async def record(only: set[str]) -> None:
    settings = Settings()
    total = 0.0
    async with http_client() as http:
        for example in load_examples(DATASET):
            if only and example.id not in only:
                continue
            loop = AgentLoop(
                provider=RecordingProvider(anthropic_provider(), model_cassette(example.id)),
                tools=RecordingToolExecutor(build_tools(settings, http), tool_cassette(example.id)),
                checkpointer=InMemoryCheckpointer(),
            )
            request = RunRequest(run_id=example.id, input=example.input, model=MODEL)
            events = [event async for event in loop.run(request)]
            (finished,) = (e for e in events if isinstance(e, RunFinished))
            total += finished.cost_usd
            print(f"{example.id}: {finished.stop_reason.value}, ${finished.cost_usd:.4f}")
    print(f"total ${total:.4f}")


if __name__ == "__main__":
    if os.environ.get("LIVE") != "1":
        sys.exit("refusing to spend money without LIVE=1 (use `just record-live`)")
    asyncio.run(record(set(sys.argv[1:])))
