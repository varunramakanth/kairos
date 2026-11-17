"""Delivery channels. Each sends one ``Notification`` or raises ``ChannelError``."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from email.message import EmailMessage
from typing import Any, Protocol, cast

import aiosmtplib
import structlog
from pydantic import SecretStr

from kairos_domain.governance import Notification, PushSubscription

log = structlog.get_logger(__name__)


class ChannelError(Exception):
    """Delivery failed. ``retryable`` is false when retrying cannot help."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class Channel(Protocol):
    @property
    def name(self) -> str: ...

    async def send(self, notification: Notification) -> None: ...


def _link(notification: Notification, base_url: str) -> str | None:
    return f"{base_url.rstrip('/')}{notification.url}" if notification.url else None


class ConsoleChannel:
    """Writes to the log. Always available; what tests and local runs see."""

    name = "console"

    async def send(self, notification: Notification) -> None:
        log.info(
            "notification",
            kind=notification.kind.value,
            title=notification.title,
            body=notification.body[:500],
            url=notification.url,
        )


type SmtpSend = Callable[[EmailMessage], Awaitable[None]]


class SmtpChannel:
    """Email to the owner's own address over SMTP with STARTTLS (Gmail by default).

    ``recipient`` is fixed at construction: this channel cannot email anyone else.
    """

    name = "email"

    def __init__(
        self,
        *,
        recipient: str,
        username: str,
        password: SecretStr,
        host: str = "smtp.gmail.com",
        port: int = 587,
        base_url: str = "http://localhost:3000",
        send: SmtpSend | None = None,
    ) -> None:
        self._recipient = recipient
        self._username = username
        self._password = password
        self._host = host
        self._port = port
        self._base_url = base_url
        self._send = send or self._smtp_send

    async def send(self, notification: Notification) -> None:
        message = EmailMessage()
        message["From"] = f"Kairos <{self._username}>"
        message["To"] = self._recipient
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


class PushGone(Exception):
    """The push service says this subscription no longer exists (404/410)."""


type PushSend = Callable[[PushSubscription, str], Awaitable[None]]


class WebPushChannel:
    """Web Push (RFC 8030) with VAPID, to every subscribed browser. Self-hosted:
    the browsers' own push services carry the message; no third-party account."""

    name = "push"

    def __init__(
        self,
        *,
        subscriptions: Callable[[], Awaitable[Sequence[PushSubscription]]],
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

    async def send(self, notification: Notification) -> None:
        targets = await self._subscriptions()
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
