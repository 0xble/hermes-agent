from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


PLUGIN = Path(__file__).parents[2] / "plugins" / "canonical-skill-guard" / "__init__.py"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("canonical_skill_guard", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_external_skill_write_is_blocked_and_routes_to_observation(monkeypatch, tmp_path):
    plugin = _load_plugin()
    external = tmp_path / "generated-skills"
    (external / "deploy" ).mkdir(parents=True)
    (external / "deploy" / "SKILL.md").write_text("# deploy\n", encoding="utf-8")
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [external])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    result = plugin._on_pre_tool_call(
        tool_name="skill_manage",
        args={"operations": [{"name": "deploy", "action": "patch"}]},
    )

    assert result and result["action"] == "block"
    assert "$HERMES_HOME/observations/deploy.md" in result["message"]


def test_local_skill_write_is_allowed(monkeypatch, tmp_path):
    plugin = _load_plugin()
    external = tmp_path / "generated-skills"
    external.mkdir()
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", lambda: [external])

    assert plugin._on_pre_tool_call(
        tool_name="skill_manage",
        args={"operations": [{"name": "local-only", "action": "patch"}]},
    ) is None


def test_non_skill_tools_are_ignored(monkeypatch):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "_external_skill_names", lambda: {"deploy"})
    assert plugin._on_pre_tool_call(tool_name="write_file", args={"path": "deploy/SKILL.md"}) is None


def test_manifest_declares_every_registered_hook():
    import hermes_yaml as yaml

    plugin = _load_plugin()
    registered: list[str] = []

    class _Ctx:
        def register_hook(self, name, _callback):
            registered.append(name)

    plugin.register(_Ctx())
    manifest = yaml.safe_load((PLUGIN.parent / "plugin.yaml").read_text(encoding="utf-8"))

    assert registered
    assert set(registered) <= set(manifest.get("provides_hooks") or [])


@pytest.mark.parametrize("name", ["../skills/hermes-agent/SKILL", "../../Documents/notes", "absolute", r"..\notes", "bad:name", "valid"])
def test_failed_owner_lookup_cannot_write_outside_observation_inbox(monkeypatch, tmp_path, name):
    plugin = _load_plugin()
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "Documents" / "notes.md"
    target.parent.mkdir()
    target.write_text("original")
    if name == "absolute":
        name = str(target.with_suffix(""))
    monkeypatch.setenv("HERMES_HOME", str(home))
    def unavailable():
        raise OSError("external roots unavailable")
    monkeypatch.setattr("agent.skill_utils.get_external_skills_dirs", unavailable)
    result = plugin._on_pre_tool_call(tool_name="skill_manage", args={"name": name, "action": "patch"})
    assert result and result["action"] == "block"
    assert target.read_text() == "original"
    observations = list((home / "observations").glob("*.md"))
    assert bool(observations) == (name == "valid")
    if observations:
        assert '"name": "valid"' in observations[0].read_text()


@pytest.mark.parametrize("symlink", ["leaf", "directory"])
def test_observation_symlinks_cannot_redirect_a_blocked_write(monkeypatch, tmp_path, symlink):
    plugin = _load_plugin()
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "deploy.md"
    target.write_text("original")
    root = home / "observations"
    if symlink == "directory":
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir()
        (root / "deploy.md").symlink_to(target)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(plugin, "_external_skill_names", lambda: {"deploy"})
    result = plugin._on_pre_tool_call(tool_name="skill_manage", args={"name": "deploy", "action": "patch"})
    assert result and result["action"] == "block"
    assert target.read_text() == "original"
