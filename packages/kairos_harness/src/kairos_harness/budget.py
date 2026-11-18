"""Budgets and pricing.

Budgets are checked between steps, so a run can overshoot a cap by at most one
model call or one tool batch. Prices are per million tokens, from Anthropic's
first-party price list (checked 2026-11). Bedrock bills separately and may differ.
"""

from dataclasses import dataclass

from kairos_domain.run import RunState
from kairos_domain.usage import Budget, Usage


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    cache_write_per_mtok: float

    def cost(self, usage: Usage) -> float:
        return (
            usage.input_tokens * self.input_per_mtok
            + usage.output_tokens * self.output_per_mtok
            + usage.cache_read_input_tokens * self.cache_read_per_mtok
            + usage.cache_creation_input_tokens * self.cache_write_per_mtok
        ) / 1_000_000


def _price(input_: float, output: float, cache_read: float) -> ModelPrice:
    # A 5-minute cache write costs 1.25x the input price.
    return ModelPrice(input_, output, cache_read, input_ * 1.25)


DEFAULT_PRICES: dict[str, ModelPrice] = {
    "claude-opus-5-5": _price(4.00, 20.00, 0.20),
    "claude-sonnet-5-5": _price(2.00, 10.00, 0.10),
    # Haiku 5.5 costs more beyond 100K prompt tokens; this is the base tier.
    "claude-haiku-5-5": _price(0.10, 0.50, 0.01),
}


class PriceTable:
    """Looks up prices by model ID, tolerating Bedrock prefixes such as ``us.anthropic.``."""

    def __init__(self, prices: dict[str, ModelPrice] | None = None) -> None:
        self._prices = dict(DEFAULT_PRICES if prices is None else prices)

    def lookup(self, model: str) -> ModelPrice | None:
        bare = model.rsplit("anthropic.", 1)[-1]
        return self._prices.get(bare)

    def cost(self, model: str, usage: Usage) -> float:
        """Cost in USD, or 0.0 when the model has no known price."""
        price = self.lookup(model)
        return price.cost(usage) if price else 0.0


class BudgetStop(Exception):  # a stop signal, not an error
    """Raised by a hook (e.g. a daily spend cap) to end the run as ``budget_exceeded``.
    The message is shown to the user."""


def exceeded(budget: Budget, state: RunState, *, next_tool_calls: int = 0) -> str | None:
    """Return a description of the first cap the run has hit, or ``None``.

    ``next_tool_calls`` is the size of the batch about to run, so a batch that would
    cross ``max_tool_calls`` is refused before it starts.
    """
    spent = state.spent
    if budget.max_steps is not None and spent.steps >= budget.max_steps and not next_tool_calls:
        return f"step limit reached ({budget.max_steps})"
    if budget.max_total_tokens is not None and spent.total_tokens >= budget.max_total_tokens:
        return f"token budget reached ({spent.total_tokens} >= {budget.max_total_tokens})"
    if budget.max_cost_usd is not None and spent.cost_usd >= budget.max_cost_usd:
        return f"cost budget reached (${spent.cost_usd:.4f} >= ${budget.max_cost_usd:.4f})"
    if budget.max_wall_seconds is not None and spent.wall_seconds >= budget.max_wall_seconds:
        return f"time budget reached ({spent.wall_seconds:.1f}s >= {budget.max_wall_seconds:.1f}s)"
    if (
        budget.max_tool_calls is not None
        and spent.tool_calls + next_tool_calls > budget.max_tool_calls
        and (next_tool_calls or spent.tool_calls >= budget.max_tool_calls)
    ):
        return f"tool-call budget reached ({budget.max_tool_calls})"
    return None
