"""Unknown external ownership cannot authorize autonomous skill writes."""
import pytest


@pytest.mark.parametrize("action", ["patch", "write_file", "remove_file"])
def test_background_write_refuses_unavailable_external_ownership(tmp_path, monkeypatch, action):
    from agent import skill_utils
    from tools import skill_manager_guards as guards, skill_manager_tool as skills
    directory = tmp_path / "external-skill"
    directory.mkdir()
    content = directory / "SKILL.md"
    content.write_text("keep unchanged", encoding="utf-8")
    monkeypatch.setattr(guards, "_is_background_review", lambda: True)
    monkeypatch.setattr(skills, "_find_skill", lambda name: {"path": directory})
    def unavailable(path):
        raise OSError("ownership unavailable")
    monkeypatch.setattr(skill_utils, "is_external_skill_path", unavailable)
    result = guards._background_review_preflight(action, "external-skill")
    assert result is not None and result["success"] is False
    assert "ownership" in result["error"].lower()
    assert content.read_text(encoding="utf-8") == "keep unchanged"
