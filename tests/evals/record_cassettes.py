"""Rebuild the cassettes for the M1 golden scenarios.

Run with ``just record-cassettes``. Today this records the hand-written scripts in
``scenarios.py`` (see the warning there). The same entry point will record from a
live provider once that is approved.
"""

import asyncio

from kairos_domain.run import RunRequest
from kairos_evals import load_examples
from kairos_harness import AgentLoop, InMemoryCheckpointer, SeededIdGenerator
from kairos_harness.cassette import RecordingProvider
from kairos_harness.testing import ScriptedProvider
from tests.evals.scenarios import CASSETTES, DATASET, MODEL, SCRIPTS, build_tools


async def record() -> None:
    for example in load_examples(DATASET):
        provider = RecordingProvider(
            ScriptedProvider(SCRIPTS[example.id], name="anthropic"),
            CASSETTES / f"{example.id}.jsonl",
        )
        loop = AgentLoop(
            provider=provider,
            tools=build_tools(),
            checkpointer=InMemoryCheckpointer(),
            ids=SeededIdGenerator(example.id),
        )
        request = RunRequest(run_id=example.id, input=example.input, model=MODEL)
        events = [event async for event in loop.run(request)]
        print(f"{example.id}: recorded, final event {events[-2].type}")


if __name__ == "__main__":
    asyncio.run(record())
