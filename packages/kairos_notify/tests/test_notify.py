from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import boto3
import pytest
from botocore.stub import Stubber
from pydantic import SecretStr

from kairos_domain.governance import (
    ChannelName,
    DigestData,
    DigestJob,
    Notification,
    NotificationKind,
    NotificationStatus,
    PushKeys,
    PushSubscription,
    Recipient,
    StoredNotification,
)
from kairos_domain.tools import ToolContext
from kairos_harness import FakeClock
from kairos_notify import (
    ChannelError,
    ConsoleChannel,
    Dispatcher,
    PushGone,
    SesChannel,
    SmtpChannel,
    WebPushChannel,
    build_digest,
)
from kairos_notify.tools import notify_tool

NOW = datetime(2026, 11, 22, 23, 0, tzinfo=UTC)  # Sunday 18:00 in Toronto
OWNER_EMAIL = "owner@example.com"
OWNER = Recipient(user_id="local", email=OWNER_EMAIL)
ALERT = Notification(
    kind=NotificationKind.ALERT, title="Fare under $1500", body="1420 CAD", url="/tasks/job_1"
)


class MemoryOutbox:
    def __init__(self, *items: Notification) -> None:
        self.rows = {
            i: StoredNotification(
                id=i,
                notification=n,
                status=NotificationStatus.PENDING,
                attempts=0,
                results={},
                created_at=NOW,
                sent_at=None,
            )
            for i, n in enumerate(items, start=1)
        }
        self.retry_at: dict[int, datetime | None] = {}
        self.recipients: dict[str, Recipient] = {}

    async def claim(
        self, *, now: datetime, limit: int, ttl_seconds: float
    ) -> Sequence[StoredNotification]:
        due = [
            r
            for i, r in self.rows.items()
            if r.status is NotificationStatus.PENDING and (self.retry_at.get(i) or now) <= now
        ]
        return due[:limit]

    async def finish(
        self,
        notification_id: int,
        *,
        results: Mapping[str, str],
        sent: bool,
        retry_at: datetime | None,
        now: datetime,
    ) -> None:
        row = self.rows[notification_id]
        status = (
            NotificationStatus.PENDING
            if retry_at
            else (NotificationStatus.SENT if sent else NotificationStatus.FAILED)
        )
        self.rows[notification_id] = row.model_copy(
            update={"status": status, "attempts": row.attempts + 1, "results": dict(results)}
        )
        self.retry_at[notification_id] = retry_at

    async def recipient(self, user_id: str) -> Recipient:
        return self.recipients.get(user_id, Recipient(user_id=user_id, email=OWNER_EMAIL))


class Flaky:
    name = "email"

    def __init__(self, failures: int, *, retryable: bool = True) -> None:
        self.failures = failures
        self.retryable = retryable
        self.sent: list[Notification] = []

    async def send(self, notification: Notification, recipient: Recipient) -> None:
        if self.failures:
            self.failures -= 1
            raise ChannelError("smtp down", retryable=self.retryable)
        self.sent.append(notification)


async def test_dispatch_sends_on_requested_channels_and_skips_unconfigured_ones() -> None:
    outbox = MemoryOutbox(ALERT)
    email = Flaky(0)
    await Dispatcher(outbox, [ConsoleChannel(), email], clock=FakeClock(NOW)).tick()
    row = outbox.rows[1]
    assert row.status is NotificationStatus.SENT
    assert row.results == {"console": "sent", "email": "sent", "push": "skipped (not configured)"}
    assert email.sent == [ALERT]


async def test_a_failing_channel_is_retried_without_resending_the_others() -> None:
    outbox, clock = MemoryOutbox(ALERT), FakeClock(NOW)
    email = Flaky(1)
    console_calls: list[Notification] = []

    class CountingConsole(ConsoleChannel):
        async def send(self, notification: Notification, recipient: Recipient) -> None:
            console_calls.append(notification)

    dispatcher = Dispatcher(outbox, [CountingConsole(), email], clock=clock)
    await dispatcher.tick()
    assert outbox.rows[1].status is NotificationStatus.PENDING
    assert outbox.rows[1].results["email"] == "error: smtp down"
    assert outbox.retry_at[1] == NOW + timedelta(minutes=1)

    clock.advance(60)
    await dispatcher.tick()
    assert outbox.rows[1].status is NotificationStatus.SENT
    assert len(console_calls) == 1  # not sent twice
    assert len(email.sent) == 1


async def test_a_permanent_failure_is_not_retried() -> None:
    outbox = MemoryOutbox(ALERT.model_copy(update={"channels": ["email"]}))
    await Dispatcher(outbox, [Flaky(5, retryable=False)], clock=FakeClock(NOW)).tick()
    assert outbox.rows[1].status is NotificationStatus.FAILED
    assert outbox.retry_at[1] is None


async def test_email_goes_only_to_the_recipient_with_a_link_back() -> None:
    sent: list[EmailMessage] = []

    async def capture(message: EmailMessage) -> None:
        sent.append(message)

    channel = SmtpChannel(
        username="owner@example.com",
        password=SecretStr("app-password"),
        base_url="http://localhost:3000",
        send=capture,
    )
    await channel.send(ALERT, OWNER)
    (message,) = sent
    assert message["To"] == "owner@example.com"
    assert message["Subject"] == "[Kairos] Fare under $1500"
    assert "Open in Kairos: http://localhost:3000/tasks/job_1" in message.get_content()
    assert "app-password" not in message.as_string()


async def test_push_reaches_every_browser_and_forgets_expired_ones() -> None:
    def sub(name: str) -> PushSubscription:
        return PushSubscription(
            endpoint=f"https://push.example/{name}", keys=PushKeys(p256dh="k", auth="a")
        )

    forgotten: list[str] = []
    delivered: list[str] = []

    asked: list[str] = []

    async def subscriptions(user_id: str) -> list[PushSubscription]:
        asked.append(user_id)
        return [sub("live"), sub("gone")]

    async def forget(endpoint: str) -> None:
        forgotten.append(endpoint)

    async def send(subscription: PushSubscription, payload: str) -> None:
        if subscription.endpoint.endswith("gone"):
            raise PushGone
        delivered.append(payload)

    channel = WebPushChannel(
        subscriptions=subscriptions,
        forget=forget,
        vapid_private_key=SecretStr("k"),
        subject="mailto:o@example.com",
        send=send,
    )
    await channel.send(ALERT, OWNER)
    assert asked == [OWNER.user_id]  # only this user's browsers
    assert forgotten == ["https://push.example/gone"]
    assert '"url": "/tasks/job_1"' in delivered[0]


def test_digest_summarizes_the_week() -> None:
    data = DigestData(
        since=NOW - timedelta(days=7),
        until=NOW,
        alerts=["Fare under $1500: 1420 CAD"],
        jobs=[
            DigestJob(
                name="YYZ-CJB fare",
                kind="watch",
                runs=28,
                failures=2,
                status="active",
                latest="1420 CAD",
            )
        ],
        pending_approvals=1,
        runs=31,
        cost_usd=0.4321,
    )
    digest = build_digest(data)
    assert digest.kind is NotificationKind.DIGEST
    assert digest.title == "Weekly digest: 1 alerts, 1 waiting for you"
    assert "Sun 15 Nov to Sun 22 Nov" in digest.body
    assert "- YYZ-CJB fare (watch, active): 28 runs, 2 failed; latest: 1420 CAD" in digest.body
    assert "31 agent runs this week, $0.43 in model spend." in digest.body
    quiet = build_digest(data.model_copy(update={"alerts": [], "jobs": [], "pending_approvals": 0}))
    assert quiet.title == "Weekly digest: a quiet week"


@pytest.mark.parametrize(
    ("channels", "start"),
    [
        (["console", "email", "push"], "Queued for delivery to the user by email and to browsers"),
        (
            ["console", "email"],
            "Queued for delivery to the user by email (browser push is not set up)",
        ),
        (["console", "push"], "Queued for browser push to the user (email is not set up)"),
        (["console"], "Recorded, but NOT delivered"),
    ],
)
async def test_notify_me_says_exactly_where_the_message_went(
    channels: list[ChannelName], start: str
) -> None:
    queued: list[tuple[Notification, str, str]] = []

    async def enqueue(notification: Notification, key: str, user_id: str) -> None:
        queued.append((notification, key, user_id))

    tool = notify_tool(enqueue, channels)
    ctx = ToolContext(run_id="r", tool_call_id="t1", idempotency_key="t1", user_id="u_bea")
    output = await tool({"title": "Hi", "body": "Hello"}, ctx)
    assert output.content.startswith(start)
    ((notification, key, user_id),) = queued
    assert notification.channels == channels
    assert key == "notify:t1"
    assert user_id == "u_bea"  # the run's own user, never one the model picks


def ses_channel() -> tuple[SesChannel, Stubber]:
    client = boto3.client(  # pyright: ignore[reportUnknownMemberType]  # only sesv2 stubs installed
        "sesv2",
        region_name="ca-central-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",  # noqa: S106 (fake key; the stubber never sends)
    )
    channel = SesChannel(sender="owner@example.com", region="ca-central-1")
    channel._client = client  # pyright: ignore[reportPrivateUsage]  # inject the stubbed client
    return channel, Stubber(client)


async def test_ses_sends_a_plain_text_email_to_the_recipient_only() -> None:
    channel, stubber = ses_channel()
    stubber.add_response(
        "send_email",
        {"MessageId": "m-1"},
        {
            "FromEmailAddress": "Kairos <owner@example.com>",
            "Destination": {"ToAddresses": ["owner@example.com"]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": "[Kairos] Fare under $1500", "Charset": "UTF-8"},
                    "Body": {
                        "Text": {
                            "Data": "1420 CAD\n\nOpen in Kairos: http://localhost:3000/tasks/job_1",
                            "Charset": "UTF-8",
                        }
                    },
                }
            },
        },
    )
    with stubber:
        await channel.send(ALERT, OWNER)
    stubber.assert_no_pending_responses()


@pytest.mark.parametrize(
    ("code", "retryable"),
    [
        ("MessageRejected", False),
        ("AccessDeniedException", False),
        ("TooManyRequestsException", True),
    ],
)
async def test_ses_errors_say_whether_a_retry_can_help(code: str, retryable: bool) -> None:
    channel, stubber = ses_channel()
    stubber.add_client_error("send_email", service_error_code=code, http_status_code=400)
    with stubber, pytest.raises(ChannelError) as raised:
        await channel.send(ALERT, OWNER)
    assert str(raised.value) == f"SES {code}"
    assert raised.value.retryable is retryable


async def test_email_needs_a_verified_address() -> None:
    channel = SesChannel(sender="owner@example.com", region="ca-central-1")
    with pytest.raises(ChannelError) as raised:
        await channel.send(ALERT, Recipient(user_id="u_bea", email=None))
    assert raised.value.retryable is False


async def test_nothing_is_sent_to_a_disabled_user() -> None:
    outbox = MemoryOutbox(ALERT)
    outbox.recipients["local"] = Recipient(user_id="local", email=None, active=False)
    email = Flaky(0)
    await Dispatcher(outbox, [ConsoleChannel(), email], clock=FakeClock(NOW)).tick()
    assert email.sent == []
    assert outbox.rows[1].results["email"] == "skipped (user disabled)"
