"""Policies through real profile storage, registry dispatch and review boundaries."""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

from tools import skill_provenance as provenance


def test_review_modes_and_trigger_scope(tmp_path, monkeypatch):
    from agent.background_review import _review_tool_whitelist
    from tools.skill_manager_tool import skill_manage
    from tools.review_observations import bind_review_source, list_observations
    from tools.registry import registry
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("memory:\n  background_policy: observe_only\nauxiliary:\n  background_review:\n    skill_mode: observe\n", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    with (tmp_path / "config.yaml").open("a", encoding="utf-8") as config:
        config.write(f"skills:\n  external_dirs: [{json.dumps(str(external))}]\n")
    (external / "SKILL.md").write_text("---\nname: sample\ndescription: Sample\n---\nOriginal", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "sample").symlink_to(external, target_is_directory=True)
    from tools.skills_tool import skill_view
    assert "Original" in skill_view("sample")
    token = provenance.set_current_write_origin(provenance.BACKGROUND_REVIEW)
    attended = provenance.set_review_attended(True)
    try:
        with bind_review_source("session", [{"role": "user", "content": "Prefer safer checks"}]):
            result = json.loads(registry.get_entry("skill_manage").handler({"operations": [{"action": "delete", "name": "sample"}]}))
            assert result["observed"]
            assert (external / "SKILL.md").exists()
        assert list_observations()[0]["source"]["messages"][0]["content"] == "Prefer safer checks"
        view = handle_pending_subcommand("memory", [])
        assert "observe_only" in view and "skill_mode = observe" in view and "replace/remove" in view
        agent = SimpleNamespace(_memory_enabled=True, _user_profile_enabled=True)
        allowed, extras = _review_tool_whitelist(agent, {"skill_mode": "observe", "extra_tools": ["terminal", "memory"]}, False)
        assert {"skill_manage", "skill_view", "read_file"} <= allowed
        assert not ({"terminal", "memory", "write_file", "execute_code"} & allowed)
        assert not extras
        (tmp_path / "config.yaml").write_text("auxiliary:\n  background_review:\n    skill_mode: off\n", encoding="utf-8")
        assert not json.loads(skill_manage("delete", "sample"))["success"]
    finally:
        provenance.reset_review_attended(attended)
        provenance.reset_current_write_origin(token)


def test_observation_concurrency_profile_isolation_and_disposition(tmp_path, monkeypatch):
    from tools.review_observations import record_observation, list_observations, dispose, bind_review_source
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "one"))
    def record(_):
        with bind_review_source("session", [{"role": "user", "content": "evidence"}]):
            return record_observation("skills", {"name": "example"})["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(record, range(12)))
    assert len(set(ids)) == 1 and len(list_observations()) == 1
    assert dispose(ids[0], "accepted", "applied separately")
    assert record(0) == ids[0] and not list_observations()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "two"))
    assert not list_observations(status=None)


def test_explicit_refine_bypasses_auto_enable_not_skill_off(tmp_path, monkeypatch):
    from run_agent import AIAgent
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("auxiliary:\n  background_review:\n    enabled: false\n    skill_mode: off\n", encoding="utf-8")
    agent = SimpleNamespace(_delegate_depth=0, _spawn_background_review_now=Mock())
    AIAgent._spawn_background_review(agent, [], review_memory=True, review_skills=True, explicit=True)
    assert agent._spawn_background_review_now.call_args.kwargs["explicit"]
    assert not agent._spawn_background_review_now.call_args.kwargs["review_skills"]
