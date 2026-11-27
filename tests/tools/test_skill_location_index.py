"""locate_skill resolves bare names from a per-root index instead of globbing on every call."""

from __future__ import annotations

import json

import pytest

import tools.skills_tool as st
from tools.skills_tool import locate_skill


def _skill(root, rel, name=None):
    d = root / rel
    d.mkdir(parents=True)
    body = f"---\nname: {name or d.name}\ndescription: x\n---\nbody\n"
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return d


@pytest.fixture
def roots(tmp_path, monkeypatch):
    from agent.skill_utils import TIER_LOCAL
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(st, "_SKILL_LOCATION_CACHE", {})
    return root, [(TIER_LOCAL, root)]


def test_locates_by_dir_name_frontmatter_name_and_flat_markdown(roots):
    root, rs = roots
    d = _skill(root, "cat/deep/alpha")
    _skill(root, "cat/beta", name="renamed")
    (root / "cat" / "flat.md").write_text("flat", encoding="utf-8")

    assert locate_skill("alpha", None, rs)[2] == d / "SKILL.md"
    assert locate_skill("renamed", None, rs)[2] == root / "cat" / "beta" / "SKILL.md"
    assert locate_skill("flat", None, rs)[2] == root / "cat" / "flat.md"
    assert "not found" in json.loads(locate_skill("missing", None, rs)[0])["error"]


def test_repeated_lookups_do_not_glob_or_rewalk(roots, monkeypatch):
    root, rs = roots
    _skill(root, "cat/alpha")
    assert locate_skill("alpha", None, rs)[2] is not None  # builds the index

    def boom(*_a, **_k):
        raise AssertionError("per-call filesystem scan")

    monkeypatch.setattr(type(root), "rglob", boom)
    monkeypatch.setattr("agent.skill_utils.iter_skill_index_files", boom)
    for _ in range(3):
        assert locate_skill("alpha", None, rs)[2] == root / "cat" / "alpha" / "SKILL.md"


def test_install_and_uninstall_are_seen_without_waiting(roots):
    import shutil
    root, rs = roots
    _skill(root, "cat/alpha")
    assert locate_skill("alpha", None, rs)[2] is not None
    assert locate_skill("later", None, rs)[2] is None

    _skill(root, "cat/later")
    assert locate_skill("later", None, rs)[2] == root / "cat" / "later" / "SKILL.md"

    shutil.rmtree(root / "cat" / "alpha")
    assert locate_skill("alpha", None, rs)[2] is None


def test_clear_skills_cache_drops_the_index_and_ttl_bounds_in_place_edits(roots, monkeypatch):
    root, rs = roots
    d = _skill(root, "cat/alpha")
    assert locate_skill("alpha", None, rs)[2] is not None
    assert st._SKILL_LOCATION_CACHE

    (d / "SKILL.md").write_text("---\nname: renamed\n---\n", encoding="utf-8")  # no dir mtime change
    st.clear_skills_cache()
    assert not st._SKILL_LOCATION_CACHE
    assert locate_skill("renamed", None, rs)[2] == d / "SKILL.md"

    clock = {"now": 1000.0}
    monkeypatch.setattr(st.time, "monotonic", lambda: clock["now"])
    st.clear_skills_cache()
    assert locate_skill("renamed", None, rs)[2] is not None
    (d / "SKILL.md").write_text("---\nname: again\n---\n", encoding="utf-8")
    assert locate_skill("again", None, rs)[2] is None
    clock["now"] += st._SKILLS_CACHE_TTL_SECONDS + 1
    assert locate_skill("again", None, rs)[2] == d / "SKILL.md"


def test_path_shaped_names_keep_the_direct_and_scan_semantics(roots):
    root, rs = roots
    d = _skill(root, "mlops/axolotl")
    assert locate_skill("mlops/axolotl", None, rs)[2] == d / "SKILL.md"
    assert locate_skill("axolotl", "mlops/axolotl", rs)[2] == d / "SKILL.md"
