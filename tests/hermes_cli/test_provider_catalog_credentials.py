"""Catalog entries must preserve the credentials declared by provider plugins."""

from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("catalog_env", [(), ("CMD_API_KEY",)])
def test_catalog_preserves_plugin_credentials(monkeypatch, catalog_env):
    import model_tools  # noqa: F401
    from hermes_cli import providers
    from providers import get_provider_profile

    profile = get_provider_profile("commandcode")
    key_vars = tuple(v for v in profile.env_vars if not v.endswith(("_BASE_URL", "_URL")))
    catalog = SimpleNamespace(
        env=catalog_env, name="CommandCode", api=profile.base_url, doc="",
    )
    monkeypatch.setattr(providers, "_models_dev_info", lambda *args: catalog)

    resolved = providers.resolve_provider_full(profile.name, {}, [])

    assert set(key_vars) <= set(resolved.api_key_env_vars)
    assert set(catalog_env) <= set(resolved.api_key_env_vars)
    assert len(resolved.api_key_env_vars) == len(set(resolved.api_key_env_vars))
    assert not any(v.endswith(("_BASE_URL", "_URL")) for v in resolved.api_key_env_vars)
    assert resolved.transport == "openai_chat"