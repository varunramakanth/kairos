"""The weekly digest: built from data, deterministically. No model call."""

from zoneinfo import ZoneInfo

from kairos_domain.governance import DigestData, Notification, NotificationKind


def build_digest(data: DigestData, *, timezone: str = "America/Toronto") -> Notification:
    tz = ZoneInfo(timezone)
    since = data.since.astimezone(tz).strftime("%a %d %b")
    until = data.until.astimezone(tz).strftime("%a %d %b")
    lines = [f"Your week in Kairos, {since} to {until}.", ""]

    lines.append(f"Alerts ({len(data.alerts)})")
    lines += [f"- {alert}" for alert in data.alerts[:20]] or ["- none"]
    if len(data.alerts) > 20:
        lines.append(f"- and {len(data.alerts) - 20} more")
    lines.append("")

    lines.append("Tasks and watches")
    if not data.jobs:
        lines.append("- nothing ran")
    for job in data.jobs:
        failed = f", {job.failures} failed" if job.failures else ""
        latest = f"; latest: {job.latest}" if job.latest else ""
        lines.append(f"- {job.name} ({job.kind}, {job.status}): {job.runs} runs{failed}{latest}")
    lines.append("")

    if data.pending_approvals:
        lines.append(f"{data.pending_approvals} action(s) are waiting for your approval.")
        lines.append("")
    lines.append(f"{data.runs} agent runs this week, ${data.cost_usd:.2f} in model spend.")

    attention = len(data.alerts) + data.pending_approvals + sum(1 for j in data.jobs if j.failures)
    title = f"Weekly digest: {len(data.alerts)} alerts" + (
        f", {data.pending_approvals} waiting for you" if data.pending_approvals else ""
    )
    return Notification(
        kind=NotificationKind.DIGEST,
        title=title if attention else "Weekly digest: a quiet week",
        body="\n".join(lines),
        url="/tasks",
        channels=["console", "email"],
    )
