"""Generic atomic writers only apply Hermes admission to Hermes-owned documents."""
import os
import subprocess
import sys

import pytest

from hermes_cli import config
from hermes_cli.config_errors import ConfigValidationError
from utils import atomic_write_text


@pytest.mark.parametrize("writer", ["atomic_json_write", "atomic_write_text"])
def test_non_config_atomic_write_does_not_import_config(tmp_path, writer):
    code = f"""
import sys
from pathlib import Path
from utils import {writer}
assert 'hermes_cli.config' not in sys.modules
path = Path(sys.argv[1])
{writer}(path, {{'fixture': True}} if '{writer}' == 'atomic_json_write' else 'fixture')
assert path.is_file()
assert 'hermes_cli.config' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "state.json")],
        env={**os.environ, "HERMES_HOME": str(tmp_path)},
        text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_skill_template_config_is_not_a_hermes_document(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    template = tmp_path / "skills" / "sample" / "templates" / "config.yaml"
    template.parent.mkdir(parents=True)
    for text in ("- application-list\n", "port: {{ port }}\n", "timezone: application-specific\n"):
        atomic_write_text(template, text)
        assert template.read_text(encoding="utf-8") == text
    assert not template.with_name("config.yaml.lock").exists()


@pytest.mark.parametrize("destination", ["active", "profile", "explicit", "explicit-other-name"])
def test_owned_or_explicit_config_transactions_still_validate_final_bytes(tmp_path, monkeypatch, destination):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = {"active": tmp_path / "config.yaml",
            "profile": tmp_path / "profiles" / "other" / "config.yaml",
            "explicit": tmp_path / "staging" / "config.yaml",
            "explicit-other-name": tmp_path / "staging" / "settings.yml"}[destination]
    path.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ConfigValidationError):
        if destination.startswith("explicit"):
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
