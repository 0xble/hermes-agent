"""Configured bundled providers survive PM rebuilds without an installed user copy."""
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from pm.environment import managed_environment
from pm import plugins_state, workspace
from plugins import memory
from tests.pm.test_environment_build import locked_project as locked_project


def _provider(root, name, dependency):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text("# MemoryProvider fixture, never imported\n")
    (directory / "plugin.yaml").write_text(
        f"name: {name}\npip_dependencies: ['{dependency}']\n")
    return directory


@pytest.mark.parametrize("shadow", [False, True])
def test_configured_bundled_provider_builds_complete_union(locked_project, tmp_path, monkeypatch, shadow):  # health: allow F811 -- pytest injects the imported fixture
    core, uv, _env = locked_project
    metadata = core / "pyproject.toml"
    metadata.write_text(metadata.read_text().replace('[tool.uv.workspace]\nmembers=["member"]\n', ""))
    bundled = _provider(core / "plugins/memory", "fixture-memory", "member-dep==1.0")
    home = tmp_path / "home"
    sibling = home / "profiles/work"
    sibling.mkdir(parents=True)
    (home / "config.yaml").write_text("memory: {provider: fixture-memory}\n")
    (sibling / "config.yaml").write_text("plugins: {enabled: [sibling-plugin]}\n")
    other = _provider(sibling / "plugins", "sibling-plugin", "other-dep==1.0")
    if shadow:
        _provider(home / "plugins", "fixture-memory", "missing-shadow-dependency==1.0")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(workspace.paths, "repo_root", lambda: core)
    monkeypatch.setattr(memory, "_MEMORY_PLUGINS_DIR", core / "plugins/memory")
    monkeypatch.setattr("pm._uv._toolchain", lambda **kwargs: (uv, Path(sys.executable)))

    entries = workspace.enabled_plugin_entries()
    assert (home / "plugins", "fixture-memory", bundled) in entries
    assert (sibling / "plugins", "sibling-plugin", other) in entries
    assert plugins_state.enabled_plugins_ordered()[home / "plugins"] == ["fixture-memory"]
    members = workspace.enabled_member_dirs()
    assert members == [bundled, other]
    environment = managed_environment(tmp_path / "candidate")
    workspace.lock_and_sync(members, ["chosen"], root=tmp_path / "workspace", source=core,
                            seed_lock=core / "uv.lock", environment=environment)
    packages = subprocess.check_output(
        [str(environment.destination / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")), "-c", "from importlib.metadata import distributions; "
         "print(sorted(d.metadata['Name'].replace('_', '-') for d in distributions()))"], text=True)
    for expected in ("base-dep", "member-dep", "other-dep", "chosen-dep"):
        assert expected in packages
    assert "missing-shadow-dependency" not in packages
    assert not (home / "plugins/fixture-memory").exists() or shadow
    generated = tomllib.loads((tmp_path / "workspace/pyproject.toml").read_text())
    assert len(generated["tool"]["uv"]["workspace"]["members"]) == 2


def test_provider_consent_and_general_plugin_resolution_are_preserved(tmp_path, monkeypatch):
    home = tmp_path / "home"
    core = tmp_path / "core"
    bundled = _provider(core / "plugins/memory", "fixture-memory", "bundled-dep")
    user = _provider(home / "plugins", "fixture-memory", "user-dep")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(workspace.paths, "repo_root", lambda: core)
    monkeypatch.setattr(memory, "_MEMORY_PLUGINS_DIR", core / "plugins/memory")
    config = home / "config.yaml"
    config.write_text("plugins: {enabled: [fixture-memory]}\n")
    assert workspace.enabled_member_dirs() == [user]
    config.write_text("memory: {provider: fixture-memory}\nplugins: {disabled: [fixture-memory]}\n")
    assert workspace.enabled_member_dirs() == [bundled]  # runtime ignores bundled disables
    config.write_text("plugins: {enabled: [fixture-memory], disabled: [fixture-memory]}\n")
    assert workspace.enabled_member_dirs() == []
