"""Token usage and budgets."""

from kairos_domain.base import DomainModel


class Usage(DomainModel):
    """Token counts for one model call, or a running total.

    ``input_tokens`` counts only uncached input, as the Anthropic API reports it.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        """Size of the prompt, cached or not."""
        return self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + other.cache_creation_input_tokens,
        )


class Budget(DomainModel):
    """Hard caps for one run. ``None`` means no cap on that dimension."""

    max_total_tokens: int | None = None
    max_cost_usd: float | None = None
    max_wall_seconds: float | None = None
    max_tool_calls: int | None = None
    max_steps: int | None = 50


class BudgetSpent(DomainModel):
    """What a run has consumed so far, in the same dimensions as :class:`Budget`."""

    total_tokens: int = 0
    cost_usd: float = 0.0
    wall_seconds: float = 0.0
    tool_calls: int = 0
    steps: int = 0
