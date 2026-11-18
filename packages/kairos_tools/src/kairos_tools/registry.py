"""The ``@tool`` decorator and the registry that runs tools for the harness.

One schema format for every tool: the input is a Pydantic model, and its JSON
Schema is what the model provider receives::

    class SearchInput(BaseModel):
        query: str = Field(description="What to search for")

    @tool(provenance=Provenance.UNTRUSTED)
    async def web_search(args: SearchInput, context: ToolContext) -> str:
        '''Search the web and return the top results.'''
        ...

``ToolRegistry`` satisfies the harness ``ToolExecutor`` protocol.
"""

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast, get_type_hints

from pydantic import BaseModel, JsonValue, ValidationError

from kairos_domain.content import Provenance
from kairos_domain.run import ToolCallRecord
from kairos_domain.tools import ToolContext, ToolOutput, ToolRisk, ToolSpec

type ToolFunction[M: BaseModel] = Callable[[M, ToolContext], Awaitable[str | ToolOutput]]


@dataclass(frozen=True)
class Tool[M: BaseModel]:
    """A registered tool: its public description plus how to validate and run it."""

    spec: ToolSpec
    input_model: type[M]
    function: ToolFunction[M]

    async def __call__(self, raw_input: dict[str, JsonValue], context: ToolContext) -> ToolOutput:
        """Validate ``raw_input`` and run the tool.

        Invalid input is reported back to the model as an error result so it can
        correct itself. This check matters: with eager input streaming the provider
        does not validate tool input for us.
        """
        try:
            args = self.input_model.model_validate(raw_input)
        except ValidationError as error:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'input'}: {e['msg']}"
                for e in error.errors()
            )
            return ToolOutput(
                content=f"invalid input for {self.spec.name}: {problems}", is_error=True
            )
        result = await self.function(args, context)
        return result if isinstance(result, ToolOutput) else ToolOutput(content=result)


def tool[M: BaseModel](
    *,
    name: str | None = None,
    description: str | None = None,
    risk: ToolRisk = ToolRisk.READ,
    idempotent: bool = True,
    provenance: Provenance = Provenance.TRUSTED,
) -> Callable[[ToolFunction[M]], Tool[M]]:
    """Turn an async function into a :class:`Tool`.

    The name defaults to the function name and the description to its docstring.
    The input model is read from the type of the first parameter.

    Args:
        risk: What the call can affect. Policy uses this to decide on approval.
        idempotent: Whether an interrupted call may be re-run automatically. Set to
            ``False`` for anything with an external side effect that has no
            idempotency key (sending email, posting).
        provenance: ``UNTRUSTED`` when the result contains content from outside
            (web pages, email, documents). The harness then wraps it as data.
    """

    def decorate(function: ToolFunction[M]) -> Tool[M]:
        parameters = list(inspect.signature(function).parameters)
        hints = get_type_hints(function)
        if not parameters or parameters[0] not in hints:
            raise TypeError(f"{function.__name__}: the first parameter needs a type annotation")
        input_model = hints[parameters[0]]
        if not (inspect.isclass(input_model) and issubclass(input_model, BaseModel)):
            raise TypeError(f"{function.__name__}: the first parameter must be a Pydantic model")
        doc = description or inspect.getdoc(function)
        if not doc:
            raise TypeError(f"{function.__name__}: a tool needs a docstring or a description")
        schema = cast("dict[str, JsonValue]", input_model.model_json_schema())
        spec = ToolSpec(
            name=name or function.__name__,
            description=doc,
            input_schema=schema,
            risk=risk,
            idempotent=idempotent,
            provenance=provenance,
        )
        return Tool(spec=spec, input_model=cast("type[M]", input_model), function=function)

    return decorate


class ToolRegistry:
    """Holds tools in registration order and runs them by name."""

    def __init__(self, tools: Sequence[Tool[Any]] = ()) -> None:
        self._tools: dict[str, Tool[Any]] = {}
        for item in tools:
            self.register(item)

    def register(self, item: Tool[Any]) -> None:
        if item.spec.name in self._tools:
            raise ValueError(f"a tool named {item.spec.name!r} is already registered")
        self._tools[item.spec.name] = item

    def specs(self) -> Sequence[ToolSpec]:
        # Registration order is stable, which keeps the provider's prompt cache valid.
        return [item.spec for item in self._tools.values()]

    async def execute(self, call: ToolCallRecord, context: ToolContext) -> ToolOutput:
        item = self._tools.get(call.name)
        if item is None:
            return ToolOutput(content=f"unknown tool: {call.name}", is_error=True)
        return await item(call.input, context)
