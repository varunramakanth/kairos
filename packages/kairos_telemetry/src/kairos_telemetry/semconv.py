"""Attribute names used on Kairos spans.

The OpenTelemetry GenAI semantic conventions are still in "Development" status and
live in their own repository (``semantic-conventions-genai``). Names are written out
here instead of imported from the SDK's private ``_incubating`` module, so a change
upstream is a deliberate edit to this file. Checked against the spec on 2026-11-16.
"""

SEMCONV_NOTE = "OpenTelemetry GenAI semantic conventions (Development), checked 2026-11-16"

# --- gen_ai.* (spec) ------------------------------------------------------------------
OPERATION_NAME = "gen_ai.operation.name"
PROVIDER_NAME = "gen_ai.provider.name"
AGENT_NAME = "gen_ai.agent.name"
CONVERSATION_ID = "gen_ai.conversation.id"
REQUEST_MODEL = "gen_ai.request.model"
REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
RESPONSE_MODEL = "gen_ai.response.model"
RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"
USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
"""All input tokens, cached or not. The cache counts below are subsets of it."""
USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
USAGE_CACHE_READ = "gen_ai.usage.cache_read.input_tokens"
USAGE_CACHE_CREATION = "gen_ai.usage.cache_creation.input_tokens"
TOOL_NAME = "gen_ai.tool.name"
TOOL_CALL_ID = "gen_ai.tool.call.id"
TOOL_TYPE = "gen_ai.tool.type"
ERROR_TYPE = "error.type"

OP_INVOKE_AGENT = "invoke_agent"
OP_CHAT = "chat"
OP_EXECUTE_TOOL = "execute_tool"

# --- kairos.* (ours; PLAN.md section 3.7) ---------------------------------------------
RUN_ID = "kairos.run_id"
THREAD_ID = "kairos.thread_id"
JOB_ID = "kairos.job_id"
HARNESS_VERSION = "kairos.harness_version"
ATTEMPT = "kairos.attempt"
STEP = "kairos.step"
STOP_REASON = "kairos.stop_reason"
COST_USD = "kairos.cost_usd"
STEPS = "kairos.steps"
TOOL_CALLS = "kairos.tool_calls"
TOOL_RISK = "kairos.tool.risk"
TOOL_PROVENANCE = "kairos.tool.provenance"
TOOL_IS_ERROR = "kairos.tool.is_error"

# --- read by Langfuse's OTLP ingestion ------------------------------------------------
LANGFUSE_SESSION_ID = "langfuse.session.id"
LANGFUSE_COST = "gen_ai.usage.cost"
INPUT_VALUE = "input.value"
OUTPUT_VALUE = "output.value"
