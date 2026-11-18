"""Base class shared by all domain models."""

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Immutable, strict base for values that cross a boundary (API, events, tools, DB).

    Frozen because history is append-only: a message or event is never edited after
    it is created. Unknown fields are rejected so schema drift fails loudly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
