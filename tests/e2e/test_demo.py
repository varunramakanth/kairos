"""The committed demo cassette matches what the app would ask today.

If a prompt, tool schema or the system prompt changes, the demo would fail at the
first message. This test re-records the script and compares fingerprints, so the
break shows up here instead. Fix: ``uv run python -m tests.e2e.demo``.
"""

from pathlib import Path

from kairos_harness.cassette import load_cassette
from tests.e2e.demo import CASSETTE, PROMPTS, record


async def test_the_demo_cassette_is_up_to_date(tmp_path: Path) -> None:
    fresh = tmp_path / "demo.jsonl"
    await record(fresh)
    committed = [entry.fingerprint for entry in load_cassette(CASSETTE)]
    assert [entry.fingerprint for entry in load_cassette(fresh)] == committed, (
        "the demo cassette is stale: run `uv run python -m tests.e2e.demo`"
    )


def test_every_scene_has_a_tool_call_and_an_answer() -> None:
    entries = load_cassette(CASSETTE)
    assert len(entries) == 2 * len(PROMPTS)
    assert all(entry.ignores_tool_output for entry in entries)


def test_the_web_examples_are_the_demo_prompts() -> None:
    examples = Path(__file__).parents[2] / "apps" / "web" / "src" / "lib" / "examples.ts"
    source = " ".join(examples.read_text().split())  # Biome may wrap long strings
    for prompt in PROMPTS:
        assert f'"{prompt}"' in source, f"apps/web examples differ from the demo: {prompt!r}"
