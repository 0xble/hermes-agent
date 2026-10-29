"""Required shared-state maintenance in an immutable update candidate."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import update_cmd_maint


@pytest.fixture
def homes(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    sibling = root / "profiles" / "sibling"
    sibling.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root, sibling


def test_required_maintenance_seeds_catalog_skills_and_migrates_each_home(homes, monkeypatch):
    from hermes_cli import config, model_catalog

    root, sibling = homes
    for home in homes:
        (home / "config.yaml").write_text("model:\n  default: test-model\n", encoding="utf-8")
    assert update_cmd_maint.strict_immutable_maintenance(
        Path(__file__).parents[2]
    ) is True
    for home in homes:
        assert (home / "skills" / ".bundled-manifest").exists() or any(
            (home / "skills").rglob("SKILL.md")
        )
    for home in homes:
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        token = set_hermes_home_override(home)
        try:
            current, latest = config.check_config_version(raise_on_parse_error=True)
            assert current == latest
            assert config.read_raw_config()["model"]["default"] == "test-model"
        finally:
            reset_hermes_home_override(token)
    assert (root / "cache" / "model_catalog.json").is_file()
    assert model_catalog._read_disk_cache()[0] is not None


@pytest.mark.parametrize("failure", ["catalog", "skills", "skills-update"])
def test_required_maintenance_blocks_real_filesystem_failures(homes, tmp_path, monkeypatch, failure):
    from hermes_cli.profiles import seed_profile_skills

    root, _sibling = homes
    for home in homes:
        (home / "config.yaml").write_text("model:\n  default: test-model\n", encoding="utf-8")

    if failure == "catalog":
        # The cache parent is a file, so the atomic catalog write has a real
        # filesystem failure rather than a mocked helper result.
        (root / "cache").write_text("not-a-directory", encoding="utf-8")
    elif failure == "skills":
        # Per-skill copy errors must survive the profile helper's subprocess.
        (root / "skills").mkdir()
        (root / "skills" / "apple").write_text("not-a-directory", encoding="utf-8")
    else:
        # Seed a pristine skill, change the bundle, then obstruct its backup
        # path with a file: replacing the real destination directory must fail.
        bundled = tmp_path / "bundled"
        skill = bundled / "category" / "example"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: example\n---\nold\n", encoding="utf-8")
        monkeypatch.setenv("HERMES_BUNDLED_SKILLS", str(bundled))
        seeded = seed_profile_skills(root, quiet=True)
        assert seeded is not None and seeded["copied"] == ["example"]
        (skill / "SKILL.md").write_text("---\nname: example\n---\nnew\n", encoding="utf-8")
        (root / "skills" / "category" / "example.bak").write_text("not-a-directory", encoding="utf-8")

    reason = "catalog" if failure == "catalog" else "bundled skills"
    with pytest.raises(RuntimeError, match=reason):
        update_cmd_maint.strict_immutable_maintenance(Path(__file__).parents[2])
    if failure == "catalog":
        assert not (root / "cache" / "model_catalog.json").exists()
    elif failure == "skills":
        assert not (root / "skills" / "apple" / "apple-notes" / "SKILL.md").exists()
    else:
        assert (root / "skills" / "category" / "example" / "SKILL.md").read_text().endswith("old\n")


@pytest.mark.parametrize("failure", ["catalog", "skills", "config", "migration"])
def test_required_maintenance_propagates_failure(homes, monkeypatch, failure):
    from hermes_cli import config, model_catalog, profiles

    root, sibling = homes
    original = "model: [invalid\n" if failure == "config" else "model:\n  default: test-model\n"
    for home in homes:
        (home / "config.yaml").write_text(original, encoding="utf-8")
    monkeypatch.setattr(profiles, "list_profiles", lambda **kw: [SimpleNamespace(path=root, name="default")])
    monkeypatch.setattr(model_catalog, "seed_cache_from_checkout", lambda root, **kw: failure != "catalog")
    monkeypatch.setattr(profiles, "seed_profile_skills", lambda *a, **kw: None if failure == "skills" else {"copied": [], "total_bundled": 1})
    if failure == "migration":
        def fail_migration(*, interactive, quiet):
            raise RuntimeError("forced migration failure")
        monkeypatch.setattr(config, "migrate_config", fail_migration)
    with pytest.raises(Exception):
        update_cmd_maint.strict_immutable_maintenance(Path(__file__).parents[2])
    assert (root / "config.yaml").read_text(encoding="utf-8") == original
