"""Regression tests for xAI provider label disambiguation."""

from hermes_cli.providers import get_label


def test_xai_oauth_provider_label_is_not_collapsed_to_api_key_label():
    """The model picker must distinguish xAI API-key and OAuth providers."""
    assert get_label("xai-oauth") != get_label("xai")
    assert get_label("grok-oauth") == get_label("xai-oauth")


def test_xai_label_reads_registry_without_waiting_for_discovery(monkeypatch):
    from types import SimpleNamespace

    calls = []

    def cached_info(provider, *, allow_network=True):
        calls.append((provider, allow_network))
        return SimpleNamespace(name="xAI", env=[], api="https://api.x.ai/v1", doc="")

    monkeypatch.setattr("agent.models_dev.get_provider_info", cached_info)
    assert get_label("xai") == "xAI"
    assert calls == [("xai", False)]

