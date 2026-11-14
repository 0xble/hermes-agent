"""A private interval never captures another session's disposable or durable files."""
from pathlib import Path
import os

import pytest

from agent.incognito import incognito_scope, register_session
from agent import incognito_private_dir as private_dirs
from hermes_constants import apply_scratch_tmp_env, get_hermes_dir
from plugins.plugin_storage import plugin_data_dir

_default_root = private_dirs._configured_root  # captured before any fixture replaces it


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(private_dirs, "_configured_root", lambda: tmp_path / "private")
    yield home
    for identity in ("private-a", "private-b"):
        register_session(identity, False)
        private_dirs.discard_private_dir(identity)
    private_dirs.sweep_private_dirs()


def test_default_parent_is_scoped_to_one_hermes_home(tmp_path, monkeypatch):
    """One home's startup sweep must never reach another home's live interval."""
    parents = []
    for name in ("home-a", "home-b", "home-a"):
        home = tmp_path / name
        home.mkdir(exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        parent = _default_root()
        assert home not in parent.parents and parent != home
        parents.append(parent)
    assert parents[0] == parents[2] != parents[1]
    assert parents[1] not in parents[0].parents and parents[0] not in parents[1].parents


def test_unparseable_config_uses_default_parent(tmp_path, monkeypatch):
    home = tmp_path / "broken-home"
    home.mkdir()
    (home / "config.yaml").write_text("incognito: [broken\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(private_dirs, "_configured_root", _default_root)
    assert private_dirs._configured_root() == _default_root()


def test_non_mapping_incognito_config_uses_default_parent(tmp_path, monkeypatch):
    home = tmp_path / "invalid-home"
    home.mkdir()
    (home / "config.yaml").write_text("incognito: true\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(private_dirs, "_configured_root", _default_root)
    assert private_dirs._configured_root() == _default_root()


def test_private_subprocess_tmpdir_overrides_launchd_tmp(tmp_path, isolated_paths):
    register_session("private-a")
    private_env = {"TMPDIR": "/var/folders/x", "HERMES_HOME": str(isolated_paths)}
    with incognito_scope(True, "private-a"):
        assert apply_scratch_tmp_env(private_env) is True
        private_root = private_dirs.private_path_for_current_session()
        assert private_root is not None
        assert Path(private_env["TMPDIR"]).is_relative_to(private_root / "cache" / "scratch")

    public_env = {"TMPDIR": "/var/folders/x", "HERMES_HOME": str(isolated_paths)}
    with incognito_scope(False, "public-b"):
        assert apply_scratch_tmp_env(public_env) is False
    assert public_env["TMPDIR"] == "/var/folders/x"


def test_private_cache_resolution_fails_closed_without_bound_identity():
    with incognito_scope(True, "unregistered-private"):
        with pytest.raises(RuntimeError, match="No private interval"):
            get_hermes_dir("cache/audio", "audio_cache")


def test_public_voice_cache_and_plugin_data_survive_private_off(isolated_paths):
    register_session("private-a")
    with incognito_scope(True, "private-a"):
        private_root = private_dirs.private_path_for_current_session()
        assert private_root is not None
        private_file = get_hermes_dir("cache/audio", "audio_cache") / "private.ogg"
        private_file.parent.mkdir(parents=True, exist_ok=True)
        private_file.write_bytes(b"private")
    with incognito_scope(False, "public-b"):
        public_audio = get_hermes_dir("cache/audio", "audio_cache") / "public.ogg"
        public_audio.parent.mkdir(parents=True, exist_ok=True)
        public_audio.write_bytes(b"public voice")
        public_data = plugin_data_dir("public-test") / "data.txt"
        public_data.write_text("public plugin")
        assert not public_audio.is_relative_to(private_root)
        assert public_data.is_relative_to(isolated_paths / "plugin-data")
    register_session("private-a", False)
    private_dirs.discard_private_dir("private-a")
    assert not private_root.exists()
    assert public_audio.read_bytes() == b"public voice"
    assert public_data.read_text() == "public plugin"


def test_two_private_topics_have_separate_directories():
    register_session("private-a")
    register_session("private-b")
    paths = []
    for identity in ("private-a", "private-b"):
        with incognito_scope(True, identity):
            paths.append(private_dirs.private_path_for_current_session())
    assert paths[0] != paths[1]
    assert all(path.is_dir() for path in paths)


def test_unattributed_public_work_does_not_share_private_cleanup(monkeypatch):
    from agent import incognito
    monkeypatch.setattr(incognito, "_sessions", set())
    register_session("private-a")
    with incognito_scope(True, "private-a"):
        private_root = private_dirs.private_path_for_current_session()
    with incognito_scope(False, "public-b"):
        assert private_dirs.disposable_path_for_current_session() is None
    public_file = private_dirs.disposable_path_for_current_session()
    assert public_file is None
    register_session("private-a", False)
    private_dirs.discard_private_dir("private-a")
    assert private_root is not None and not private_root.exists()


def test_private_delegated_child_uses_parent_interval():
    from agent import incognito
    from agent.delegation_context import delegated_child_context

    register_session("private-a")
    with incognito_scope(True, "private-a"):
        parent_root = private_dirs.private_path_for_current_session()
    assert parent_root is not None
    with delegated_child_context("child-session", parent_session_id="private-a"):
        with incognito_scope(True, "child-session"):
            child_root = private_dirs.private_dir_for_session("child-session")
            assert child_root == parent_root
            assert not incognito.is_registered("child-session")
            child_env = {"TMPDIR": "/tmp/public", "HERMES_HOME": str(parent_root)}
            assert apply_scratch_tmp_env(child_env)
            child_file = Path(child_env["TMPDIR"]) / "child.txt"
            child_file.write_text("private child")
    register_session("child-session", False)
    private_dirs.release_private_session("child-session")
    assert child_file.exists()
    register_session("private-a", False)
    private_dirs.discard_private_dir("private-a")
    assert not parent_root.exists()
    with incognito_scope(False, "public-after"):
        assert private_dirs.disposable_path_for_current_session() is None


@pytest.mark.parametrize("root_registered", [True, False])
def test_nested_private_delegated_child_resolves_only_a_live_root(root_registered):
    from agent.delegation_context import delegated_child_context

    root_id = "private-a"
    if root_registered:
        register_session(root_id)
    root = private_dirs.private_dir_for_session(root_id)
    try:
        with delegated_child_context("grandchild-session", parent_session_id="child-session", root_session_id=root_id):
            with incognito_scope(True, "grandchild-session"):
                assert private_dirs.private_dir_for_session("grandchild-session") == root
                if root_registered:
                    assert root is not None
                else:
                    assert root is None
    finally:
        register_session(root_id, False)
        private_dirs.discard_private_dir(root_id)
    if root is not None:
        assert not root.exists()


def test_discard_shuts_down_kernels_for_session_and_aliases_before_removal(monkeypatch):
    calls = []
    removed = []

    def shutdown(owner):
        calls.append(owner)
        assert removed == []

    monkeypatch.setattr("tools.code_kernel.shutdown_kernels_for_owner", shutdown, raising=False)
    register_session("session-id")
    with incognito_scope(True, "session-id"):
        interval = private_dirs.private_path_for_current_session()
    assert interval is not None
    private_dirs.bind_private_session("topic-key", interval)
    real_remove = private_dirs._remove

    def observe(path):
        removed.append(path)
        real_remove(path)

    monkeypatch.setattr(private_dirs, "_remove", observe)
    register_session("session-id", False)
    private_dirs.discard_private_dir("session-id")
    assert calls == ["session-id", "topic-key"]
    assert removed
    register_session("topic-key", False)


def test_cleanup_failure_is_logged_without_raising(caplog, monkeypatch):
    register_session("private-a")
    with incognito_scope(True, "private-a"):
        private_root = private_dirs.private_path_for_current_session()
    assert private_root is not None

    def refuse(path):
        raise OSError("cleanup refused")

    monkeypatch.setattr(private_dirs.shutil, "rmtree", refuse)
    with caplog.at_level("ERROR"):
        private_dirs.discard_private_dir("private-a")
    register_session("private-a", False)
    assert any("private cleanup" in record.message for record in caplog.records)
    private_dirs._remove(private_root)


def test_last_interval_exit_removes_a_directory_no_session_owns():
    """A directory left bound to an identity that is no longer private must not outlive the last interval."""
    register_session("private-a")
    with incognito_scope(True, "private-a"):
        private_root = private_dirs.private_path_for_current_session()
    stray = private_dirs._new_dir()
    private_dirs.bind_private_session("no-longer-private", stray)
    leftover = stray / "cache" / "audio" / "stray.ogg"
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"stray-private")
    register_session("private-a", False)
    private_dirs.discard_private_dir("private-a")
    assert not private_root.exists()
    assert not stray.exists()


def test_sweep_skip_live_owner_keeps_live_foreign_and_removes_stale(tmp_path, monkeypatch):
    root = tmp_path / "private"
    monkeypatch.setattr(private_dirs, "_configured_root", lambda: root)
    live = private_dirs._new_dir()
    (live / ".owner").write_text(f"{os.getppid()}\n", encoding="ascii")
    dead = private_dirs._new_dir()
    (dead / ".owner").write_text("999999999\n", encoding="ascii")
    missing = private_dirs._new_dir()
    (missing / ".owner").unlink()
    (root / "unattributed").mkdir()
    assert private_dirs.sweep_private_dirs(skip_live_owners=True) == 3
    assert live.exists()
    assert not dead.exists()
    assert not missing.exists()
    assert not (root / "unattributed").exists()
    assert private_dirs.sweep_private_dirs() == 1
    assert not live.exists()


def test_new_dir_records_only_owner_pid():
    interval = private_dirs._new_dir()
    marker = interval / ".owner"
    assert marker.read_text(encoding="ascii") == f"{os.getpid()}\n"
    assert marker.stat().st_mode & 0o777 == 0o600


def test_discard_owned_private_dirs_removes_unmapped_owned_interval():
    interval = private_dirs._new_dir()
    assert interval.exists()
    assert private_dirs.discard_owned_private_dirs() == 1
    assert not interval.exists()
