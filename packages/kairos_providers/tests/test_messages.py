"""The provider against the real SDK, with the HTTP transport replaced.

Nothing here touches the network: ``httpx2.MockTransport`` answers with hand-written
server-sent events in the Messages API wire format, so the SDK's own parsing and
our translation are both exercised.
"""

import json
from collections.abc import Sequence
from typing import Any

import httpx2
import pytest
from anthropic import AsyncAnthropic
from anthropic.lib.bedrock import AsyncAnthropicBedrockMantle
from pydantic import JsonValue

from kairos_domain.content import (
    Message,
    OpaqueBlock,
    Provenance,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    user_text,
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
from kairos_providers import MessagesProvider, anthropic_provider, bedrock_provider

THINKING: dict[str, JsonValue] = {"type": "thinking", "thinking": "", "signature": "sig-abc"}
ECHO = ToolSpec(name="echo", description="Echo", input_schema={"type": "object"})


def sse(events: Sequence[dict[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def tool_use_stream(model: str = "claude-opus-5-5") -> bytes:
    """Thinking, then text, then one tool call."""
    usage = {
        "input_tokens": 12,
        "output_tokens": 1,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 40,
    }
    message: dict[str, Any] = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": usage,
    }
    start, delta, stop = "content_block_start", "content_block_delta", "content_block_stop"
    return sse(
        [
            {"type": "message_start", "message": message},
            {"type": start, "index": 0, "content_block": {**THINKING, "signature": ""}},
            {
                "type": delta,
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "sig-abc"},
            },
            {"type": stop, "index": 0},
            {"type": start, "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": delta, "index": 1, "delta": {"type": "text_delta", "text": "Let me "}},
            {"type": delta, "index": 1, "delta": {"type": "text_delta", "text": "check."}},
            {"type": stop, "index": 1},
            {
                "type": start,
                "index": 2,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "echo", "input": {}},
            },
            {
                "type": delta,
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '{"q": '},
            },
            {
                "type": delta,
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '"hi"}'},
            },
            {"type": stop, "index": 2},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 57},
            },
            {"type": "message_stop"},
        ]
    )


class FakeApi:
    """Answers every request with the same response and records what was sent."""

    def __init__(self, body: bytes = b"", status: int = 200) -> None:
        self.body = body
        self.status = status
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.status != 200:
            error = {"type": "error", "error": {"type": "api_error", "message": "nope"}}
            return httpx2.Response(self.status, json=error)
        return httpx2.Response(
            200, content=self.body, headers={"content-type": "text/event-stream"}
        )

    def http_client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self))

    def anthropic(self, *, refusal_fallbacks: bool = True) -> MessagesProvider:
        client = AsyncAnthropic(api_key="test-key", http_client=self.http_client(), max_retries=0)
        return anthropic_provider(client, refusal_fallbacks=refusal_fallbacks)

    @property
    def sent(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


def request(**overrides: object) -> ModelRequest:
    base: dict[str, object] = {
        "model": "claude-opus-5-5",
        "system": "You are Kairos.",
        "messages": (user_text("hi"),),
        "tools": (ECHO,),
        "effort": "medium",
    }
    return ModelRequest.model_validate({**base, **overrides})


async def collect(provider: MessagesProvider, model_request: ModelRequest) -> list[ModelEvent]:
    return [event async for event in provider.stream(model_request)]


async def test_stream_is_translated_into_model_events() -> None:
    api = FakeApi(tool_use_stream())
    events = await collect(api.anthropic(), request())

    assert events[:-1] == [
        ModelTextDelta(text="Let me "),
        ModelTextDelta(text="check."),
        ModelToolCallStart(tool_call_id="toolu_1", name="echo"),
        ModelToolCallArgsDelta(tool_call_id="toolu_1", partial_json='{"q": '),
        ModelToolCallArgsDelta(tool_call_id="toolu_1", partial_json='"hi"}'),
    ]
    completed = events[-1]
    assert isinstance(completed, ModelCompleted)
    assert completed.stop_reason is ModelStopReason.TOOL_USE
    assert completed.model == "claude-opus-5-5"
    assert completed.usage == Usage(
        input_tokens=12,
        output_tokens=57,
        cache_read_input_tokens=900,
        cache_creation_input_tokens=40,
    )
    assert completed.message.content == (
        OpaqueBlock(provider="anthropic", kind="thinking", data=THINKING),
        TextBlock(text="Let me check."),
        ToolUseBlock(id="toolu_1", name="echo", input={"q": "hi"}),
    )


async def test_request_uses_adaptive_thinking_effort_caching_and_fallbacks() -> None:
    api = FakeApi(tool_use_stream())
    await collect(api.anthropic(), request())
    sent = api.sent

    assert sent["model"] == "claude-opus-5-5"
    assert sent["stream"] is True
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"] == {"effort": "medium"}
    assert sent["cache_control"] == {"type": "ephemeral"}
    assert sent["system"] == [
        {"type": "text", "text": "You are Kairos.", "cache_control": {"type": "ephemeral"}}
    ]
    assert sent["tools"] == [
        {
            "name": "echo",
            "description": "Echo",
            "input_schema": {"type": "object"},
            "eager_input_streaming": True,
        }
    ]
    assert sent["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in api.requests[-1].headers["anthropic-beta"]
    # Rejected by current models, so they must never be sent.
    assert not {"temperature", "top_p", "top_k", "tool_choice"} & sent.keys()


async def test_fallbacks_are_only_requested_where_supported() -> None:
    api = FakeApi(tool_use_stream())
    await collect(api.anthropic(), request(model="claude-haiku-5-5"))
    assert "fallbacks" not in api.sent

    await collect(api.anthropic(refusal_fallbacks=False), request())
    assert "fallbacks" not in api.sent
    assert "anthropic-beta" not in api.requests[-1].headers


async def test_history_is_replayed_with_thinking_blocks_unchanged() -> None:
    history = (
        user_text("hi"),
        Message(
            role="assistant",
            content=(
                OpaqueBlock(provider="anthropic", kind="thinking", data=THINKING),
                TextBlock(text="Let me check."),
                ToolUseBlock(id="toolu_1", name="echo", input={"q": "hi"}),
            ),
        ),
        Message(
            role="user",
            content=(
                ToolResultBlock(
                    tool_use_id="toolu_1", content="echo:hi", provenance=Provenance.UNTRUSTED
                ),
            ),
        ),
    )
    api = FakeApi(tool_use_stream())
    await collect(api.anthropic(), request(messages=history))

    assert api.sent["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [
                THINKING,
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "toolu_1", "name": "echo", "input": {"q": "hi"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": "echo:hi",
                    "is_error": False,
                }
            ],
        },
    ]


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (500, True), (529, True), (400, False), (401, False), (404, False)],
)
async def test_http_errors_map_to_provider_errors(status: int, retryable: bool) -> None:
    api = FakeApi(status=status)
    with pytest.raises(ProviderError) as error:
        await collect(api.anthropic(), request())
    assert error.value.retryable is retryable
    assert error.value.status_code == status


async def test_connection_errors_are_retryable() -> None:
    def refuse(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    client = AsyncAnthropic(
        api_key="test-key",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(refuse)),
        max_retries=0,
    )
    with pytest.raises(ProviderError) as error:
        await collect(anthropic_provider(client), request())
    assert error.value.retryable


async def test_bedrock_uses_the_mantle_endpoint_and_prefixed_model_ids() -> None:
    api = FakeApi(tool_use_stream("anthropic.claude-opus-5-5"))
    client = AsyncAnthropicBedrockMantle(
        aws_region="ca-central-1",
        aws_access_key="AKIAFAKEFAKEFAKEFAKE",
        aws_secret_key="fake-secret",  # noqa: S106  # not a real credential
        http_client=api.http_client(),
        max_retries=0,
    )
    provider = bedrock_provider(client, model_prefix="us.anthropic.")
    events = await collect(provider, request())

    assert provider.name == "aws.bedrock"
    assert isinstance(events[-1], ModelCompleted)
    sent = api.requests[-1]
    assert sent.url.host == "bedrock-mantle.ca-central-1.api.aws"
    assert api.sent["model"] == "us.anthropic.claude-opus-5-5"
    assert "fallbacks" not in api.sent
    assert "AWS4-HMAC-SHA256" in sent.headers["authorization"]  # signed with SigV4


def test_bedrock_needs_a_region() -> None:
    with pytest.raises(ValueError, match="region"):
        bedrock_provider()
