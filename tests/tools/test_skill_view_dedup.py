"""Tests for skill_view repeat-view dedup (unchanged-skill stub)."""

import json
import os
import re
import time

import pytest

from agent.conversation_compression_boundary import mark_context_loss_boundary
from tools.skills_tool import (
    _skill_view_with_bump,
    reset_skill_view_dedup,
)

_SETTLED_NS = time.time_ns() - 3600 * 1_000_000_000


def _pin_mtime(path, ns):
    os.utime(path, ns=(ns, ns))


@pytest.fixture
def skills_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    skills = home / "skills"
    d = skills / "demo-dedup-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: demo-dedup-skill\ndescription: Demo skill for dedup tests.\n---\n"
        "# Demo\n\nStep one: run the demo procedure fully.\n",
        encoding="utf-8",
    )
    refs = d / "references"
    refs.mkdir()
    (refs / "guide.md").write_text("# Guide\n\nDetailed reference content here.\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    reset_skill_view_dedup()
    from tools.skill_manager_guards import _reset_background_review_read_marks
    _reset_background_review_read_marks()
    return home

def _view(name, file_path=None, task="t-svd"):
    args = {"name": name}
    if file_path:
        args["file_path"] = file_path
    return json.loads(_skill_view_with_bump(args, task_id=task))

class TestSkillViewDedup:
    def test_first_view_returns_full_content(self, skills_home):
        r = _view("demo-dedup-skill")
        assert r["success"] is True
        assert "Step one" in r.get("content", "")

    def test_repeat_view_returns_stub(self, skills_home):
        first = _view("demo-dedup-skill")
        r2 = _view("demo-dedup-skill")
        assert r2["success"] is True
        assert r2.get("dedup") is True
        assert r2.get("content_returned") is False
        assert "content" not in r2
        assert r2["name"] == "demo-dedup-skill"
        assert r2["path"] == first["path"]

    def test_changed_dependency_note_returns_full_view(self, skills_home):
        from tools import skills_tool_dedup
        base = {"name": "demo-dedup-skill", "content": "body", "path": "p"}
        assert skills_tool_dedup.dedup_skill_view("t-deps", "demo-dedup-skill", None, base, "{}") is None
        assert skills_tool_dedup.dedup_skill_view("t-deps", "demo-dedup-skill", None, dict(base), "{}")
        noted = dict(base, deps_note="install failed")
        assert skills_tool_dedup.dedup_skill_view("t-deps", "demo-dedup-skill", None, noted, "{}") is None

    def test_modified_skill_returns_full_content(self, skills_home):
        _view("demo-dedup-skill")
        md = skills_home / "skills" / "demo-dedup-skill" / "SKILL.md"
        time.sleep(0.01)
        md.write_text(md.read_text(encoding="utf-8") + "\nStep two: new instruction.\n", encoding="utf-8")
        r2 = _view("demo-dedup-skill")
        assert "Step two" in r2.get("content", "")
        assert r2.get("dedup") is None

    def test_linked_file_dedup_is_independent(self, skills_home):
        _view("demo-dedup-skill")
        # First view of a DIFFERENT file within the skill: full content.
        r = _view("demo-dedup-skill", file_path="references/guide.md")
        assert "Detailed reference" in r.get("content", "")
        # Repeat of that file: stub.
        r2 = _view("demo-dedup-skill", file_path="references/guide.md")
        assert r2.get("dedup") is True

    def test_changed_linked_file_returns_full_content(self, skills_home):
        _view("demo-dedup-skill", file_path="references/guide.md")
        ref = skills_home / "skills" / "demo-dedup-skill" / "references" / "guide.md"
        ref.write_text("# Guide\n\nUpdated reference content.\n", encoding="utf-8")
        r2 = _view("demo-dedup-skill", file_path="references/guide.md")
        assert "Updated reference" in r2.get("content", "")
        assert r2.get("dedup") is None

    def test_different_tasks_do_not_share_cache(self, skills_home):
        _view("demo-dedup-skill", task="task-A")
        r = _view("demo-dedup-skill", task="task-B")
        assert "Step one" in r.get("content", "")

    def test_new_linked_file_with_same_body_returns_full_view(self, skills_home):
        first = _view("demo-dedup-skill")
        refs = skills_home / "skills" / "demo-dedup-skill" / "references"
        (refs / "extra.md").write_text("# Extra\n", encoding="utf-8")
        r2 = _view("demo-dedup-skill")
        assert r2.get("dedup") is None
        assert r2.get("linked_files") != first.get("linked_files")

    def test_tracked_tasks_are_bounded(self, skills_home, monkeypatch):
        from tools import skills_tool_dedup
        monkeypatch.setattr(skills_tool_dedup, "_SKILL_VIEW_DEDUP_TASK_CAP", 3)
        for n in range(6):
            _view("demo-dedup-skill", task=f"task-{n}")
        assert len(skills_tool_dedup._skill_view_tracker) == 3
        assert "task-0" not in skills_tool_dedup._skill_view_tracker

    def test_boundary_on_untracked_task_leaves_no_state(self, skills_home):
        from tools import skills_tool_dedup
        mark_context_loss_boundary(type("Agent", (), {"session_id": "s"})(), "never-viewed")
        assert "never-viewed" not in skills_tool_dedup._skill_view_tracker

    def test_context_generation_returns_full_content(self, skills_home):
        _view("demo-dedup-skill")
        assert _view("demo-dedup-skill").get("dedup") is True
        mark_context_loss_boundary(type("Agent", (), {"session_id": "session-svd"})(), "t-svd")
        r2 = _view("demo-dedup-skill")
        assert "Step one" in r2.get("content", "")
        assert r2.get("dedup") is None
        # The reloaded body is the new baseline: the next repeat points at it again.
        assert _view("demo-dedup-skill").get("dedup") is True

    def test_pointer_carries_hash_name_file_and_path(self, skills_home):
        full = _view("demo-dedup-skill", file_path="references/guide.md")
        ptr = _view("demo-dedup-skill", file_path="references/guide.md")
        assert ptr["success"] is True and ptr["dedup"] is True
        assert ptr["name"] == full["name"]
        assert ptr["file"] == "references/guide.md"
        assert re.fullmatch(r"[0-9a-f]{64}", ptr["content_hash"])
        main = _view("demo-dedup-skill")
        main_ptr = _view("demo-dedup-skill")
        assert main_ptr["file"] == "SKILL.md" and main_ptr["path"] == main["path"]
        assert main_ptr["content_hash"] != ptr["content_hash"]

    def test_linked_files_are_tracked_independently_across_boundaries(self, skills_home):
        guide2 = skills_home / "skills" / "demo-dedup-skill" / "references" / "other.md"
        guide2.write_text("# Other\n\nSecond reference body.\n", encoding="utf-8")
        _view("demo-dedup-skill")
        _view("demo-dedup-skill", file_path="references/guide.md")
        # Loading a different linked file does not satisfy the first one's repeat.
        assert "Second reference" in _view("demo-dedup-skill", file_path="references/other.md")["content"]
        # Changing one file reloads only that file; the others still point.
        guide2.write_text("# Other\n\nSecond reference, edited.\n", encoding="utf-8")
        assert "edited" in _view("demo-dedup-skill", file_path="references/other.md")["content"]
        assert _view("demo-dedup-skill").get("dedup") is True
        assert _view("demo-dedup-skill", file_path="references/guide.md").get("dedup") is True

    def test_boundary_in_one_session_does_not_touch_another(self, skills_home):
        _view("demo-dedup-skill", task="sess-A")
        _view("demo-dedup-skill", task="sess-B")
        mark_context_loss_boundary(type("Agent", (), {"session_id": "sess-A"})(), "sess-A")
        # A lost its context -> full body; B's context is intact -> pointer.
        assert "Step one" in _view("demo-dedup-skill", task="sess-A")["content"]
        assert _view("demo-dedup-skill", task="sess-B").get("dedup") is True
        # A pointer in one session is never served from another session's load.
        assert "Step one" in _view("demo-dedup-skill", task="sess-C")["content"]

    def test_no_task_id_never_dedups(self, skills_home):
        args = {"name": "demo-dedup-skill"}
        json.loads(_skill_view_with_bump(args, task_id=None))
        r2 = json.loads(_skill_view_with_bump(args, task_id=None))
        assert "Step one" in r2.get("content", "")

    def test_background_review_skips_dedup_and_marks_read(self, skills_home):
        from tools.skill_provenance import (
            reset_current_write_origin,
            set_current_write_origin,
        )

        _view("demo-dedup-skill")

        token = set_current_write_origin("background_review")
        try:
            review = _view("demo-dedup-skill")
        finally:
            reset_current_write_origin(token)

        assert review["success"] is True
        assert "Step one" in review.get("content", "")
        assert review.get("dedup") is None
        assert review.get("content_returned") is None
        # The real read marks the file, so the fork's read-before-write guard now admits the patch.
        from tools.skill_manager_guards import _background_review_has_read
        assert _background_review_has_read(skills_home / "skills" / "demo-dedup-skill" / "SKILL.md")

    def test_background_review_does_not_pollute_foreground_cache(self, skills_home):
        from tools.skill_provenance import (
            reset_current_write_origin,
            set_current_write_origin,
        )

        token = set_current_write_origin("background_review")
        try:
            _view("demo-dedup-skill")
        finally:
            reset_current_write_origin(token)

        foreground = _view("demo-dedup-skill")
        assert "Step one" in foreground.get("content", "")

        repeat = _view("demo-dedup-skill")
        assert repeat.get("dedup") is True
        assert repeat.get("content_returned") is False


class TestSkillViewDedupRaces:
    """A repeat view must never claim 'unchanged' for content the model was not served."""

    def _md(self, skills_home):
        return skills_home / "skills" / "demo-dedup-skill" / "SKILL.md"

    def test_write_while_serving_is_not_deduped(self, skills_home, monkeypatch):
        import tools.skills_tool as st

        md = self._md(skills_home)
        real = st._log_security_warnings

        def write_after_read(*args, **kwargs):
            # A background-review skill_manage patch lands after skill_view read v1.
            md.write_text(md.read_text(encoding="utf-8").replace("Step one", "Step NEW!"), encoding="utf-8")
            return real(*args, **kwargs)

        monkeypatch.setattr(st, "_log_security_warnings", write_after_read)
        first = _view("demo-dedup-skill")
        assert "Step one" in first["content"]
        monkeypatch.setattr(st, "_log_security_warnings", real)

        again = _view("demo-dedup-skill")
        assert again.get("dedup") is None
        assert "Step NEW!" in again.get("content", "")

    def test_same_size_rewrite_with_pinned_mtime_is_not_deduped(self, skills_home):
        md = self._md(skills_home)
        _view("demo-dedup-skill")
        md.write_text(md.read_text(encoding="utf-8").replace("Step one", "Step 111"), encoding="utf-8")
        _pin_mtime(md, _SETTLED_NS)

        again = _view("demo-dedup-skill")
        assert again.get("dedup") is None
        assert "Step 111" in again.get("content", "")

    def test_freshly_written_unchanged_file_is_deduped_by_content(self, skills_home):
        md = self._md(skills_home)
        # Content, not timestamps, decides: a file written this instant (any clock skew between
        # this host and the filesystem included) is still "unchanged" when its bytes are.
        md.write_text(md.read_text(encoding="utf-8"), encoding="utf-8")
        _view("demo-dedup-skill")

        again = _view("demo-dedup-skill")
        assert again.get("dedup") is True
