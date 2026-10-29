"""#111105: an atomic config.yaml replacement that keeps mtime and size
must still invalidate the load_config() cache, while an unchanged
file keeps serving the cached object."""
import os
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
