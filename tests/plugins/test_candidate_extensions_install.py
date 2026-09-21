"""E2E coverage for installing and discovering the candidate extension set."""

from __future__ import annotations

import importlib
from pathlib import Path

import yaml

from scripts.install_candidate_extensions import EXTENSIONS, install


def test_install_is_idempotent_and_enables_all_candidate_extensions(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    source = Path(__file__).parents[2] / "candidate-extensions"
    install(home, source)
    first = (home / "config.yaml").read_text(encoding="utf-8")
    install(home, source)
    assert (home / "config.yaml").read_text(encoding="utf-8") == first
    config = yaml.safe_load(first)
    assert all(name in config["plugins"]["enabled"] for name in EXTENSIONS)
    assert all((home / "plugins" / name / "plugin.yaml").is_file() for name in EXTENSIONS)


def test_installed_extensions_load_through_real_plugin_discovery(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    source = Path(__file__).parents[2] / "candidate-extensions"
    install(home, source)
    monkeypatch.setenv("HERMES_HOME", str(home))

    import hermes_cli.plugins as plugins_mod

    plugins_mod._plugin_manager = plugins_mod.PluginManager()
    plugins_mod.discover_plugins(force=True)
    loaded = plugins_mod.get_plugin_manager().list_plugins()
    names = {item["name"] for item in loaded if item["enabled"]}
    assert set(EXTENSIONS) <= names

    # The tool registrations are the user-facing E2E proof, rather than a
    # source scan or a manifest count assertion.
    from tools.registry import registry
    assert all(registry.get_entry(name) is not None for name in
               {"goal_set", "memory_undo", "request_update", "review_candidate"})


def test_maintenance_entrypoint_tracks_installed_code_without_changing_config(tmp_path):
    import os
    import subprocess
    import sys
    from scripts.install_candidate_extensions import install_maintenance
    home = tmp_path / "profile"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("unrelated: preserve\n")
    installed = home / "hermes-agent/scripts"
    installed.mkdir(parents=True)
    target = installed / "check_fork_patches.py"
    target.write_text("print('first revision')\n")
    install_maintenance(home)
    env = dict(os.environ, HERMES_HOME=str(home))
    command = [sys.executable, str(home / "scripts/check_fork_patches.py")]
    assert subprocess.check_output(command, env=env, text=True).strip() == "first revision"
    target.write_text("print('promoted revision')\n")
    assert subprocess.check_output(command, env=env, text=True).strip() == "promoted revision"
    assert config.read_text() == "unrelated: preserve\n"
    assert not (home / "plugins").exists()
