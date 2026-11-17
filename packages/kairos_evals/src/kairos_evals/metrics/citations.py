"""Layer 2 (components): RAG and web research. Are the answer's citations grounded?"""

import re

from kairos_evals.trace import Trace
from kairos_evals.types import Example, Kind, Layer, Score

OUTPUT_ATTRIBUTE = "output.value"
_LINK = re.compile(r"\]\((https?://[^)\s]+)\)")


def _normalize(url: str) -> str:
    return url.rstrip("/").removeprefix("https://").removeprefix("http://").removeprefix("www.")


class CitationGrounding:
    """Every link cited in the final answer appeared in a tool result of the same run.

    This catches invented or misremembered sources without a judge model. It needs
    content in the trace (``output.value`` on the root and tool spans), which
    Kairos records only when content capture is on, as in eval runs.

    The score is the share of cited links that are grounded; it passes only when
    all are and there are at least ``min_citations``.
    """

    id = "citation_grounding"
    layer: Layer = "component"
    kind: Kind = "deterministic"

    def __init__(self, min_citations: int = 1) -> None:
        self._min = min_citations

    async def evaluate(self, trace: Trace, example: Example | None = None) -> Score:
        root = trace.root
        answer = root.attributes.get(OUTPUT_ATTRIBUTE) if root else None
        if not isinstance(answer, str):
            return Score(value=0.0, passed=False, reason="no answer text in the trace")
        cited = list(dict.fromkeys(_LINK.findall(answer)))
        seen = " ".join(
            value
            for span in trace.tool_calls
            if isinstance(value := span.attributes.get(OUTPUT_ATTRIBUTE), str)
        )
        haystack = _normalize(seen.replace("https://", "").replace("http://", ""))
        ungrounded = [url for url in cited if _normalize(url) not in haystack]
        if not cited:
            return Score(value=0.0, passed=self._min == 0, reason="the answer cites no links")
        value = (len(cited) - len(ungrounded)) / len(cited)
        enough = len(cited) >= self._min
        reason = f"{len(cited) - len(ungrounded)}/{len(cited)} citations grounded"
        if ungrounded:
            reason += f"; not in any tool result: {ungrounded}"
        if not enough:
            reason += f"; fewer than {self._min} citations"
        return Score(value=value, passed=not ungrounded and enough, reason=reason)
