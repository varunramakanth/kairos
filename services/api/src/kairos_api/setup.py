"""What is configured, for the Welcome page's setup checklist.

Each item says whether a feature is on and, if not, the one thing that turns it on.
Only presence is reported, never a value: the page must not leak secrets.
"""

from collections.abc import Mapping

from kairos_api.settings import ApiSettings
from kairos_domain.base import DomainModel


class SetupItem(DomainModel):
    id: str
    label: str
    ok: bool
    detail: str
    """What is set up, or how to turn it on."""
    required: bool = False
    """The app does nothing useful without it."""


class SetupStatus(DomainModel):
    demo: bool
    """The model is a scripted demo cassette: answers are canned and free."""
    items: list[SetupItem]


def setup_status(settings: ApiSettings, environ: Mapping[str, str]) -> SetupStatus:
    demo = settings.provider == "replay"
    return SetupStatus(
        demo=demo,
        items=[
            _model(settings, environ),
            SetupItem(
                id="web_search",
                label="Web search",
                ok=settings.tavily_api_key is not None,
                detail="Tavily is configured."
                if settings.tavily_api_key
                else "Set TAVILY_API_KEY to let the agent search the web.",
            ),
            SetupItem(
                id="flights",
                label="Flight search",
                ok=settings.serpapi_api_key is not None,
                detail="SerpApi is configured."
                if settings.serpapi_api_key
                else "Set SERPAPI_API_KEY for flight prices and fare watches.",
            ),
            SetupItem(
                id="email",
                label="Email alerts",
                ok=settings.email_configured,
                detail=f"Alerts go to {settings.email_to}."
                if settings.email_configured
                else "Set KAIROS_EMAIL_PROVIDER and KAIROS_EMAIL_TO (docs/ses-setup.md).",
            ),
            SetupItem(
                id="push",
                label="Push notifications",
                ok=settings.push_configured,
                detail="Turn them on for this device in Settings."
                if settings.push_configured
                else "Run `uv run kairos vapid-keys` and add the pair to the environment.",
            ),
            SetupItem(
                id="browser",
                label="Browser",
                ok=settings.browser,
                detail="The agent can open pages that need JavaScript."
                if settings.browser
                else "Optional: set KAIROS_BROWSER=1 for pages that need JavaScript.",
            ),
            SetupItem(
                id="spend_cap",
                label="Daily spend cap",
                ok=settings.daily_spend_cap_usd is not None,
                detail=f"Runs stop for the day at ${settings.daily_spend_cap_usd:.2f}."
                if settings.daily_spend_cap_usd is not None
                else "No cap: set KAIROS_DAILY_SPEND_CAP_USD.",
            ),
            SetupItem(
                id="timezone",
                label="Timezone",
                ok=True,
                detail=f"Schedules use {settings.timezone} (KAIROS_TIMEZONE).",
            ),
        ],
    )


def _model(settings: ApiSettings, environ: Mapping[str, str]) -> SetupItem:
    if settings.provider == "replay":
        return SetupItem(
            id="model",
            label="Model",
            ok=True,
            required=True,
            detail="Demo mode: a scripted cassette answers, at no cost.",
        )
    if settings.provider == "bedrock":
        region = settings.bedrock_region or "default region"
        return SetupItem(
            id="model",
            label="Model",
            ok=True,
            required=True,
            detail=f"Amazon Bedrock ({region}), {settings.model}.",
        )
    has_key = bool(environ.get("ANTHROPIC_API_KEY"))
    return SetupItem(
        id="model",
        label="Model",
        ok=has_key,
        required=True,
        detail=f"Anthropic API, {settings.model}."
        if has_key
        else "Set ANTHROPIC_API_KEY (or run `just api-demo` to try it free).",
    )
