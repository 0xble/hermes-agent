"""#111105: an atomic config.yaml replacement that keeps mtime and size
must still invalidate the load_config() cache, while an unchanged
file keeps serving the cached object."""
import os
import pytest
from unittest.mock import patch

from hermes_cli import config as config_mod


def _replace_pinning_mtime(path, content: str) -> None:
    before = path.stat()
    other = path.with_name("other.yaml")
    other.write_text(content, encoding="utf-8")
    # Replace the inode, without relying on a ctime clock tick (#112042).
    os.replace(other, path)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert after.st_ino != before.st_ino


def test_load_config_sees_replacement_with_pinned_mtime_and_size(tmp_path):
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        config_mod._LOAD_CONFIG_CACHE.clear()
        config_mod._RAW_CONFIG_CACHE.clear()
        cfg = tmp_path / "config.yaml"
        cfg.write_text("model:\n  default: aaaa-route\n", encoding="utf-8")
        first = config_mod._load_config_impl(want_deepcopy=False)
        assert config_mod._load_config_impl(want_deepcopy=False) is first  # unchanged file: cache hit
        _replace_pinning_mtime(cfg, "model:\n  default: bbbb-route\n")
        assert config_mod.load_config()["model"]["default"] == "bbbb-route"


def test_load_config_readonly_reuses_raw_parse_across_secret_scopes(tmp_path, monkeypatch):
    from agent import secret_scope

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "providers:\n  demo:\n    api_key: ${CACHE_SCOPE_KEY}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()
    calls = 0
    original = config_mod.read_config_doc

    def counted(path):
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(config_mod, "read_config_doc", counted)
    try:
        for value in ("scope-a", "scope-b", "scope-a", "scope-b"):
            token = secret_scope.set_secret_scope({"CACHE_SCOPE_KEY": value})
            try:
                loaded = config_mod.load_config_readonly()
                assert loaded["providers"]["demo"]["api_key"] == value
            finally:
                secret_scope.reset_secret_scope(token)
    finally:
        config_mod._LOAD_CONFIG_CACHE.clear()
        config_mod._RAW_CONFIG_CACHE.clear()
    assert calls == 1


def test_read_raw_and_load_config_readonly_share_raw_parse_cache(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("model:\n  default: shared-route\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()
    calls = 0
    original = config_mod.read_config_doc

    def counted(path):
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(config_mod, "read_config_doc", counted)
    config_mod.read_raw_config()
    assert config_mod.load_config_readonly()["model"]["default"] == "shared-route"
    config_mod.read_raw_config()
    config_mod.load_config_readonly()
    assert calls == 1




@pytest.mark.parametrize("yaml_root", ["- not-a-mapping\n", "not-a-mapping\n", "0\n"])
def test_load_config_non_mapping_root_is_failed_config_on_miss_and_hit(tmp_path, monkeypatch, yaml_root):
    from hermes_cli.config_read_errors import FailedConfigRead

    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml_root, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()

    first = config_mod.load_config()
    second = config_mod.load_config()
    assert isinstance(first, FailedConfigRead)
    assert isinstance(second, FailedConfigRead)
    assert not isinstance(first, list)
    assert not isinstance(second, list)
    assert config_mod._RAW_CONFIG_CACHE.get(str(config_file)) is None


def test_load_config_readonly_reparses_changed_file(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("model:\n  default: aaaa-route\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()
    calls = 0
    original = config_mod.read_config_doc

    def counted(path):
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(config_mod, "read_config_doc", counted)
    assert config_mod.load_config_readonly()["model"]["default"] == "aaaa-route"
    _replace_pinning_mtime(config_file, "model:\n  default: bbbb-route\n")
    assert config_mod.load_config_readonly()["model"]["default"] == "bbbb-route"
    assert calls == 2
