"""Claude over the Messages API, on Anthropic's API or Amazon Bedrock.

Both backends speak the same wire format through the ``anthropic`` SDK
(``AsyncAnthropic`` and ``AsyncAnthropicBedrockMantle``), so one translation layer
serves both. The only differences are the client, the model ID prefix and whether
server-side refusal fallbacks exist.

API facts this module relies on (checked against the docs, 2026-11):

- Thinking is adaptive and cannot be disabled on current models; ``effort`` is the
  cost control. Sampling parameters and forced ``tool_choice`` are rejected.
- Thinking blocks must be sent back unchanged. They are kept as ``OpaqueBlock`` and
  replayed verbatim.
- Prompt caching is a prefix match over tools, then system, then messages.
- With eager input streaming the API does not validate tool input; the tool
  registry validates it before running anything.
"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

import anthropic
from anthropic import AsyncAnthropic
from anthropic.lib.bedrock import AsyncAnthropicBedrockMantle
from pydantic import JsonValue

from kairos_domain.content import (
    ContentBlock,
    Message,
    OpaqueBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from kairos_domain.errors import ProviderError
from kairos_domain.model import (
    ModelCompleted,
    ModelEvent,
    ModelRequest,
    ModelStopReason,
    ModelTextDelta,
    ModelToolCallArgsDelta,
    ModelToolCallStart,
)
from kairos_domain.tools import ToolSpec
from kairos_domain.usage import Usage

WIRE_FORMAT = "anthropic"
"""``OpaqueBlock.provider`` value for blocks in Messages API wire format."""

SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"

_STOP_REASONS: Mapping[str, ModelStopReason] = {
    "end_turn": ModelStopReason.END_TURN,
    "stop_sequence": ModelStopReason.END_TURN,
    "tool_use": ModelStopReason.TOOL_USE,
    "max_tokens": ModelStopReason.MAX_TOKENS,
    "refusal": ModelStopReason.REFUSAL,
}


@dataclass(frozen=True)
class MessagesOptions:
    model_prefix: str = ""
    """Prepended to model IDs that do not already carry it (Bedrock: ``anthropic.``,
    or an inference profile prefix such as ``us.anthropic.``)."""
    server_fallback_models: frozenset[str] = field(default_factory=frozenset[str])
    """Models for which a refused request is re-run server-side on a fallback model
    (``fallbacks="default"``). Anthropic API only."""
    eager_input_streaming: bool = True
    """Stream tool input as it is generated instead of buffering it server-side."""


class MessagesProvider:
    """Implements the harness ``Provider`` protocol over the Messages API."""

    def __init__(
        self,
        client: AsyncAnthropic | AsyncAnthropicBedrockMantle,
        *,
        name: str,
        options: MessagesOptions | None = None,
    ) -> None:
        self._client = client
        self._name = name
        self._options = options or MessagesOptions()

    @property
    def name(self) -> str:
        return self._name

    def build_params(self, request: ModelRequest) -> dict[str, Any]:
        """Translate a ``ModelRequest`` into keyword arguments for the SDK.

        Public so tests can assert on the exact request without a network call.
        """
        options = self._options
        params: dict[str, Any] = {
            "model": self._model_id(request.model),
            "max_tokens": request.max_output_tokens,
            "messages": [_message_param(m) for m in request.messages],
            "thinking": {"type": "adaptive"},
            # Caches the conversation prefix up to the last block of this request.
            "cache_control": {"type": "ephemeral"},
        }
        if request.system:
            # A second breakpoint after tools + system, so a new conversation with
            # the same tools and system prompt still gets a cache hit.
            params["system"] = [
                {"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}
            ]
        if request.tools:
            params["tools"] = [
                _tool_param(t, eager=options.eager_input_streaming) for t in request.tools
            ]
        if request.effort:
            params["output_config"] = {"effort": request.effort}
        if request.model in options.server_fallback_models:
            params["fallbacks"] = "default"
            params["betas"] = [SERVER_FALLBACK_BETA]
        return params

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        params = self.build_params(request)
        tool_ids: dict[int, str] = {}
        try:
            async with self._client.beta.messages.stream(**params) as stream:
                async for event in stream:
                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            tool_ids[event.index] = block.id
                            yield ModelToolCallStart(tool_call_id=block.id, name=block.name)
                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield ModelTextDelta(text=delta.text)
                        elif delta.type == "input_json_delta" and event.index in tool_ids:
                            yield ModelToolCallArgsDelta(
                                tool_call_id=tool_ids[event.index], partial_json=delta.partial_json
                            )
                final = await stream.get_final_message()
        except anthropic.RateLimitError as error:
            raise ProviderError(str(error), retryable=True, status_code=429) from error
        except anthropic.APIStatusError as error:
            retryable = error.status_code >= 500 or error.status_code in (408, 409)
            raise ProviderError(
                str(error), retryable=retryable, status_code=error.status_code
            ) from error
        except anthropic.APIConnectionError as error:
            raise ProviderError(f"connection error: {error}", retryable=True) from error
        except ValueError as error:
            # The SDK could not parse streamed tool input at all. Re-issue the turn.
            raise ProviderError(f"unparseable tool input: {error}", retryable=True) from error

        usage = final.usage
        details = final.stop_details
        yield ModelCompleted(
            message=Message(
                role="assistant",
                content=tuple(
                    _domain_block(b.to_dict(mode="json")) for b in final.content if _keep(b)
                ),
            ),
            stop_reason=_STOP_REASONS.get(final.stop_reason or "", ModelStopReason.OTHER),
            usage=Usage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=usage.cache_read_input_tokens or 0,
                cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            ),
            model=final.model,
            refusal_category=details.category if details and details.category else None,
        )

    def _model_id(self, model: str) -> str:
        prefix = self._options.model_prefix
        if not prefix or "anthropic." in model:
            return model
        return f"{prefix}{model}"


def _keep(block: Any) -> bool:
    # The API rejects empty text blocks when a history is replayed.
    return not (block.type == "text" and not block.text)


def _domain_block(raw: dict[str, object]) -> ContentBlock:
    """Convert one response block. Anything we do not interpret is kept verbatim."""
    data = cast("dict[str, JsonValue]", raw)
    kind = str(data.get("type"))
    if kind == "text":
        return TextBlock(text=str(data.get("text", "")))
    if kind == "tool_use":
        tool_input = data.get("input")
        return ToolUseBlock(
            id=str(data["id"]),
            name=str(data["name"]),
            input=tool_input if isinstance(tool_input, dict) else {},
        )
    return OpaqueBlock(provider=WIRE_FORMAT, kind=kind, data=data)


def _block_param(block: ContentBlock) -> dict[str, JsonValue] | None:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        return {
            "type": "tool_result",
            "tool_use_id": block.tool_use_id,
            "content": block.content,
            "is_error": block.is_error,
        }
    # Replay provider-native blocks byte-for-byte; drop ones from another wire format.
    return dict(block.data) if block.provider == WIRE_FORMAT else None


def _message_param(message: Message) -> dict[str, Any]:
    blocks = [p for b in message.content if (p := _block_param(b)) is not None]
    return {"role": message.role, "content": blocks}


def _tool_param(spec: ToolSpec, *, eager: bool) -> dict[str, Any]:
    param: dict[str, Any] = {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.input_schema,
    }
    if eager:
        param["eager_input_streaming"] = True
    return param
