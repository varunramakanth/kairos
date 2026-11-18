"""Golden cases recorded from live model and tool calls (``just record-live``).

Unlike ``scenarios.py``, nothing here is hand-written: the model turns and the tool
results are what Claude and the web actually returned on the recording date. The
replay therefore measures real behaviour, frozen in time.
"""

from pathlib import Path

ROOT = Path(__file__).parent
DATASET = ROOT / "datasets" / "live.jsonl"
CASSETTES = ROOT / "cassettes" / "live"
MODEL = "claude-sonnet-5-5"


def model_cassette(case_id: str) -> Path:
    return CASSETTES / f"{case_id}.jsonl"


def tool_cassette(case_id: str) -> Path:
    return CASSETTES / f"{case_id}.tools.jsonl"
