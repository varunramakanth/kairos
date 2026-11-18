"""Models the framework itself calls: LLM judges and the user simulator.

Both need a model that answers with one JSON object. ``ModelCassette`` records those
answers keyed by a hash of the request, so a suite that ran once live replays offline
for free and deterministically, and a changed prompt is a visible cache miss rather
than a silent new model call.
"""

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from anthropic import AsyncAnthropic
from anthropic.types import AssistantMessageParam, MessageParam, TextBlock, UserMessageParam
from pydantic import BaseModel, ConfigDict, JsonValue

PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
"""Input and output USD per million tokens, for reporting what judging cost."""


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["user", "assistant"]
    content: str


class JsonModel(Protocol):
    """A model that answers with one JSON object matching ``schema``."""

    @property
    def name(self) -> str: ...

    async def complete(
        self, *, system: str, messages: Sequence[ChatMessage], schema: dict[str, Any]
    ) -> dict[str, JsonValue]: ...


class AnthropicJsonModel:
    """Claude through the Messages API with structured JSON output.

    Effort defaults to ``low``: judging and simulating users are short, well-specified
    tasks, and effort (not a thinking switch) is the cost control on current models.
    """

    def __init__(
        self,
        model: str = "claude-sonnet-5-5",
        *,
        client: AsyncAnthropic | None = None,
        max_tokens: int = 4000,
        effort: str = "low",
    ) -> None:
        self._model = model
        self._client = client or AsyncAnthropic()
        self._max_tokens = max_tokens
        self._effort = effort
        self.cost_usd = 0.0
        self.calls = 0

    @property
    def name(self) -> str:
        return self._model

    async def complete(
        self, *, system: str, messages: Sequence[ChatMessage], schema: dict[str, Any]
    ) -> dict[str, JsonValue]:
        params: list[MessageParam] = [
            UserMessageParam(role="user", content=m.content)
            if m.role == "user"
            else AssistantMessageParam(role="assistant", content=m.content)
            for m in messages
        ]
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=params,
            output_config=cast(
                Any, {"effort": self._effort, "format": {"type": "json_schema", "schema": schema}}
            ),
        )
        self.calls += 1
        price_in, price_out = PRICES_PER_MTOK.get(self._model, (0.0, 0.0))
        usage = response.usage
        cached = (usage.cache_read_input_tokens or 0) + (usage.cache_creation_input_tokens or 0)
        self.cost_usd += (
            (usage.input_tokens + cached) * price_in + usage.output_tokens * price_out
        ) / 1_000_000
        if response.stop_reason == "refusal":
            raise RuntimeError(f"{self._model} refused the request")
        text = next(b.text for b in response.content if isinstance(b, TextBlock))
        return cast(dict[str, JsonValue], json.loads(text))


class CassetteMiss(LookupError):
    """A replayed model call that was never recorded."""


class ModelCassette:
    """Record and replay ``JsonModel`` answers in a JSONL file.

    With ``live`` set, misses are answered by the live model and appended. Without it,
    a miss raises ``CassetteMiss``: the request changed (prompt, schema or model) and
    needs re-recording. The key covers everything that can change the answer.
    """

    def __init__(self, path: Path, *, model: str, live: JsonModel | None = None) -> None:
        if live is not None and live.name != model:
            raise ValueError(f"live model {live.name} does not match cassette model {model}")
        self._path = path
        self._model = model
        self._live = live
        self._answers: dict[str, dict[str, JsonValue]] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    entry = json.loads(line)
                    self._answers[entry["key"]] = entry["answer"]

    @property
    def name(self) -> str:
        return self._model

    def key(self, system: str, messages: Sequence[ChatMessage], schema: dict[str, Any]) -> str:
        payload = {
            "model": self._model,
            "system": system,
            "messages": [m.model_dump() for m in messages],
            "schema": schema,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    async def complete(
        self, *, system: str, messages: Sequence[ChatMessage], schema: dict[str, Any]
    ) -> dict[str, JsonValue]:
        key = self.key(system, messages, schema)
        if key in self._answers:
            return self._answers[key]
        if self._live is None:
            raise CassetteMiss(
                f"no recorded answer in {self._path.name} for this request; re-record live"
            )
        answer = await self._live.complete(system=system, messages=messages, schema=schema)
        self._answers[key] = answer
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a") as file:
            file.write(json.dumps({"key": key, "answer": answer}, sort_keys=True) + "\n")
        return answer
