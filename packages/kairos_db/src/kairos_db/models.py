"""Table definitions.

``runs.state`` is the checkpoint (a serialized ``RunState``) and the only thing
needed to resume. ``run_steps`` and ``tool_calls`` are projections of ``run_events``
kept for querying; they can be rebuilt from the event log.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, datetime: DateTime(timezone=True)}  # noqa: RUF012


def _created() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_due", "status", "lease_until"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    thread_id: Mapped[str | None] = mapped_column(Text, index=True)
    status: Mapped[str] = mapped_column(Text)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    request: Mapped[dict[str, Any]]
    state: Mapped[dict[str, Any] | None]
    # Lease (ADR-003). ``lease_id`` is a fencing token: every write by the worker
    # that holds the run is conditioned on it.
    lease_id: Mapped[str | None] = mapped_column(Text)
    lease_until: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()


class RunEventRow(Base):
    """Append-only event log. ``seq`` is monotonic per run and is the SSE event id."""

    __tablename__ = "run_events"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = _created()


class RunStep(Base):
    __tablename__ = "run_steps"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    step: Mapped[int] = mapped_column(Integer, primary_key=True)
    model: Mapped[str] = mapped_column(Text)
    stop_reason: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cache_read_input_tokens: Mapped[int] = mapped_column(Integer)
    cache_creation_input_tokens: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = _created()


class ToolCall(Base):
    __tablename__ = "tool_calls"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    step: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(Text)
    input: Mapped[dict[str, Any]]
    status: Mapped[str] = mapped_column(Text)
    output: Mapped[str | None] = mapped_column(Text)
    is_error: Mapped[bool | None]
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None]


class ToolResultBlob(Base):
    """Full tool output behind a truncated result (the "read more" handle)."""

    __tablename__ = "tool_result_blobs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
