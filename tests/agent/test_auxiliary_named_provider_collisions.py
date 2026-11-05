"""Explicit custom:<name> identifiers never become built-in providers or aliases."""
from types import SimpleNamespace

import pytest

from agent import auxiliary_client as aux

NAMES = ["nous", "openrouter", "codex", "openai-codex", "copilot", "anthropic", "azure-foundry",
         "zai", "minimax", "huggingface", "main", "auto", "moa", "kimi", "llamacpp"]
NAMED_URL = "https://named-proxy.invalid/v1"
BUILTIN_URL = "https://builtin.invalid/v1"


@pytest.mark.parametrize("name", NAMES)
def test_central_router_preserves_named_ownership_through_defaults_overrides_and_cache(name, tmp_path, monkeypatch):
    import hermes_yaml
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(hermes_yaml.safe_dump({
        "providers": {name: {"base_url": NAMED_URL, "api_key": "named-key", "default_model": "entry-model"}},
    }))
    monkeypatch.setattr(aux, "OpenAI", lambda **kw: SimpleNamespace(**kw))
    monkeypatch.setattr(aux, "_try_nous", lambda **kw: (
        SimpleNamespace(base_url=BUILTIN_URL, api_key="builtin-key"), "builtin-model"))
    client, model = aux.resolve_provider_client(f"custom:{name}")
    assert client is not None
    assert (str(client.base_url).rstrip("/"), client.api_key, model) == (NAMED_URL, "named-key", "entry-model")
    # Public overrides still compose over saved defaults; Incognito strips these at its call boundary.
    client, model = aux.resolve_provider_client(f"custom:{name}", model="override-model",
                                                explicit_base_url="https://override.invalid/v1",
                                                explicit_api_key="override-key")
    assert client is not None
    assert (str(client.base_url).rstrip("/"), client.api_key, model) == (
        "https://override.invalid/v1", "override-key", "override-model")
    with aux._client_cache_lock:
        aux._client_cache.clear()
    try:
        previous = None
        for _ in range(2):
            named, model = aux._get_cached_client(f"custom:{name}", model="same-model", task="compression")
            assert named is not None
            assert (str(named.base_url).rstrip("/"), named.api_key, model) == (NAMED_URL, "named-key", "same-model")
            assert previous is None or previous is named
            previous = named
            if name == "nous":
                builtin, model = aux._get_cached_client("nous", model="same-model", task="compression")
                assert builtin is not None and builtin is not named
                assert (str(builtin.base_url).rstrip("/"), builtin.api_key, model) == (
                    BUILTIN_URL, "builtin-key", "same-model")
    finally:
        with aux._client_cache_lock:
            aux._client_cache.clear()
