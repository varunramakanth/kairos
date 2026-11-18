"""Constructors for the two supported backends."""

from anthropic import AsyncAnthropic
from anthropic.lib.bedrock import AsyncAnthropicBedrockMantle

from kairos_providers.messages import MessagesOptions, MessagesProvider

# Models that support server-side refusal fallbacks (docs, 2026-11). Haiku 5.5 does not.
SERVER_FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-sonnet-5-5"})


def anthropic_provider(
    client: AsyncAnthropic | None = None, *, refusal_fallbacks: bool = True
) -> MessagesProvider:
    """Claude on Anthropic's API.

    With no client given, credentials come from the environment
    (``ANTHROPIC_API_KEY`` or an ``ant auth login`` profile).

    Args:
        refusal_fallbacks: Ask the API to re-run a refused request on its default
            fallback model. A refusal that survives the fallback still ends the run
            with ``StopReason.REFUSED``.
    """
    options = MessagesOptions(
        server_fallback_models=SERVER_FALLBACK_MODELS if refusal_fallbacks else frozenset()
    )
    return MessagesProvider(client or AsyncAnthropic(), name="anthropic", options=options)


def bedrock_provider(
    client: AsyncAnthropicBedrockMantle | None = None,
    *,
    region: str | None = None,
    model_prefix: str = "anthropic.",
) -> MessagesProvider:
    """Claude on Amazon Bedrock, through the Messages-API (Mantle) endpoint.

    With no client given, AWS credentials come from the standard chain and
    ``region`` is required.

    Args:
        model_prefix: Prepended to bare model IDs. Use an inference profile prefix
            such as ``us.anthropic.`` or ``global.anthropic.`` where the model has
            no in-region inference.

    Bedrock has no server-side refusal fallbacks. The SDK's client-side fallback
    middleware keeps per-conversation state in memory, which cannot be
    checkpointed, so it is not used: a refusal ends the run as ``REFUSED``.
    """
    if client is None:
        if region is None:
            raise ValueError("bedrock_provider needs a region or a ready-made client")
        client = AsyncAnthropicBedrockMantle(aws_region=region)
    return MessagesProvider(
        client, name="aws.bedrock", options=MessagesOptions(model_prefix=model_prefix)
    )
