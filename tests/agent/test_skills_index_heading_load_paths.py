"""Every category heading rendered in the system-prompt skills index must be a
valid ``skill_view`` load path.

The index groups skills under category headings and instructs the agent to load
by category path — the name-collision label says so literally ("load via
category path"). Two of those headings are SYNTHETIC and match no directory:

* ``general:``      — skills sitting directly in a skills root.
* ``org:<org_id>:`` — org-mirror skills, which live at ``_org/<org_id>/...``.

Before this fix ``skill_view("general/x")`` and ``skill_view("org:acme/x")``
both returned "not found", so the prompt advertised load paths that could not
work, and a personal/org name collision was UNRESOLVABLE: the bare name was
refused as ambiguous and the only path the index offered did not load.

These tests drive the real renderer and the real tool, so they fail if either
side drifts — a heading that renders without a matching resolver, or a resolver
whose accepted form no longer matches what is rendered.
"""

import json
import re

import pytest

from agent import skill_utils as sku


def _mk_skill(root, rel, name=None, desc="d", body="# body\n"):
    d = root
    for part in rel.split("/"):
        d = d / part
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name or rel.split('/')[-1]}\ndescription: {desc}\n---\n{body}",
        encoding="utf-8",
    )
    return d


def _mark_active(skills, org_id):
    org_root = skills / sku.ORG_MIRROR_DIR_NAME
    org_root.mkdir(parents=True, exist_ok=True)
    (org_root / sku.ORG_ACTIVE_MARKER).write_text(org_id, encoding="utf-8")


@pytest.fixture
def skills_env(tmp_path, monkeypatch):
    """Isolated skills root wired into BOTH the index renderer and skill_view."""
    from agent import prompt_builder as pb
    from tools import skills_tool as st

    skills = tmp_path / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pb, "get_skills_dir", lambda: skills, raising=True)
    monkeypatch.setattr(pb, "get_all_skills_dirs", lambda: [skills], raising=True)
    monkeypatch.setattr(pb, "get_disabled_skill_names", lambda *a, **k: set())
    monkeypatch.setattr(pb, "_skills_prompt_snapshot_path", lambda: tmp_path / "snap.json")
    monkeypatch.setattr(st, "SKILLS_DIR", skills, raising=True)
    monkeypatch.setattr(sku, "get_external_skills_dirs", lambda: [], raising=True)
    monkeypatch.setattr(sku, "get_project_skills_dirs", lambda: [], raising=True)
    pb.clear_skills_system_prompt_cache()
    st._SKILLS_CACHE.clear()
    return skills, pb, st


def _headings(rendered):
    """Category headings exactly as the index renders them (two-space indent)."""
    block = rendered[rendered.find("<available_skills>"):rendered.find("</available_skills>")]
    return [m.group(1) for m in re.finditer(r"^  (\S[^:]*(?::[^:\s]+)?):", block, re.M)]


def _entries(rendered):
    """(category_heading, skill_name) for every rendered index entry."""
    block = rendered[rendered.find("<available_skills>"):rendered.find("</available_skills>")]
    out, current = [], None
    for line in block.split("\n"):
        if line.startswith("    - "):
            if current:
                out.append((current, line[6:].split(":")[0].strip()))
        elif line.startswith("  ") and line.strip():
            current = line.strip().rstrip(":")
            current = current.split(": ")[0].rstrip(":")
    return out


def _view(st, name):
    return json.loads(st.skill_view(name))


class TestEveryRenderedHeadingIsLoadable:
    """The contract, asserted against the renderer's own output."""

    def test_every_rendered_category_path_loads(self, skills_env):
        skills, pb, st = skills_env
        _mk_skill(skills, "root-level")                       # -> synthetic "general"
        _mk_skill(skills, "cli/documenso")                    # -> real "cli"
        _mk_skill(skills, "devops/nested/deep", name="deep")  # -> nested "devops/nested"
        _mk_skill(skills, f"{sku.ORG_MIRROR_DIR_NAME}/acme/shared-x", name="shared-x")
        _mark_active(skills, "acme")

        rendered = pb.build_skills_system_prompt(available_tools={"skill_view"})
        pairs = _entries(rendered)
        assert pairs, "index rendered no entries — test setup is wrong"
        # Both synthetic headings must actually be exercised by this test.
        assert "general" in {c for c, _ in pairs}
        assert "org:acme" in {c for c, _ in pairs}

        for category, name in pairs:
            result = _view(st, f"{category}/{name}")
            assert result.get("success") is True, (
                f"the index renders {category!r} as the category for {name!r} and tells the "
                f"agent to load skills by category path, but skill_view('{category}/{name}') "
                f"failed: {result.get('error')}"
            )


class TestSnapshotUpgrade:
    def test_old_category_snapshot_is_rebuilt_without_skill_edits(self, skills_env):
        skills, pb, st = skills_env
        _mk_skill(skills, "root-level")
        pb.build_skills_system_prompt(available_tools={"skill_view"})
        path = pb._skills_prompt_snapshot_path()
        snapshot = json.loads(path.read_text())
        manifest = snapshot["manifest"]
        snapshot["version"] = 2  # historical format that derived the wrong category
        for entry in snapshot["skills"]:
            entry["category"] = "root-level"
        path.write_text(json.dumps(snapshot))
        pb.clear_skills_system_prompt_cache()
        rendered = pb.build_skills_system_prompt(available_tools={"skill_view"})
        assert ("general", "root-level") in _entries(rendered)
        assert ("root-level", "root-level") not in _entries(rendered)
        assert json.loads(path.read_text())["manifest"] == manifest
        assert _view(st, "general/root-level")["success"]


class TestSyntheticGeneralHeading:
    def test_general_prefix_loads_root_level_skill(self, skills_env):
        skills, pb, st = skills_env
        _mk_skill(skills, "root-level")
        assert _view(st, "general/root-level").get("success") is True

    def test_general_prefix_matches_frontmatter_name(self, skills_env):
        """The index renders the FRONTMATTER name, so that is what must load."""
        skills, pb, st = skills_env
        _mk_skill(skills, "dir-name", name="frontmatter-name")
        assert _view(st, "general/frontmatter-name").get("success") is True

    def test_general_is_scoped_not_a_wildcard(self, skills_env):
        """A skill the index filed under a REAL category must not answer to
        'general/<name>' — the heading is a scope, not a synonym for 'anywhere'."""
        skills, pb, st = skills_env
        _mk_skill(skills, "cli/documenso")
        assert _view(st, "general/documenso").get("success") is False

    def test_real_general_directory_still_loads(self, skills_env):
        """An on-disk ``general/`` category keeps working (no regression)."""
        skills, pb, st = skills_env
        _mk_skill(skills, "general/real-cat-skill")
        result = _view(st, "general/real-cat-skill")
        assert result.get("success") is True
        assert result["path"].startswith("general/")

    @pytest.mark.parametrize(
        ("category", "directory", "frontmatter"),
        [
            ("cli", "renamed-cli", "cli-name"),
            ("devops/nested", "renamed-nested", "nested-name"),
        ],
    )
    def test_category_path_uses_frontmatter_name_without_leaking_scope(
        self, skills_env, category, directory, frontmatter
    ):
        skills, pb, st = skills_env
        _mk_skill(skills, f"{category}/{directory}", name=frontmatter)
        _mk_skill(skills, f"other/{directory}", name=frontmatter)
        result = _view(st, f"{category}/{frontmatter}")
        assert result.get("success") is True, result.get("error")
        assert f"{category}/{directory}" in result["_source_path"]
        assert _view(st, f"wrong/{frontmatter}").get("success") is False

    def test_synthetic_general_wins_only_for_root_skill_when_real_category_collides(
        self, skills_env
    ):
        skills, pb, st = skills_env
        _mk_skill(skills, "root-dir", name="same", body="root\n")
        _mk_skill(skills, "general/real-dir", name="same", body="real\n")
        assert _view(st, "general/same")["_source_path"].endswith("root-dir/SKILL.md")


class TestSyntheticOrgHeading:
    def test_org_heading_loads_org_skill(self, skills_env):
        skills, pb, st = skills_env
        _mk_skill(skills, f"{sku.ORG_MIRROR_DIR_NAME}/acme/shared-x", name="shared-x")
        _mark_active(skills, "acme")
        assert _view(st, "org:acme/shared-x").get("success") is True

    def test_org_heading_flattens_nested_categories(self, skills_env):
        """The heading is org-wide, so a nested org skill loads under it too."""
        skills, pb, st = skills_env
        _mk_skill(skills, f"{sku.ORG_MIRROR_DIR_NAME}/acme/devops/beta", name="beta")
        _mark_active(skills, "acme")
        assert _view(st, "org:acme/beta").get("success") is True
        assert _view(st, "org:acme/devops/beta").get("success") is True

    def test_inactive_org_mirror_stays_token_gated(self, skills_env):
        """The new load path must not become a bypass for the active-org token gate."""
        skills, pb, st = skills_env
        _mk_skill(skills, f"{sku.ORG_MIRROR_DIR_NAME}/acme/shared-x", name="shared-x")
        _mk_skill(skills, f"{sku.ORG_MIRROR_DIR_NAME}/stale/ghost", name="ghost")
        _mark_active(skills, "acme")
        assert _view(st, "org:stale/ghost").get("success") is False

    def test_no_marker_means_no_org_load(self, skills_env):
        skills, pb, st = skills_env
        _mk_skill(skills, f"{sku.ORG_MIRROR_DIR_NAME}/acme/shared-x", name="shared-x")
        assert _view(st, "org:acme/shared-x").get("success") is False

    def test_org_heading_does_not_reach_personal_skills(self, skills_env):
        skills, pb, st = skills_env
        _mk_skill(skills, "personal-only")
        _mk_skill(skills, f"{sku.ORG_MIRROR_DIR_NAME}/acme/shared-x", name="shared-x")
        _mark_active(skills, "acme")
        assert _view(st, "org:acme/personal-only").get("success") is False


class TestCollisionLabelAdviceActuallyWorks:
    """The collision label tells the agent to 'load via category path'.

    Both sides of the collision must therefore be loadable by the category path
    the index rendered for them — otherwise the org-side skill is unreachable.
    """

    def test_both_sides_of_a_personal_org_collision_load(self, skills_env):
        skills, pb, st = skills_env
        _mk_skill(skills, "cli/deploy", name="deploy", body="personal\n")
        _mk_skill(skills, f"{sku.ORG_MIRROR_DIR_NAME}/acme/deploy", name="deploy", body="org\n")
        _mark_active(skills, "acme")

        rendered = pb.build_skills_system_prompt(available_tools={"skill_view"})
        assert rendered.count("[name collision") == 2, "test needs a flagged collision"
        assert "load via category path" in rendered

        personal = _view(st, "cli/deploy")
        org = _view(st, "org:acme/deploy")
        assert personal.get("success") is True, personal.get("error")
        assert org.get("success") is True, org.get("error")
        # The two paths must resolve to DIFFERENT skills — a category path that
        # silently served the same file would defeat the disambiguation.
        assert personal["_source_path"] != org["_source_path"]

    def test_scoped_paths_disambiguate_without_changing_bare_name_behavior(self, skills_env):
        """The scoped paths are ADDITIVE: whatever the bare name did before, it still does.

        This pins the contract that matters here — a personal/org name clash is
        reachable by the two paths the index renders — without freezing the bare
        name's own resolution policy, which this change does not touch.
        """
        skills, pb, st = skills_env
        _mk_skill(skills, "cli/deploy", name="deploy")
        _mk_skill(skills, f"{sku.ORG_MIRROR_DIR_NAME}/acme/deploy", name="deploy")
        _mark_active(skills, "acme")
        personal = _view(st, "cli/deploy")
        org = _view(st, "org:acme/deploy")
        assert personal.get("success") is True, personal.get("error")
        assert org.get("success") is True, org.get("error")
        assert personal["_source_path"] != org["_source_path"]


class TestHeadingPathsAreNotAnEscapeHatch:
    @pytest.mark.parametrize("name", [
        "general/../cli/documenso",
        "org:acme/../stale/ghost",
        "general/../../etc/passwd",
    ])
    def test_traversal_is_refused(self, skills_env, name):
        skills, pb, st = skills_env
        _mk_skill(skills, "cli/documenso")
        _mk_skill(skills, f"{sku.ORG_MIRROR_DIR_NAME}/stale/ghost", name="ghost")
        _mark_active(skills, "acme")
        assert _view(st, name).get("success") is False

    def test_invalid_org_id_is_not_a_heading(self, skills_env):
        skills, pb, st = skills_env
        _mk_skill(skills, f"{sku.ORG_MIRROR_DIR_NAME}/acme/shared-x", name="shared-x")
        _mark_active(skills, "acme")
        # An org id must be a valid namespace ([a-zA-Z0-9_-]+); anything that could
        # smuggle a separator or traversal into the joined path is not a heading.
        for bad in ("org:../x", "org:a b/x", "org:.hidden/x", "org:/x"):
            assert sku.parse_index_heading_path(bad) is None, bad

    def test_nested_org_category_path_is_still_a_heading(self, skills_env):
        """`org:<id>/<category>/<skill>` is legitimate — the id is only the first segment."""
        assert sku.parse_index_heading_path("org:acme/devops/beta") == (
            f"{sku.ORG_MIRROR_DIR_NAME}/acme", "devops/beta", True)

    def test_missing_skills_still_fail_cleanly(self, skills_env):
        skills, pb, st = skills_env
        _mark_active(skills, "acme")
        for name in ("general/nope", "org:acme/nope"):
            assert _view(st, name).get("success") is False

    def test_package_owned_document_cannot_shadow_a_real_skill(self, skills_env):
        skills, pb, st = skills_env
        package = _mk_skill(skills, "cli/owner", name="owner")
        (package / "doc.md").write_text("not a standalone skill\n", encoding="utf-8")
        result = _view(st, "doc")
        assert result.get("success") is False


class TestParseIndexHeadingPath:
    """Unit contract for the shared parser."""

    def test_general(self):
        assert sku.parse_index_heading_path("general/x") == ("", "x", False)

    def test_org(self):
        assert sku.parse_index_heading_path("org:acme/x") == (
            f"{sku.ORG_MIRROR_DIR_NAME}/acme", "x", True)

    @pytest.mark.parametrize("name", [
        "cli/documenso",    # real category — untouched
        "documenso",        # bare name
        "general",          # heading with no skill
        "org:acme",         # org heading with no skill
        "plugin:skill",     # plugin qualified name
        "",
    ])
    def test_non_heading_inputs_return_none(self, name):
        assert sku.parse_index_heading_path(name) is None
