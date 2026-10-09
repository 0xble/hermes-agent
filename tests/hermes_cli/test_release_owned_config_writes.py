"""Only the live release, or the updater, may write a release-managed home's config.yaml.

A dev build run against a home on immutable releases once rewrote config.yaml with its own,
newer ``_config_version`` stamp, and the next ``hermes update`` refused to promote the live
release (``config ... is newer than this release``). These tests drive the real write paths
(``config set``/``unset``, ``migrate_config``'s stamp, ``save_config``) against a tmp home whose
``current`` symlink names a fake ready release.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import hermes_yaml as yaml
from hermes_cli import config


ORIGINAL = "_config_version: 48\nmodel:\n  default: test-model\nagent:\n  max_turns: 7\n"


def _make_release(home: Path, sha: str, schema: int = 49) -> Path:
    release = home / "releases" / sha
    release.mkdir(parents=True)
    for name in (".release-ready", ".hermes_build_sha"):
        (release / name).write_text(sha + "\n", encoding="utf-8")
    defaults = release / "hermes_cli" / "config_defaults.py"
    defaults.parent.mkdir()
    defaults.write_text(f'    "_config_version": {schema},\n', encoding="utf-8")
    return release


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "config.yaml").write_text(ORIGINAL, encoding="utf-8")
    return home


@pytest.fixture
def live_release(home):
    release = _make_release(home, "a" * 40)
    (home / "current").symlink_to(release)
    return release


def _run_from(monkeypatch, root: Path, schema: int | None = None) -> None:
    from hermes_cli import release_config_owner
    monkeypatch.setattr(release_config_owner, "_running_code_root", lambda: root.resolve())
    if schema is not None:
        monkeypatch.setattr(release_config_owner, "_running_schema_version", lambda: schema)


def _writes():
    return {
        "config set": lambda: config.set_config_value("agent.max_turns", "9"),
        "config unset": lambda: config.unset_config_value("agent.max_turns"),
        "migrate_config stamp": lambda: config.migrate_config(interactive=False, quiet=True),
        "save_config": lambda: config.save_config({**config.read_raw_config(), "x_marker": 1}),
    }


@pytest.mark.parametrize("write", list(_writes()))
def test_foreign_build_write_is_refused_and_config_untouched(home, live_release, tmp_path, monkeypatch, write):
    dev_root = tmp_path / "dev-worktree"
    dev_root.mkdir()
    _run_from(monkeypatch, dev_root, schema=50)
    before = (home / "config.yaml").read_bytes()

    with pytest.raises(RuntimeError) as excinfo:
        _writes()[write]()

    message = str(excinfo.value)
    assert str(dev_root.resolve()) in message
    assert str(live_release.resolve()) in message
    assert "refusing to write" in message
    assert (home / "config.yaml").read_bytes() == before
    assert yaml.safe_load(before)["_config_version"] == 48


def test_previously_live_release_can_write_after_promotion(home, live_release, tmp_path, monkeypatch):
    """Promotion must not break an older long-lived process's harmless config writes."""
    candidate = _make_release(home, "c" * 40, schema=50)
    (home / "current").unlink()
    (home / "current").symlink_to(candidate)
    _run_from(monkeypatch, live_release)

    config.set_config_value("agent.max_turns", "9")
    assert config.read_raw_config()["agent"]["max_turns"] == 9


def test_newer_foreign_build_is_refused(home, live_release, tmp_path, monkeypatch):
    dev_root = _make_release(tmp_path, "d" * 40, schema=50)
    _run_from(monkeypatch, dev_root, schema=50)
    before = (home / "config.yaml").read_bytes()

    with pytest.raises(RuntimeError, match="config schema 50"):
        config.set_config_value("agent.max_turns", "9")
    assert (home / "config.yaml").read_bytes() == before


def test_config_set_cli_exits_nonzero_naming_both_roots(home, live_release, tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from hermes_cli.main import cmd_config

    dev_root = tmp_path / "dev-worktree"
    dev_root.mkdir()
    _run_from(monkeypatch, dev_root, schema=50)
    before = (home / "config.yaml").read_bytes()

    with pytest.raises(SystemExit) as excinfo:
        cmd_config(SimpleNamespace(config_command="unset", key="agent.max_turns"))

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert str(dev_root.resolve()) in err and str(live_release.resolve()) in err
    assert (home / "config.yaml").read_bytes() == before


def test_profile_config_under_release_managed_root_is_guarded(home, live_release, tmp_path, monkeypatch):
    profile = home / "profiles" / "work"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(ORIGINAL, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    dev_root = tmp_path / "dev-worktree"
    dev_root.mkdir()
    _run_from(monkeypatch, dev_root, schema=50)

    with pytest.raises(RuntimeError, match="refusing to write"):
        config.set_config_value("agent.max_turns", "9")
    assert (profile / "config.yaml").read_text(encoding="utf-8") == ORIGINAL


@pytest.mark.parametrize("write", list(_writes()))
def test_live_release_writes_normally(home, live_release, monkeypatch, write):
    _run_from(monkeypatch, live_release)
    _writes()[write]()
    assert (home / "config.yaml").read_text(encoding="utf-8") != ORIGINAL


def test_live_release_reached_through_current_symlink_writes(home, live_release, monkeypatch):
    _run_from(monkeypatch, home / "current")
    config.set_config_value("agent.max_turns", "9")
    assert config.read_raw_config()["agent"]["max_turns"] == 9


@pytest.mark.parametrize("write", list(_writes()))
def test_home_without_release_pointer_writes_normally(home, tmp_path, monkeypatch, write):
    dev_root = tmp_path / "dev-worktree"
    dev_root.mkdir()
    _run_from(monkeypatch, dev_root, schema=50)
    _writes()[write]()
    assert (home / "config.yaml").read_text(encoding="utf-8") != ORIGINAL


def test_unready_release_pointer_is_not_a_release_managed_home(home, tmp_path, monkeypatch):
    staged = home / "releases" / ("b" * 40)
    staged.mkdir(parents=True)  # no ready markers: not a valid release
    (home / "current").symlink_to(staged)
    dev_root = tmp_path / "dev-worktree"
    dev_root.mkdir()
    _run_from(monkeypatch, dev_root, schema=50)
    config.set_config_value("agent.max_turns", "9")
    assert config.read_raw_config()["agent"]["max_turns"] == 9


def test_updater_context_may_write_from_candidate_release(home, live_release, monkeypatch):
    """The immutable post-swap child runs from the staged candidate while ``current`` still
    names the old release; its strict maintenance must migrate config before promotion."""
    from hermes_cli.release_config_owner import updater_owns_config_writes

    candidate = _make_release(home, "c" * 40, schema=50)
    _run_from(monkeypatch, candidate, schema=50)
    with updater_owns_config_writes():
        config.migrate_config(interactive=False, quiet=True)
    stamp, latest = config._read_config_version_stamp()
    assert stamp == latest > 1

    # The exemption is scoped: the same process refuses once the updater context exits.
    before = (home / "config.yaml").read_bytes()
    with pytest.raises(RuntimeError, match="refusing to write"):
        config.set_config_value("agent.max_turns", "9")
    assert (home / "config.yaml").read_bytes() == before


def test_cmd_update_runs_impl_inside_updater_context(monkeypatch):
    """``hermes update`` (and its ``--post-swap`` child, which re-enters ``cmd_update``) is the
    only entry that sets the exemption."""
    from types import SimpleNamespace
    from hermes_cli import main as hermes_main
    from hermes_cli import update_cmd, update_lock
    from hermes_cli.release_config_owner import in_updater_context

    seen = []
    monkeypatch.setattr(hermes_main, "_update_preflight_handled", lambda args: False)
    monkeypatch.setattr(hermes_main, "_install_hangup_protection", lambda **kw: None)
    monkeypatch.setattr(hermes_main, "_finalize_update_output", lambda state: None)
    monkeypatch.setattr(hermes_main, "_finalize_update_receipt", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.update_owning_install.retarget_to_owning_install", lambda root: None)
    monkeypatch.setattr(update_lock.UpdateLock, "acquire", lambda self: True)
    monkeypatch.setattr(update_lock.UpdateLock, "release", lambda self: None)
    monkeypatch.setattr(update_cmd, "_cmd_update_impl", lambda args, gateway_mode: seen.append(in_updater_context()))
    monkeypatch.setattr(hermes_main, "PROJECT_ROOT", Path(__file__).resolve().parents[2])

    assert in_updater_context() is False
    hermes_main.cmd_update(SimpleNamespace(post_swap="x", rollback=False, gateway=False))
    assert seen == [True]
    assert in_updater_context() is False
