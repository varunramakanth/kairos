import pytest

from kairos_worker.settings import Settings


def test_a_blank_tavily_key_means_web_search_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "")
    assert Settings(_env_file=None).tavily_api_key is None  # type: ignore[call-arg]


def test_a_tavily_key_is_read_without_the_kairos_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    key = Settings(_env_file=None).tavily_api_key  # type: ignore[call-arg]
    assert key is not None
    assert key.get_secret_value() == "tvly-test"
