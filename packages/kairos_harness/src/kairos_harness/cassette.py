"""Record/replay of model calls at the provider boundary (ADR-005).

A cassette is a JSONL file. Each line is one model call: a fingerprint of the
normalized request and the events the provider produced for it. Because recording
happens above the SDK and transport, cassettes survive SDK upgrades, never contain
credentials, and work for any provider.

``RecordingProvider`` and ``ReplayProvider`` both satisfy the ``Provider`` protocol,
so the loop cannot tell them from a live backend.
"""

import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path

from kairos_domain.base import DomainModel
from kairos_domain.errors import KairosError
from kairos_domain.model import ModelEvent, ModelRequest
from kairos_harness.protocols import Provider


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
            yield event
        # Only complete calls are recorded; a failed stream leaves no entry.
        entry = CassetteEntry(
            fingerprint=fingerprint(request), summary=_summary(request), events=events
        )
        with self._path.open("a") as file:
            file.write(entry.model_dump_json() + "\n")


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
