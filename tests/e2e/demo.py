"""A scripted demo for running the UI with no API key and no cost (`just api-demo`).

Four scenes, each answering one of the example prompts on the Welcome page:

1. a plan with a live checklist;
2. a fare watch proposed for the user to confirm;
3. a memory proposed for the user to confirm;
4. an email that waits for the user's approval, then is recorded.

The model turns are hand-written, not real model output; the tools are real. Because
tools return fresh ids and times, the cassette is recorded with fingerprints that
ignore tool-result text (``ignore_tool_output``); everything else must still match.

``python -m tests.e2e.demo`` re-records ``tests/e2e/cassettes/demo.jsonl``;
``python -m tests.e2e.demo serve`` runs the API on it, in its own ``kairos_demo``
database (created and migrated on start) so scripted threads never mix with real
ones. Both build their settings with ``demo_settings``, which ignores ``.env`` and
the environment, so the tool set, and with it every fingerprint, is the same.
"""

import asyncio
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from pydantic import JsonValue
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from kairos_api.settings import ApiSettings
from kairos_domain.governance import Notification
from kairos_domain.run import RunRequest
from kairos_harness import AgentLoop, InMemoryCheckpointer
from kairos_harness.cassette import RecordingProvider
from kairos_harness.testing import ScriptedProvider, Turn, text_turn, tool_turn
from kairos_memory.testing import MemoryMemories
from kairos_scheduler.testing import MemoryJobs
from kairos_worker.wiring import add_delegate, build_tools, http_client

CASSETTE = Path(__file__).parent / "cassettes" / "demo.jsonl"
MODEL = "claude-opus-5-5"
LOCAL_DATABASE = "postgresql+asyncpg://kairos:kairos@localhost:5432/kairos"
DEMO_DATABASE = "kairos_demo"

PLAN_PROMPT = "Plan a three-step comparison of Postgres queue libraries."
WATCH_PROMPT = (
    "Every weekday at 8am, check flights from Toronto to Lisbon in March and tell me "
    "if a round trip drops under $700."
)
MEMORY_PROMPT = "Remember that I prefer aisle seats and usually fly out of Toronto Pearson."
EMAIL_PROMPT = "Email me a short packing list for a week in Lisbon in March."
PROMPTS = (PLAN_PROMPT, WATCH_PROMPT, MEMORY_PROMPT, EMAIL_PROMPT)
"""In scene order. The web app's Welcome page offers the same prompts."""

DEMO_NOTE = "\n\n*(Scripted demo answer.)*"

PLAN: list[JsonValue] = [
    {"step": "List the candidate libraries", "status": "completed"},
    {"step": "Compare durability and locking", "status": "in_progress"},
    {"step": "Recommend one", "status": "pending"},
]
PLAN_ANSWER = (
    """Here is the comparison plan, already started:

1. **Candidates**: [procrastinate](https://procrastinate.readthedocs.io/), \
[pgqueuer](https://github.com/janbjorge/pgqueuer), and a hand-written \
`FOR UPDATE SKIP LOCKED` queue like the one in Kairos.
2. **Durability and locking**: all three rely on row locks with `SKIP LOCKED`; \
the differences are retries, scheduling and how a lost worker is detected.
3. **Recommendation**: pick after step 2, weighing features against code you own."""
    + DEMO_NOTE
)

WATCH: dict[str, JsonValue] = {
    "name": "Toronto to Lisbon fare in March",
    "kind": "watch",
    "schedule": {"kind": "cron", "expression": "0 8 * * mon-fri", "timezone": "America/Toronto"},
    "watch": {
        "target": "round-trip flights Toronto (YYZ) to Lisbon (LIS), departing in March",
        "extract": "lowest round-trip economy fare in CAD",
        "condition": {"kind": "below", "threshold": 700},
    },
}
WATCH_ANSWER = (
    "I've proposed a watch: every weekday at 8:00 (Toronto time) I'll check round-trip "
    "fares from Toronto to Lisbon for March and alert you when one drops under $700. "
    "It stays off until you confirm it, on the card above or under Tasks & watches." + DEMO_NOTE
)

MEMORY: dict[str, JsonValue] = {
    "content": "Prefers aisle seats and usually flies out of Toronto Pearson (YYZ).",
    "kind": "preference",
}
MEMORY_ANSWER = (
    "Noted. I've proposed remembering that you prefer aisle seats and fly out of "
    "Toronto Pearson. Confirm it on the Memory page and I'll use it from then on." + DEMO_NOTE
)

PACKING = (
    "Lisbon in March: 12-18 C, some rain.\n"
    "- Light rain jacket and a compact umbrella\n"
    "- Two light sweaters for the evenings\n"
    "- Comfortable shoes with grip: the hills and cobbles are steep\n"
    "- Adapter: type F (European two-pin)\n"
    "- A tote for the market and a reusable water bottle"
)
EMAIL: dict[str, JsonValue] = {"title": "Packing list: a week in Lisbon in March", "body": PACKING}
EMAIL_ANSWER = (
    "Done. In this demo, email and push are switched off, so the list was not "
    "delivered anywhere: you'll find it under Settings, in Recent notifications. "
    "With email set up, it would be in your inbox." + DEMO_NOTE
)

SCRIPT: list[Turn] = [
    tool_turn(
        ("toolu_demo_plan", "update_plan", {"items": PLAN}), text="Let me plan this.", model=MODEL
    ),
    text_turn(PLAN_ANSWER, model=MODEL),
    tool_turn(
        ("toolu_demo_watch", "schedule_create", WATCH),
        text="I'll set that up as a watch.",
        model=MODEL,
    ),
    text_turn(WATCH_ANSWER, model=MODEL),
    tool_turn(("toolu_demo_memory", "memory_remember", MEMORY), model=MODEL),
    text_turn(MEMORY_ANSWER, model=MODEL),
    tool_turn(
        ("toolu_demo_email", "notify_me", EMAIL),
        text="Here's a short list. I'll email it to you once you approve.",
        model=MODEL,
    ),
    text_turn(EMAIL_ANSWER, model=MODEL),
]

_ISOLATE_PREFIXES = ("KAIROS_", "TAVILY_", "SERPAPI_")


@contextmanager
def _isolated_environment() -> Generator[None]:
    """Hide KAIROS_* and tool keys from pydantic-settings, then restore them."""
    saved = {k: v for k, v in os.environ.items() if k.startswith(_ISOLATE_PREFIXES)}
    for key in saved:
        del os.environ[key]
    try:
        yield
    finally:
        os.environ.update(saved)


def demo_database_url() -> str:
    """The local database URL with the database name swapped for ``kairos_demo``."""
    base = make_url(os.environ.get("KAIROS_DATABASE_URL") or LOCAL_DATABASE)
    return base.set(database=DEMO_DATABASE).render_as_string(hide_password=False)


async def _create_demo_database() -> None:
    """Create ``kairos_demo`` next to the main database if it does not exist yet."""
    main = make_url(os.environ.get("KAIROS_DATABASE_URL") or LOCAL_DATABASE)
    engine = create_async_engine(main, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            exists = await connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": DEMO_DATABASE}
            )
            if not exists:
                await connection.execute(text(f'CREATE DATABASE "{DEMO_DATABASE}"'))
    finally:
        await engine.dispose()


def demo_settings(database_url: str | None = None) -> ApiSettings:
    """The settings both the recording and the demo server use."""
    url = database_url or demo_database_url()
    with _isolated_environment():
        return ApiSettings(
            _env_file=None,  # type: ignore[call-arg]  # pydantic-settings init option
            database_url=url,
            provider="replay",
            replay_cassette=CASSETTE,
            model=MODEL,
            embedded_worker=True,
            timezone="America/Toronto",
            injection_screening="patterns",
            memory_reflection=False,
            browser=False,
        )


async def _discard(notification: Notification, key: str) -> None:
    return None


async def record(path: Path = CASSETTE) -> None:
    """Run every scene through the real tool set and record the model calls."""
    settings = demo_settings()
    async with http_client() as http:
        provider = ScriptedProvider(SCRIPT, name="anthropic")
        tools = build_tools(
            settings, http, jobs=MemoryJobs(), notify=_discard, memory=MemoryMemories()
        )
        add_delegate(tools, settings, provider)  # the API registers it last, too
        recorder = RecordingProvider(provider, path, ignore_tool_output=True)
        for index, prompt in enumerate(PROMPTS):
            loop = AgentLoop(provider=recorder, tools=tools, checkpointer=InMemoryCheckpointer())
            request = RunRequest(run_id=f"demo_{index}", input=prompt, model=MODEL)
            _ = [event async for event in loop.run(request)]


def serve(port: int = 8000) -> None:
    import uvicorn

    from kairos_api.app import create_app
    from kairos_db.migrate import upgrade

    asyncio.run(_create_demo_database())
    settings = demo_settings()
    upgrade(settings.database_url)
    print(f"demo database: {DEMO_DATABASE} (delete it to start the demo afresh)")
    uvicorn.run(create_app(settings), port=port)


if __name__ == "__main__":
    if sys.argv[1:] == ["serve"]:
        serve()
    else:
        asyncio.run(record())
        print(f"recorded {CASSETTE}")
