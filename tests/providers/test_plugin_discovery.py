"""Tests for the model-providers plugin discovery system.

Verifies that:
 1. All bundled providers at plugins/model-providers/<name>/ are discovered
 2. User plugins at $HERMES_HOME/plugins/model-providers/<name>/ override bundled
 3. plugin.yaml manifests with kind=model-provider are correctly categorized
"""

from __future__ import annotations

import sys
from pathlib import Path



REPO_ROOT = Path(__file__).resolve().parents[2]


def _clear_provider_caches():
    """Force providers/__init__.py to re-discover on next list_providers()."""
    import providers as _pkg
    _pkg._REGISTRY.clear()
    _pkg._ALIASES.clear()
    _pkg._PROVIDER_LIST_CACHE = None
    _pkg._discovered = False
    # Evict any cached plugin modules so the next import re-executes.
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("plugins.model_providers")
            or mod.startswith("_hermes_user_provider")
        ):
            del sys.modules[mod]


def _bundled_plugin_dirs():
    plugins_dir = REPO_ROOT / "plugins" / "model-providers"
    assert plugins_dir.is_dir(), f"Missing {plugins_dir}"
    # Git may leave empty directories or bytecode after a bundled provider moves
    # out of core. Either plugin marker enrolls a directory so a missing partner
    # still fails the layout contract instead of silently disappearing.
    return [
        child for child in plugins_dir.iterdir()
        if child.is_dir() and not child.name.startswith(("_", "."))
        and any((child / marker).is_file() for marker in ("__init__.py", "plugin.yaml"))
    ]


def test_bundled_plugins_discovered():
    """Every bundled provider has both import code and a manifest."""
    child_dirs = _bundled_plugin_dirs()
    assert child_dirs, "No bundled provider plugins"

    for child in child_dirs:
        assert (child / "__init__.py").exists(), f"{child.name} missing __init__.py"
        assert (child / "plugin.yaml").exists(), f"{child.name} missing plugin.yaml"


def test_all_profiles_register():
    """After discovery, the registry must contain every bundled provider directory.

    This is an invariant — the number of profiles matches the number of plugin
    directories, not a hardcoded count. Counts shift when providers are
    added/removed; that's expected and shouldn't break CI.
    """
    _clear_provider_caches()
    from providers import list_providers

    plugin_dir_count = len(_bundled_plugin_dirs())

    profiles = list_providers()
    names = sorted(p.name for p in profiles)
    # Some plugin __init__.py files register multiple profiles, so the registry
    # count is >= the directory count (never less).
    assert len(names) >= plugin_dir_count, (
        f"Expected at least {plugin_dir_count} profiles (one per plugin dir), got {len(names)}: {names}"
    )


def test_user_plugin_overrides_bundled(tmp_path, monkeypatch):
    """A user plugin with the same name must override the bundled profile."""
    # Point HERMES_HOME at a fresh temp dir
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    # get_hermes_home() may be module-cached depending on codebase; ensure the
    # env var is the source of truth. Most code paths re-read it each call.

    # Drop a user plugin that replaces 'gmi'
    user_gmi = hermes_home / "plugins" / "model-providers" / "gmi"
    user_gmi.mkdir(parents=True)
    (user_gmi / "__init__.py").write_text(
        "from providers import register_provider\n"
        "from providers.base import ProviderProfile\n"
        "\n"
        "custom_gmi = ProviderProfile(\n"
        '    name="gmi",\n'
        '    aliases=("gmi-user-override-test",),\n'
        '    env_vars=("GMI_API_KEY",),\n'
        '    base_url="https://user-override.example.com/v1",\n'
        '    auth_type="api_key",\n'
        ")\n"
        "register_provider(custom_gmi)\n"
    )
    (user_gmi / "plugin.yaml").write_text(
        "name: gmi-user-override\n"
        "kind: model-provider\n"
        "version: 0.0.1\n"
        "description: Test user override\n"
    )

    _clear_provider_caches()
    from providers import get_provider_profile

    gmi = get_provider_profile("gmi")
    assert gmi is not None
    assert gmi.base_url == "https://user-override.example.com/v1", (
        f"User override not applied; got base_url={gmi.base_url!r}"
    )
    assert "gmi-user-override-test" in gmi.aliases

    # Clean up: reset discovery state so other tests see the bundled version
    _clear_provider_caches()
