"""The setup checklist reports what is configured, and never a secret's value."""

from kairos_api.settings import ApiSettings
from kairos_api.setup import setup_status


def settings(**values: object) -> ApiSettings:
    # Pin features off so a developer's .env cannot leak into the test.
    base: dict[str, object] = {
        "provider": "anthropic",
        "tavily_api_key": None,
        "serpapi_api_key": None,
        "email_provider": None,
        "email_to": None,
        "vapid_public_key": None,
        "vapid_private_key": None,
        "browser": False,
    }
    return ApiSettings(_env_file=None, **(base | values))  # type: ignore[call-arg]  # pydantic-settings init kwargs


def items(status_settings: ApiSettings, environ: dict[str, str] | None = None) -> dict[str, bool]:
    status = setup_status(status_settings, environ or {})
    return {item.id: item.ok for item in status.items}


def test_a_bare_install_needs_only_the_model_key() -> None:
    status = setup_status(settings(), {})
    assert not status.demo
    model = next(item for item in status.items if item.id == "model")
    assert model.required
    assert not model.ok
    assert "ANTHROPIC_API_KEY" in model.detail
    assert items(settings())["timezone"]


def test_configured_features_are_reported_as_on() -> None:
    configured = settings(
        tavily_api_key="tvly-secret-value",
        serpapi_api_key="serp-secret-value",
        email_provider="ses",
        email_to="owner@example.com",
        browser=True,
    )
    found = items(configured, {"ANTHROPIC_API_KEY": "sk-ant-secret-value"})
    assert found["model"]
    assert found["web_search"]
    assert found["flights"]
    assert found["email"]
    assert found["browser"]
    assert not found["push"]


def test_no_secret_value_is_ever_returned() -> None:
    configured = settings(
        tavily_api_key="tvly-secret-value",
        serpapi_api_key="serp-secret-value",
        vapid_public_key="public",
        vapid_private_key="vapid-secret-value",
    )
    text = setup_status(configured, {"ANTHROPIC_API_KEY": "sk-ant-secret-value"}).model_dump_json()
    for secret in ("tvly-secret-value", "serp-secret-value", "vapid-secret-value", "sk-ant"):
        assert secret not in text


def test_demo_mode_needs_no_key() -> None:
    status = setup_status(settings(provider="replay", replay_cassette="demo.jsonl"), {})
    assert status.demo
    assert items(settings(provider="replay", replay_cassette="demo.jsonl"))["model"]
