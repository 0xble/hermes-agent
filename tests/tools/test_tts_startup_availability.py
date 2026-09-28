"""A TTS capability probe must never install a package during unrelated agent startup."""

import pytest

from tools import lazy_deps, tts_tool
from tools.registry import invalidate_check_fn_cache, registry


def test_tts_schema_defers_missing_sdk_install_until_use(monkeypatch):
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "edge"})
    monkeypatch.setattr(tts_tool, "_package_installed", lambda _name: False)
    monkeypatch.setattr(tts_tool, "_check_neutts_available", lambda: False)
    monkeypatch.setattr(lazy_deps, "_allow_lazy_installs", lambda: True)
    calls = []
    monkeypatch.setattr(lazy_deps, "ensure", lambda feature, **_kw: calls.append(feature))
    invalidate_check_fn_cache()

    schemas = registry.get_definitions({"text_to_speech"})
    assert [schema["function"]["name"] for schema in schemas] == ["text_to_speech"]
    assert calls == []

    # Execution, unlike discovery, may install the SDK for the selected provider.
    monkeypatch.setattr(tts_tool.importlib, "import_module", lambda _name: object())
    assert tts_tool._select_builtin_engine("edge") == ("edge", None)
    assert calls == ["tts.edge"]


def test_tts_schema_respects_lazy_install_opt_out(monkeypatch):
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "edge"})
    monkeypatch.setattr(tts_tool, "_package_installed", lambda _name: False)
    monkeypatch.setattr(tts_tool, "_check_neutts_available", lambda: False)
    monkeypatch.setattr(lazy_deps, "_allow_lazy_installs", lambda: False)
    monkeypatch.setattr(lazy_deps, "ensure", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("must not install")))
    invalidate_check_fn_cache()

    assert registry.get_definitions({"text_to_speech"}) == []


@pytest.mark.parametrize(("provider", "module"), [("elevenlabs", "elevenlabs"), ("mistral", "mistralai")])
def test_cloud_tts_schema_defers_install(monkeypatch, provider, module):
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": provider})
    monkeypatch.setattr(tts_tool, "_package_installed", lambda _name: False)
    monkeypatch.setattr(tts_tool, "_resolve_provider_key", lambda *_args: "test-key")
    monkeypatch.setattr(lazy_deps, "_allow_lazy_installs", lambda: True)
    monkeypatch.setattr(lazy_deps, "ensure", lambda *_args, **_kw: (_ for _ in ()).throw(AssertionError("must not install")))
    invalidate_check_fn_cache()

    assert tts_tool._sdk_available_or_installable(module)
    assert registry.get_definitions({"text_to_speech"})


def test_managed_install_without_target_does_not_advertise_missing_sdk(monkeypatch):
    from hermes_cli import config

    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "edge"})
    monkeypatch.setattr(tts_tool, "_package_installed", lambda _name: False)
    monkeypatch.setattr(tts_tool, "_check_neutts_available", lambda: False)
    monkeypatch.setattr(lazy_deps, "_allow_lazy_installs", lambda: True)
    monkeypatch.setattr(lazy_deps, "_lazy_install_target", lambda: None)
    monkeypatch.setattr(config, "get_managed_system", lambda: "nix")
    monkeypatch.setattr(lazy_deps, "ensure", lambda *_args, **_kw: (_ for _ in ()).throw(AssertionError("must not install")))
    invalidate_check_fn_cache()

    assert registry.get_definitions({"text_to_speech"}) == []
