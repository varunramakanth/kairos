"""An in-memory ``Checkpointer`` for tests, evals and one-off CLI runs."""

from collections.abc import Sequence

from kairos_domain.errors import LostLeaseError
from kairos_domain.events import DurableEvent
from kairos_domain.run import RunState


class InMemoryCheckpointer:
    """Keeps snapshots as JSON so a loaded state shares nothing with the saved one.

    That mirrors what a database does and catches code that relies on object
    identity surviving a checkpoint.
    """

    def __init__(self) -> None:
        self._states: dict[str, str] = {}
        self._events: dict[str, list[DurableEvent]] = {}
        self._blobs: dict[str, str] = {}
        self._revoked: set[str] = set()
        self.save_count = 0

    async def load(self, run_id: str) -> RunState | None:
        raw = self._states.get(run_id)
        return RunState.model_validate_json(raw) if raw is not None else None

    async def save(self, state: RunState, events: Sequence[DurableEvent]) -> None:
        if state.run_id in self._revoked:
            raise LostLeaseError(f"run {state.run_id} is no longer owned by this process")
        self._states[state.run_id] = state.model_dump_json()
        self._events.setdefault(state.run_id, []).extend(events)
        self.save_count += 1

    async def put_blob(self, run_id: str, content: str) -> str:
        blob_id = f"blob_{run_id}_{len(self._blobs) + 1:04d}"
        self._blobs[blob_id] = content
        return blob_id

    async def get_blob(self, blob_id: str) -> str | None:
        return self._blobs.get(blob_id)

    # --- test helpers -----------------------------------------------------------

    def events(self, run_id: str) -> list[DurableEvent]:
        """Durable events stored for a run, in order."""
        return list(self._events.get(run_id, []))

    def revoke(self, run_id: str) -> None:
        """Simulate another worker taking the run: the next ``save`` raises."""
        self._revoked.add(run_id)
