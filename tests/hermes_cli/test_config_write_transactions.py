"""Managed writes validate the whole candidate and never silently replay stale snapshots."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from hermes_cli import config
from hermes_cli.config_errors import ConfigValidationError
from hermes_cli.config_backups import backup_config, list_config_backups, load_newest_good_backup
from utils import atomic_write_text, atomic_yaml_write, atomic_roundtrip_yaml_update


VALID = """# operator comment
model_presets:
  worker:
    provider: openrouter
    model: vendor/worker
    reasoning_effort: high
delegation:
  model_preset: worker
  max_iterations: 12
"""
INVALID = VALID.replace("  model_preset: worker", "  model_preset: worker\n  provider: openrouter")


@pytest.fixture
def authored(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text(VALID, encoding="utf-8")
    return path


@pytest.mark.parametrize("writer", ["force", "unset", "save", "atomic", "text", "leaf"])
def test_all_managed_writers_reject_invalid_complete_candidate(authored, writer):
    before = authored.read_bytes()
    with pytest.raises((ConfigValidationError, SystemExit)):
        if writer == "force":
            config.set_config_value("mcp_servers", "not-a-mapping", force=True)
        elif writer == "unset":
            config.unset_config_value("model_presets.worker")
        elif writer == "save":
            config.save_config({"mcp_servers": "not-a-mapping"})
        elif writer == "atomic":
            atomic_yaml_write(authored, {"delegation": {"model_preset": "missing"}})
        elif writer == "text":
            atomic_write_text(authored, INVALID)
        else:
            atomic_roundtrip_yaml_update(authored, "delegation.provider", "openrouter")
    assert authored.read_bytes() == before


@pytest.mark.parametrize("reader", ["raw", "expanded", "copied"])
def test_stale_read_cannot_undo_newer_edit(authored, reader):
    snapshot = config.load_config() if reader == "expanded" else config.read_user_config_raw()
    if reader == "copied":
        snapshot = snapshot.copy()
    config.set_config_value("delegation.max_iterations", "13")
    latest = authored.read_bytes()
    snapshot["delegation"]["max_iterations"] = 14
    with pytest.raises(ValueError, match="changed.*read|read.*again"):
        config.save_config(snapshot, merge_existing=False)
    assert authored.read_bytes() == latest


def test_two_processes_cannot_commit_the_same_revision(authored):
    # Both read before either writes. No sleep or reliance on process startup jitter.
    code = """
import sys
from hermes_cli.config import read_user_config_raw, atomic_config_write, get_config_path
snapshot = read_user_config_raw()
print('read', flush=True)
sys.stdin.readline()
snapshot['delegation']['max_iterations'] = int(sys.argv[1])
try:
    atomic_config_write(get_config_path(), snapshot)
except ValueError as exc:
    print('conflict: ' + str(exc), flush=True)
else:
    print('saved', flush=True)
"""
    env = {**os.environ, "HERMES_HOME": str(authored.parent)}
    children = [subprocess.Popen([sys.executable, "-u", "-c", code, str(n)],
                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True) for n in (20, 30)]
    try:
        for child in children:
            assert child.stdout.readline().strip() == "read"
        children[0].stdin.write("write\n")
        children[0].stdin.flush()
        assert children[0].stdout.readline().strip() == "saved"
        first = authored.read_bytes()
        children[1].stdin.write("write\n")
        children[1].stdin.flush()
        assert children[1].stdout.readline().startswith("conflict:")
        assert authored.read_bytes() == first
        for child in children:
            _, err = child.communicate(timeout=20)
            assert child.returncode == 0, err
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


def test_external_edit_during_serialization_is_not_overwritten(authored, monkeypatch):
    import utils
    original = utils.os.fsync
    replacement = VALID.replace("max_iterations: 12", "max_iterations: 19").encode()

    def edit_while_flushing(fd):
        authored.write_bytes(replacement)
        return original(fd)

    monkeypatch.setattr(utils.os, "fsync", edit_while_flushing)
    with pytest.raises(ValueError, match="changed.*read|read.*again"):
        config.atomic_config_replace(authored, {"delegation": {"max_iterations": 15}})
    assert authored.read_bytes() == replacement


@pytest.mark.parametrize("reader", ["load", "effective", "backup"])
def test_invalid_config_never_becomes_good_snapshot(authored, reader):
    authored.write_text(INVALID, encoding="utf-8")
    if reader == "load":
        with pytest.raises(ConfigValidationError):
            config.load_config()
    elif reader == "effective":
        from hermes_cli.config_effective import load_user_config_effective
        with pytest.raises(ConfigValidationError):
            load_user_config_effective(fail_closed=True)
    else:
        assert backup_config(authored, "good") is None
    assert not list_config_backups(authored, "good")


def test_recovery_skips_semantically_invalid_good_snapshots(authored):
    root = authored.parent / "backups" / "config"
    root.mkdir(parents=True)
    (root / "config.yaml.good.20261027-010000").write_text(VALID, encoding="utf-8")
    (root / "config.yaml.good.20261027-020000").write_text(INVALID, encoding="utf-8")
    (root / "config.yaml.good.20261027-030000").write_text("mcp_servers: broken\n", encoding="utf-8")
    assert load_newest_good_backup(authored)["delegation"] == {"model_preset": "worker", "max_iterations": 12}
    authored.write_text("model: [unterminated\n", encoding="utf-8")
    recovered = config.load_config()
    assert recovered["delegation"]["model"] == "vendor/worker"
    assert "unterminated" in authored.read_text(encoding="utf-8")


def test_raw_get_is_authored_and_expanded_flag_keeps_resolved_view(authored, capsys):
    config.get_config_value("delegation", as_json=True, raw=True)
    raw = json.loads(capsys.readouterr().out)
    assert raw == {"model_preset": "worker", "max_iterations": 12}
    config.get_config_value("delegation", as_json=True, raw=True, expanded=True)
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["model"] == "vendor/worker"
    assert "model_preset" not in resolved
    # Reading authored data must still work while a manually edited route is invalid.
    authored.write_text(INVALID, encoding="utf-8")
    config.get_config_value("delegation", as_json=True, raw=True)
    assert json.loads(capsys.readouterr().out)["model_preset"] == "worker"


def test_saved_object_can_be_saved_again_but_independent_stale_copy_cannot(authored):
    import copy
    loaded = config.load_config()
    old_copy = copy.deepcopy(loaded)
    loaded["delegation"]["max_iterations"] = 13
    config.save_config(loaded)
    loaded["delegation"]["max_iterations"] = 14
    config.save_config(loaded)
    latest = authored.read_bytes()
    with pytest.raises(ValueError, match="changed.*read|read.*again"):
        config.save_config(old_copy)
    assert authored.read_bytes() == latest


def test_cached_raw_and_missing_file_snapshots_are_versioned(authored):
    cached = config.read_raw_config()
    config.set_config_value("delegation.max_iterations", "13")
    latest = authored.read_bytes()
    with pytest.raises(ValueError, match="changed.*read|read.*again"):
        atomic_yaml_write(authored, cached)
    assert authored.read_bytes() == latest
    authored.unlink()
    missing = config.read_raw_config()
    config.atomic_config_write(authored, {"delegation": {"max_iterations": 14}})
    latest = authored.read_bytes()
    with pytest.raises(ValueError, match="changed.*read|read.*again"):
        config.save_config(missing)
    assert authored.read_bytes() == latest


def test_effective_fail_open_read_never_retains_invalid_snapshot(authored):
    from hermes_cli import config_effective
    authored.write_text(INVALID, encoding="utf-8")
    assert config_effective.load_user_config_effective()["delegation"]["model_preset"] == "worker"
    assert str(authored) not in config_effective._LAST_GOOD_USER_RAW
    assert not list_config_backups(authored, "good")


def test_gateway_refresh_does_not_swallow_semantic_admission_errors(authored, monkeypatch):
    from gateway.run import GatewayRunner
    from types import SimpleNamespace
    monkeypatch.setattr("gateway.run._gateway_config_home", lambda: authored.parent)
    authored.write_text("mcp_servers: not-a-mapping\n", encoding="utf-8")
    runner = SimpleNamespace(_fallback_model=None)
    with pytest.raises(ConfigValidationError, match="mcp_servers"):
        GatewayRunner._refresh_fallback_model.__get__(runner)()


def test_raw_cli_preserves_env_templates_and_expanded_cli_resolves_them(authored):
    with authored.open("a", encoding="utf-8") as stream:
        stream.write("operator_value: ${GUARD_TEST_VALUE}\n")
    env = {**os.environ, "HERMES_HOME": str(authored.parent), "GUARD_TEST_VALUE": "resolved-fixture"}
    command = [sys.executable, "-m", "hermes_cli.main", "config", "get", "operator_value", "--raw", "--json"]
    raw = subprocess.run(command, env=env, text=True, capture_output=True, timeout=30, check=True)
    expanded = subprocess.run([*command, "--expanded"], env=env, text=True, capture_output=True, timeout=30, check=True)
    assert json.loads(raw.stdout) == "${GUARD_TEST_VALUE}"
    assert json.loads(expanded.stdout) == "resolved-fixture"


@pytest.mark.parametrize("invalid", ["[]\n", "false\n", "mcp_servers: broken\n"])
def test_invalid_root_or_structure_never_replaces_in_memory_good(authored, invalid):
    assert config.load_config()["delegation"]["model"] == "vendor/worker"
    before = [p.read_bytes() for p in list_config_backups(authored, "good")]
    authored.write_text(invalid, encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        config.load_config()
    assert [p.read_bytes() for p in list_config_backups(authored, "good")] == before
    authored.write_text("broken: [unterminated\n", encoding="utf-8")
    assert config.load_config()["delegation"]["model"] == "vendor/worker"


def test_final_serialized_bytes_are_validated_before_creating_file(authored):
    authored.unlink()
    # An empty mapping emits '{}', so appending a block mapping would test YAML
    # syntax rejection instead of semantic admission of the final serialized bytes.
    with pytest.raises(ConfigValidationError, match="unknown preset"):
        config.atomic_config_write(authored, {"operator_marker": "fixture"},
                                   extra_content_on_create="delegation:\n  model_preset: missing\n")
    assert not authored.exists()


def test_malformed_serialized_bytes_never_create_config(authored):
    import hermes_yaml as yaml

    authored.unlink()
    with pytest.raises(yaml.YAMLError):
        config.atomic_config_write(authored, {}, extra_content_on_create="delegation:\n  model_preset: missing\n")
    assert not authored.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX advisory-lock probe")
def test_atomic_writer_holds_cross_process_lock(authored):
    import fcntl
    code = """
import sys
from hermes_cli.config import get_config_path
from utils import _atomic_write

def emit(stream):
    stream.write('operator_marker: locked\\n')
    print('locked', flush=True)
    sys.stdin.readline()

_atomic_write(get_config_path(), emit, prefix='.lock-probe_')
"""
    child = subprocess.Popen([sys.executable, "-u", "-c", code], env={**os.environ, "HERMES_HOME": str(authored.parent)},
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        with authored.with_name("config.yaml.lock").open("a+") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _, err = child.communicate("release\n", timeout=20)
        assert child.returncode == 0, err
        assert config.read_user_config_raw()["operator_marker"] == "locked"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_fresh_snapshot_serializes_without_revision_metadata(authored):
    raw = config.read_user_config_raw()
    atomic_yaml_write(authored, raw)
    saved = config.read_user_config_raw()
    assert saved == raw
    assert "source_revision" not in authored.read_text(encoding="utf-8")
    assert "ConfigSnapshot" not in authored.read_text(encoding="utf-8")


def test_shared_resolution_keeps_legacy_root_turns_with_null_nested_value(authored):
    authored.write_text("max_turns: 42\nagent:\n  max_turns: null\n", encoding="utf-8")
    assert config.load_config()["agent"]["max_turns"] == 42
