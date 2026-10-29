"""Generic atomic writers only apply Hermes admission to Hermes-owned documents."""
import pytest

from hermes_cli import config
from hermes_cli.config_errors import ConfigValidationError
from utils import atomic_write_text


def test_skill_template_config_is_not_a_hermes_document(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    template = tmp_path / "skills" / "sample" / "templates" / "config.yaml"
    template.parent.mkdir(parents=True)
    for text in ("- application-list\n", "port: {{ port }}\n", "timezone: application-specific\n"):
        atomic_write_text(template, text)
        assert template.read_text(encoding="utf-8") == text
    assert not template.with_name("config.yaml.lock").exists()


@pytest.mark.parametrize("destination", ["active", "profile", "explicit"])
def test_owned_or_explicit_config_transactions_still_validate_final_bytes(tmp_path, monkeypatch, destination):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = {"active": tmp_path / "config.yaml",
            "profile": tmp_path / "profiles" / "other" / "config.yaml",
            "explicit": tmp_path / "staging" / "config.yaml"}[destination]
    path.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ConfigValidationError):
        if destination == "explicit":
            config.atomic_config_write(path, {"marker": "fixture"}, extra_content_on_create="timezone: NOT/A_ZONE\n")
        else:
            atomic_write_text(path, "timezone: NOT/A_ZONE\n")
    assert not path.exists()


def test_symlinked_named_profile_retains_owned_document_admission(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    target = tmp_path / "operator-owned-profile"
    target.mkdir()
    (profiles / "other").symlink_to(target, target_is_directory=True)
    path = profiles / "other" / "config.yaml"
    path.write_text("timezone: UTC\n", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ConfigValidationError):
        atomic_write_text(path, "timezone: NOT/A_ZONE\n")
    assert path.read_bytes() == before
