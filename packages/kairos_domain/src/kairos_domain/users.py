"""Users of one Kairos deployment (ADR-018).

The owner is the ``admin``; people they invite are ``member``s. Every root row in
the database (threads, runs, jobs, memories, notifications, the audit log) carries
the ``id`` of the user it belongs to.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from kairos_domain.base import DomainModel

LOCAL_USER = "local"
"""The user id when sign-in is off (local development, the demo), and the owner's id
in production until their first sign-in binds it to their Cognito ``sub``."""

DEFAULT_MEMBER_CAP_USD = 2.0


class Role(StrEnum):
    ADMIN = "admin"
    MEMBER = "member"


class User(DomainModel):
    id: str
    """The Cognito ``sub``, or ``local``."""
    email: str | None
    role: Role
    daily_cap_usd: float | None
    """This user's own daily spend cap; ``None`` means only the overall cap applies."""
    email_verified: bool
    """SES may send to ``email`` (in the SES sandbox, only verified addresses work)."""
    disabled: bool
    created_at: datetime


class Invite(DomainModel):
    email: str = Field(max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    daily_cap_usd: float | None = Field(default=DEFAULT_MEMBER_CAP_USD, ge=0, le=1000)


class UserPatch(DomainModel):
    """Change a user's cap, or disable or re-enable them."""

    daily_cap_usd: float | None = Field(default=None, ge=0, le=1000)
    clear_cap: bool = False
    """Remove the user's own cap (``daily_cap_usd`` cannot say "none" on its own)."""
    disabled: bool | None = None
