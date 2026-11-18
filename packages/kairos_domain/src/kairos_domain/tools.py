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
    suspected_injection: bool = False
    """Set by the loop after screening untrusted output (never by tools themselves)."""


class ScreenVerdict(DomainModel):
    """The result of screening untrusted content for prompt injection."""

    content: str
    """The content to show the model: hidden characters removed, otherwise unchanged."""
    suspected: bool = False
    reasons: tuple[str, ...] = ()


class ToolContext(DomainModel):
    """Per-call context handed to a tool implementation."""

    run_id: str
    tool_call_id: str
    idempotency_key: str
    """Stable across retries of this call. Pass it to external APIs that support one."""


class Allow(DomainModel):
    """Run the tool call."""


class Deny(DomainModel):
    """Do not run it; the model receives ``reason`` as an error result."""

    reason: str


class RequireApproval(DomainModel):
    """Pause the run until a human decides."""

    reason: str


type ToolDecision = Allow | Deny | RequireApproval
"""What a ``before_tool`` hook (policy, guardrails) decides about one call."""
