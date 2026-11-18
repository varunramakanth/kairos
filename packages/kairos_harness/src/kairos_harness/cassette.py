"""Record/replay of model calls at the provider boundary (ADR-005).

A cassette is a JSONL file. Each line is one model call: a fingerprint of the
normalized request and the events the provider produced for it. Because recording
happens above the SDK and transport, cassettes survive SDK upgrades, never contain
credentials, and work for any provider.

``RecordingProvider`` and ``ReplayProvider`` both satisfy the ``Provider`` protocol,
so the loop cannot tell them from a live backend.

Live recordings also need the tools' results, since the web changes between runs:
``RecordingToolExecutor`` and ``ReplayToolExecutor`` do the same at the tool
boundary. Unit tests of individual HTTP tools use a mocked transport instead.
"""

import hashlib
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path

from pydantic import JsonValue

from kairos_domain.base import DomainModel
from kairos_domain.errors import KairosError
from kairos_domain.model import ModelCompleted, ModelEvent, ModelRequest
from kairos_domain.run import ToolCallRecord
from kairos_domain.tools import ToolContext, ToolOutput, ToolSpec
from kairos_harness.protocols import Provider, ToolExecutor


class CassetteMissError(KairosError):
    """Replay was asked for a model call the cassette does not contain.

    This is a test failure, never a reason to call the network. It usually means a
    prompt, tool schema or tool result changed and the cassette must be re-recorded.
    """


class CassetteEntry(DomainModel):
    fingerprint: str
    summary: str
    """Human-readable hint about the request, for diffs and miss messages."""
    events: list[ModelEvent]


def fingerprint(request: ModelRequest) -> str:
    """A stable hash of everything in the request that the model's answer depends on."""
    canonical = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _summary(request: ModelRequest) -> str:
    last = request.messages[-1] if request.messages else None
    preview = ""
    if last is not None:
        preview = (last.text or f"[{', '.join(b.type for b in last.content)}]")[:60]
    return f"{request.model} | {len(request.messages)} messages | last {preview!r}"


def load_cassette(path: Path) -> list[CassetteEntry]:
    lines = path.read_text().splitlines()
    return [CassetteEntry.model_validate_json(line) for line in lines if line.strip()]


class RecordingProvider:
    """Wraps a provider and appends every completed model call to a cassette file."""

    def __init__(self, inner: Provider, path: Path) -> None:
        self._inner = inner
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    @property
    def name(self) -> str:
        return self._inner.name

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        events: list[ModelEvent] = []
        async for event in self._inner.stream(request):
            events.append(event)
            if isinstance(event, ModelCompleted):
                # Write before yielding: consumers stop iterating at the completion,
                # so code after the loop would never run. A failed stream never
                # reaches this point and leaves no entry.
                entry = CassetteEntry(
                    fingerprint=fingerprint(request), summary=_summary(request), events=events
                )
                with self._path.open("a") as file:
                    file.write(entry.model_dump_json() + "\n")
            yield event


class ReplayProvider:
    """Serves model calls from a cassette. Never touches the network.

    Calls are matched by fingerprint. Identical requests are served in the order
    they were recorded.
    """

    def __init__(self, path: Path, *, name: str = "replay") -> None:
        self._path = path
        self._name = name
        self._queues: dict[str, list[CassetteEntry]] = {}
        self._entries = load_cassette(path)
        for entry in self._entries:
            self._queues.setdefault(entry.fingerprint, []).append(entry)

    @property
    def name(self) -> str:
        return self._name

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        key = fingerprint(request)
        queue = self._queues.get(key)
        if not queue:
            recorded = "\n".join(f"  {e.fingerprint}  {e.summary}" for e in self._entries)
            raise CassetteMissError(
                f"no recorded model call matches {key} ({_summary(request)}) in {self._path}.\n"
                f"Recorded calls:\n{recorded or '  (none)'}\n"
                "Re-record the cassette if the prompt, tools or tool results changed."
            )
        for event in queue.pop(0).events:
            yield event


# --- tool cassettes ---------------------------------------------------------------------


class ToolCassetteEntry(DomainModel):
    name: str
    input: dict[str, JsonValue]
    output: ToolOutput


class ToolCassetteHeader(DomainModel):
    """First line of a tool cassette: the tool set the run was recorded with.

    Replay serves these specs, so a recorded case replays identically even where the
    live tool set differs (for example, CI has no search API key).
    """

    specs: list[ToolSpec]


def _tool_key(name: str, tool_input: Mapping[str, JsonValue]) -> str:
    return f"{name}:{json.dumps(tool_input, sort_keys=True, separators=(',', ':'))}"


class RecordingToolExecutor:
    """Wraps a real ``ToolExecutor`` and records every call's input and output."""

    def __init__(self, inner: ToolExecutor, path: Path) -> None:
        self._inner = inner
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        header = ToolCassetteHeader(specs=list(inner.specs()))
        path.write_text(header.model_dump_json() + "\n")

    def specs(self) -> Sequence[ToolSpec]:
        return self._inner.specs()

    async def execute(self, call: ToolCallRecord, context: ToolContext) -> ToolOutput:
        output = await self._inner.execute(call, context)
        entry = ToolCassetteEntry(name=call.name, input=call.input, output=output)
        with self._path.open("a") as file:
            file.write(entry.model_dump_json() + "\n")
        return output


class ReplayToolExecutor:
    """Serves tool results from a tool cassette. Never runs a real tool."""

    def __init__(self, path: Path) -> None:
        lines = [line for line in path.read_text().splitlines() if line.strip()]
        if not lines:
            raise CassetteMissError(f"empty tool cassette: {path}")
        self._path = path
        self._specs = ToolCassetteHeader.model_validate_json(lines[0]).specs
        self._queues: dict[str, list[ToolOutput]] = {}
        for line in lines[1:]:
            entry = ToolCassetteEntry.model_validate_json(line)
            self._queues.setdefault(_tool_key(entry.name, entry.input), []).append(entry.output)

    def specs(self) -> Sequence[ToolSpec]:
        return self._specs

    async def execute(self, call: ToolCallRecord, context: ToolContext) -> ToolOutput:
        queue = self._queues.get(_tool_key(call.name, call.input))
        if not queue:
            raise CassetteMissError(
                f"no recorded result for {call.name} {call.input} in {self._path}"
            )
        return queue.pop(0)
