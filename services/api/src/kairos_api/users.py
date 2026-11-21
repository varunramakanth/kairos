"""Who is calling, and the directory behind invites (ADR-018).

``Directory`` is the seam to Amazon Cognito (sign-in accounts) and Amazon SES (email
verification). ``AwsDirectory`` calls them with ``boto3`` in a thread; tests use a
fake. ``resolve_user`` turns a verified access token into a ``User``, binding the
owner's first sign-in to the ``local`` admin.
"""

import asyncio
from typing import Any, Protocol, cast

import httpx2
import structlog

from kairos_db.users import UserStore
from kairos_domain.users import LOCAL_USER, User

log = structlog.get_logger(__name__)


class Directory(Protocol):
    """Cognito and SES operations the API needs."""

    async def userinfo_email(self, access_token: str) -> str | None:
        """The signed-in user's email, from Cognito's ``/oauth2/userInfo``."""
        ...

    async def invite(self, email: str) -> str:
        """Create a Cognito user (Cognito emails a temporary password). Returns the
        new user's ``sub``."""
        ...

    async def set_enabled(self, sub: str, *, enabled: bool) -> None: ...

    async def delete(self, sub: str) -> None: ...

    async def request_verification(self, email: str) -> None:
        """Ask SES to email a verification link to ``email`` (idempotent)."""
        ...

    async def is_verified(self, email: str) -> bool:
        """Whether SES may send to ``email`` yet."""
        ...

    async def forget_email(self, email: str) -> None:
        """Remove ``email``'s SES identity (when its user is deleted)."""
        ...


class DirectoryNotConfiguredError(RuntimeError):
    """Invites need Cognito (``KAIROS_AUTH_USER_POOL_ID``); sign-in is off locally."""


class AwsDirectory:
    """``Directory`` on Cognito and SES. Credentials come from the task role."""

    def __init__(self, *, region: str, user_pool_id: str | None, domain: str | None) -> None:
        self._region = region
        self._pool = user_pool_id
        self._domain = domain.rstrip("/") if domain else None
        self._cognito: Any = None
        self._ses: Any = None

    def _clients(self) -> tuple[Any, Any]:
        if self._cognito is None:
            import boto3

            # No cognito-idp stubs are installed; these clients are typed Any on purpose.
            self._cognito = cast("Any", boto3.client("cognito-idp", region_name=self._region))  # pyright: ignore[reportUnknownMemberType]
            self._ses = cast("Any", boto3.client("sesv2", region_name=self._region))  # pyright: ignore[reportUnknownMemberType]
        return self._cognito, self._ses

    def _pool_id(self) -> str:
        if self._pool is None:
            raise DirectoryNotConfiguredError("KAIROS_AUTH_USER_POOL_ID is not set")
        return self._pool

    async def userinfo_email(self, access_token: str) -> str | None:
        if self._domain is None:
            return None
        async with httpx2.AsyncClient(timeout=10) as http:
            response = await http.get(
                f"{self._domain}/oauth2/userInfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if response.status_code != 200:
            log.warning("userinfo_failed", status=response.status_code)
            return None
        email = response.json().get("email")
        return email if isinstance(email, str) else None

    async def invite(self, email: str) -> str:
        cognito, _ = self._clients()
        pool = self._pool_id()

        def create() -> str:
            result = cognito.admin_create_user(
                UserPoolId=pool,
                Username=email,
                UserAttributes=[
                    {"Name": "email", "Value": email},
                    {"Name": "email_verified", "Value": "true"},
                ],
                DesiredDeliveryMediums=["EMAIL"],
            )
            attributes = {a["Name"]: a["Value"] for a in result["User"]["Attributes"]}
            return str(attributes["sub"])

        return await asyncio.to_thread(create)

    async def set_enabled(self, sub: str, *, enabled: bool) -> None:
        cognito, _ = self._clients()
        pool = self._pool_id()
        call = cognito.admin_enable_user if enabled else cognito.admin_disable_user
        await asyncio.to_thread(call, UserPoolId=pool, Username=sub)

    async def delete(self, sub: str) -> None:
        cognito, _ = self._clients()
        await asyncio.to_thread(cognito.admin_delete_user, UserPoolId=self._pool_id(), Username=sub)

    async def request_verification(self, email: str) -> None:
        _, ses = self._clients()

        def create() -> None:
            try:
                ses.create_email_identity(EmailIdentity=email)
            except ses.exceptions.AlreadyExistsException:
                # Recreate it so SES sends a fresh verification email.
                ses.delete_email_identity(EmailIdentity=email)
                ses.create_email_identity(EmailIdentity=email)

        await asyncio.to_thread(create)

    async def is_verified(self, email: str) -> bool:
        _, ses = self._clients()

        def check() -> bool:
            try:
                identity = ses.get_email_identity(EmailIdentity=email)
            except ses.exceptions.NotFoundException:
                return False
            return bool(identity.get("VerifiedForSendingStatus"))

        return await asyncio.to_thread(check)

    async def forget_email(self, email: str) -> None:
        _, ses = self._clients()

        def remove() -> None:
            try:
                ses.delete_email_identity(EmailIdentity=email)
            except ses.exceptions.NotFoundException:
                return

        await asyncio.to_thread(remove)


class NotInvitedError(Exception):
    """A valid Cognito sign-in with no (enabled) Kairos user behind it."""


async def resolve_user(
    users: UserStore,
    directory: Directory,
    *,
    sub: str,
    access_token: str,
    owner_email: str | None,
) -> User:
    """The ``User`` for a verified token.

    An unknown ``sub`` is only accepted once: when the ``local`` admin is still
    unclaimed and Cognito says this person's email is the configured owner's. Then
    ``local`` is renamed to ``sub`` and the owner's existing data moves with it.
    """
    user = await users.get(sub)
    if user is None and owner_email and await users.unbound_owner() is not None:
        email = await directory.userinfo_email(access_token)
        if email is not None and email.lower() == owner_email.lower():
            user = await users.bind_owner(sub, email)
            log.info("owner_bound", previous=LOCAL_USER)
    if user is None or user.disabled:
        raise NotInvitedError(sub)
    return user
