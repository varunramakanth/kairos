"""Delivery channels. Each sends one ``Notification`` or raises ``ChannelError``."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any, Protocol, cast

import aiosmtplib
import structlog
from pydantic import SecretStr

from kairos_domain.governance import Notification, PushSubscription, Recipient

if TYPE_CHECKING:
    from types_boto3_sesv2 import SESV2Client

log = structlog.get_logger(__name__)


class ChannelError(Exception):
    """Delivery failed. ``retryable`` is false when retrying cannot help."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class Channel(Protocol):
    @property
    def name(self) -> str: ...

    async def send(self, notification: Notification, recipient: Recipient) -> None:
        """Deliver to ``recipient`` only (ADR-018). Raise ``ChannelError`` on failure."""
        ...


def _address(recipient: Recipient) -> str:
    if recipient.email is None:
        raise ChannelError("no verified email address for this user", retryable=False)
    return recipient.email


def _link(notification: Notification, base_url: str) -> str | None:
    return f"{base_url.rstrip('/')}{notification.url}" if notification.url else None


class ConsoleChannel:
    """Writes to the log. Always available; what tests and local runs see."""

    name = "console"

    async def send(self, notification: Notification, recipient: Recipient) -> None:
        log.info(
            "notification",
            user_id=recipient.user_id,
            kind=notification.kind.value,
            title=notification.title,
            body=notification.body[:500],
            url=notification.url,
        )


type SmtpSend = Callable[[EmailMessage], Awaitable[None]]


class SmtpChannel:
    """Email over SMTP with STARTTLS (Gmail by default), to the notification's own
    user and nobody else."""

    name = "email"

    def __init__(
        self,
        *,
        username: str,
        password: SecretStr,
        host: str = "smtp.gmail.com",
        port: int = 587,
        base_url: str = "http://localhost:3000",
        send: SmtpSend | None = None,
    ) -> None:
        self._username = username
        self._password = password
        self._host = host
        self._port = port
        self._base_url = base_url
        self._send = send or self._smtp_send

    async def send(self, notification: Notification, recipient: Recipient) -> None:
        message = EmailMessage()
        message["From"] = f"Kairos <{self._username}>"
        message["To"] = _address(recipient)
        message["Subject"] = f"[Kairos] {notification.title}"
        link = _link(notification, self._base_url)
        message.set_content(notification.body + (f"\n\nOpen in Kairos: {link}" if link else ""))
        await self._send(message)

    async def _smtp_send(self, message: EmailMessage) -> None:
        try:
            await aiosmtplib.send(
                message,
                hostname=self._host,
                port=self._port,
                start_tls=True,
                username=self._username,
                password=self._password.get_secret_value(),
                timeout=30,
            )
        except aiosmtplib.SMTPAuthenticationError as error:
            raise ChannelError(f"SMTP login failed ({error.code})", retryable=False) from error
        except aiosmtplib.SMTPException as error:
            raise ChannelError(f"SMTP error: {type(error).__name__}") from error


type SesSend = Callable[[dict[str, Any]], Awaitable[None]]

# SES errors where retrying cannot help: fix the setup instead.
_SES_PERMANENT = {
    "MessageRejected",
    "MailFromDomainNotVerifiedException",
    "AccountSuspendedException",
    "BadRequestException",
    "NotFoundException",
    "AccessDeniedException",
    "AccessDenied",
    "UnrecognizedClientException",
    "InvalidClientTokenId",
}


class SesChannel:
    """Email through Amazon SES (API v2), send-only, to the notification's own user.

    Credentials come from the standard AWS chain (``AWS_PROFILE``, SSO, an instance
    role). Give them only ``ses:SendEmail`` on the sender identity, ideally with
    ``ses:FromAddress`` and ``ses:Recipients`` conditions: then the key can send to
    the owner and do nothing else (see ADR-010).
    """

    name = "email"

    def __init__(
        self,
        *,
        sender: str,
        region: str,
        profile: str | None = None,
        base_url: str = "http://localhost:3000",
        send: SesSend | None = None,
    ) -> None:
        self._sender = sender
        self._region = region
        self._profile = profile
        self._base_url = base_url
        self._send = send or self._boto_send
        self._client: SESV2Client | None = None

    def request(self, notification: Notification, to: str) -> dict[str, Any]:
        link = _link(notification, self._base_url)
        body = notification.body + (f"\n\nOpen in Kairos: {link}" if link else "")
        return {
            "FromEmailAddress": f"Kairos <{self._sender}>",
            "Destination": {"ToAddresses": [to]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": f"[Kairos] {notification.title}", "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                }
            },
        }

    async def send(self, notification: Notification, recipient: Recipient) -> None:
        await self._send(self.request(notification, _address(recipient)))

    async def _boto_send(self, request: dict[str, Any]) -> None:
        # boto3 is synchronous; keep it off the event loop.
        await asyncio.to_thread(self._send_blocking, request)

    def _send_blocking(self, request: dict[str, Any]) -> None:
        import boto3
        from botocore.exceptions import (
            BotoCoreError,
            ClientError,
            NoCredentialsError,
            ProfileNotFound,
        )

        try:
            if self._client is None:
                # Only the sesv2 stubs are installed; other services overloads are unknown.
                session = boto3.Session(profile_name=self._profile, region_name=self._region)
                self._client = session.client("sesv2")  # pyright: ignore[reportUnknownMemberType]
            self._client.send_email(**request)
        except ProfileNotFound as error:
            raise ChannelError(
                f"AWS profile {self._profile!r} not found", retryable=False
            ) from error
        except NoCredentialsError as error:
            raise ChannelError(
                "no AWS credentials found (set AWS_PROFILE or run `aws sso login`)",
                retryable=False,
            ) from error
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", "Unknown"))
            raise ChannelError(f"SES {code}", retryable=code not in _SES_PERMANENT) from error
        except BotoCoreError as error:
            raise ChannelError(f"SES {type(error).__name__}") from error


class PushGone(Exception):
    """The push service says this subscription no longer exists (404/410)."""


type PushSend = Callable[[PushSubscription, str], Awaitable[None]]


class WebPushChannel:
    """Web Push (RFC 8030) with VAPID, to every browser the notification's user
    subscribed. Self-hosted:
    the browsers' own push services carry the message; no third-party account."""

    name = "push"

    def __init__(
        self,
        *,
        subscriptions: Callable[[str], Awaitable[Sequence[PushSubscription]]],
        forget: Callable[[str], Awaitable[None]],
        vapid_private_key: SecretStr,
        subject: str,
        send: PushSend | None = None,
    ) -> None:
        self._subscriptions = subscriptions
        self._forget = forget
        self._key = vapid_private_key
        self._subject = subject
        self._send = send or self._webpush

    async def send(self, notification: Notification, recipient: Recipient) -> None:
        targets = await self._subscriptions(recipient.user_id)
        if not targets:
            raise ChannelError("no browser has enabled push", retryable=False)
        payload = json.dumps(
            {"title": notification.title, "body": notification.body[:300], "url": notification.url}
        )
        delivered = 0
        errors: list[str] = []
        for subscription in targets:
            try:
                await self._send(subscription, payload)
                delivered += 1
            except PushGone:
                await self._forget(subscription.endpoint)
            except Exception as error:
                errors.append(f"{type(error).__name__}")
        if delivered == 0:
            raise ChannelError("push failed: " + (", ".join(errors) or "no live subscriptions"))

    async def _webpush(self, subscription: PushSubscription, payload: str) -> None:
        await asyncio.to_thread(_webpush_blocking, subscription, payload, self._key, self._subject)


def _webpush_blocking(
    subscription: PushSubscription, payload: str, key: SecretStr, subject: str
) -> None:
    # pywebpush is synchronous and untyped; it runs in a worker thread.
    import pywebpush  # pyright: ignore[reportMissingTypeStubs]

    # pywebpush ships partial annotations; narrow them to what we call.
    webpush = cast("Callable[..., object]", pywebpush.webpush)  # pyright: ignore[reportUnknownMemberType]
    WebPushException: type[Exception] = pywebpush.WebPushException

    try:
        webpush(
            subscription_info=subscription.model_dump(),
            data=payload,
            vapid_private_key=key.get_secret_value(),
            vapid_claims={"sub": subject},
            ttl=24 * 3600,
            timeout=15,
        )
    except WebPushException as error:
        response: Any = getattr(error, "response", None)
        if response is not None and getattr(response, "status_code", None) in (404, 410):
            raise PushGone from error
        raise
