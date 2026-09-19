from __future__ import annotations

import importlib.util
from pathlib import Path


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
