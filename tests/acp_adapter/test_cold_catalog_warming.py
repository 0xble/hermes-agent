"""Cold ACP picker catalogs must warm every endpoint without read-path network."""
import threading

import pytest

from acp_adapter import model_catalog as catalog
from hermes_cli import model_switch_providers as providers


@pytest.fixture(autouse=True)
def _offline_model_inventory():
    """Use the real inventory, not ACP conftest's stub, in these integration regressions."""


@pytest.fixture
def cold_picker(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(providers, "_picker_prewarm_done", threading.Event())
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda *a, **k: {})


def test_acp_inventory_requests_cache_only_registry(cold_picker, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    calls = []

    def inventory(*args, **kwargs):
        calls.append(kwargs)
        return {"providers": []}

    monkeypatch.setattr("hermes_cli.inventory.build_models_payload", inventory)
    monkeypatch.setattr(providers, "prewarm_picker_cache_async", lambda **kwargs: None)
    catalog.build_model_state("current-model", "openai", "")
    assert calls and calls[0]["registry_cache_only"] is True
    assert calls[0]["non_blocking_catalogs"] is True


@pytest.mark.parametrize("transport", [None, "openai_chat"])
def test_cold_canonical_collision_warms_named_proxy(cold_picker, monkeypatch, transport):
    cfg = {"model": {"provider": "openrouter", "default": "canonical-model"}, "providers": {
        "openrouter": {"base_url": "https://proxy.example/v1", "api_key": "proxy-key"}}}
    if transport:
        cfg["providers"]["openrouter"]["transport"] = transport
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    warmed = set()
    canonical_seen = []

    def authenticated(**kwargs):
        # Reproduce the real listing's seen_slugs collision rather than mocking discovery:
        # a canonical row takes openrouter before _lap_user_provider_rows gets to the proxy.
        from hermes_cli.model_switch_providers import _lap_user_provider_rows
        from types import SimpleNamespace
        b = SimpleNamespace(seen_slugs={"openrouter"})
        _lap_user_provider_rows(b, cfg["providers"])
        canonical_seen.append(True)
        return [{"slug": "openrouter", "models": ["canonical-model"]}]

    def discover(key, url, *args, probe_live=True, **kwargs):
        if probe_live:
            assert threading.current_thread() is not threading.main_thread()
            warmed.add((url, kwargs.get("api_mode")))
        return (["proxy-model"] if (url, kwargs.get("api_mode")) in warmed else None), False

    monkeypatch.setattr("hermes_cli.model_switch.list_authenticated_providers", authenticated)
    monkeypatch.setattr(providers, "_discover_endpoint_models", discover)
    monkeypatch.setattr("hermes_cli.inventory.build_models_payload", lambda *a, **k: {
        "providers": [{"slug": "openrouter", "models": ["canonical-model"]}]})
    state = catalog.build_model_state("canonical-model", "openrouter", "https://openrouter.ai/api/v1")
    # Join all picker-owned warms so the next snapshot is deterministically after warmup.
    for thread in threading.enumerate():
        if thread.name.startswith("picker-cache-prewarm"):
            thread.join(5)
    assert canonical_seen
    state = catalog.build_model_state("canonical-model", "openrouter", "https://openrouter.ai/api/v1")
    assert state is not None
    assert "custom:openrouter:proxy-model" in [m.model_id for m in state.available_models]
    assert state.current_model_id == "openrouter:canonical-model"
