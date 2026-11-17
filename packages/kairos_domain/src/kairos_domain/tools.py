"""Tool descriptions shared by the harness, providers and policy."""

from enum import StrEnum

from pydantic import JsonValue

from kairos_domain.base import DomainModel
from kairos_domain.content import Provenance


class ToolRisk(StrEnum):
    """How much damage a tool call can do. The policy engine keys approval rules on this."""

    READ = "read"
    WRITE_SELF = "write_self"
    WRITE_EXTERNAL = "write_external"
    FINANCIAL = "financial"


class ToolSpec(DomainModel):
    """Everything the harness and the model need to know about a tool."""

    name: str
    description: str
    input_schema: dict[str, JsonValue]
    risk: ToolRisk = ToolRisk.READ
    idempotent: bool = True
    """Safe to call again with the same input after a crash. If false, an
    interrupted call is never retried automatically."""
    provenance: Provenance = Provenance.TRUSTED
    """Trust level of what the tool returns."""


class ToolOutput(DomainModel):
    """What a tool executor returns, before truncation and wrapping."""

    content: str
    is_error: bool = False


class ToolContext(DomainModel):
    """Per-call context handed to a tool implementation."""

    run_id: str
    tool_call_id: str
    idempotency_key: str
    """Stable across retries of this call. Pass it to external APIs that support one."""
