"""Result sinks."""

from collections.abc import Sequence
from pathlib import Path

from kairos_evals.types import EvalResult


class JsonlSink:
    """Appends results to a JSONL file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    async def write(self, results: Sequence[EvalResult]) -> None:
        with self._path.open("a") as file:
            file.writelines(result.model_dump_json() + "\n" for result in results)
