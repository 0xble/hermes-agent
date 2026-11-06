"""Config v52 adopts launcher settings and preserves the native browser owner."""

import hermes_yaml as yaml
import pytest

from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

_AB_ENV = ("AGENT_BROWSER_EXECUTABLE_PATH", "AGENT_BROWSER_PROFILE", "AGENT_BROWSER_ARGS",
           "AGENT_BROWSER_HEADED", "AGENT_BROWSER_ENGINE")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _AB_ENV:
        monkeypatch.delenv(name, raising=False)


def _home(path, config, env=""):
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (path / ".env").write_text(env, encoding="utf-8")
    return path


def _raw(home):
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))


def _migrate(home, *, quiet=False):
    from hermes_cli.config import migrate_config

    token = set_hermes_home_override(home)
    try:
        return migrate_config(interactive=False, quiet=quiet)
    finally:
        reset_hermes_home_override(token)






def _boot_agents(home, monkeypatch, starts: int) -> list[str]:
    """Boot maintenance for *home* (its config step), then *starts* agents in that process; the startup
    warnings they show."""
    import hermes_cli.left_core_migration as lcm
    from hermes_cli import post_update

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(lcm, "_attempted", set())
    post_update.step_migrate_config()
    said = []
    for _ in range(starts):
        lcm.recover_at_startup(say=said.append)
    return said










def test_dotenv_settings_move_to_unset_keys_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    env = ("AGENT_BROWSER_EXECUTABLE_PATH=/opt/chrome/chrome\n"
           "AGENT_BROWSER_PROFILE=/data/profile\n"
           "AGENT_BROWSER_ARGS=--no-sandbox, --lang=de\n"
           "AGENT_BROWSER_HEADED=true\n"
           "AGENT_BROWSER_ENGINE=lightpanda\n")
    home = _home(tmp_path / "a", {"_config_version": 51, "browser": {"user_data_dir": "/mine"}}, env)
    results = _migrate(home, quiet=True)
    browser = _raw(home)["browser"]
    assert browser == {
        "executable_path": "/opt/chrome/chrome",
        "user_data_dir": "/mine",  # already set: the user's value wins
        "chromium_args": ["--no-sandbox", "--lang=de"],
        "headed": True,
        "engine": "lightpanda",
    }
    assert any("AGENT_BROWSER_ARGS → browser.chromium_args" in item for item in results["config_added"])
    # .env is left as it was.
    assert (home / ".env").read_text(encoding="utf-8") == env


def test_env_wins_over_the_defaults_core_wrote(tmp_path, monkeypatch):
    """Core resolved ``engine: auto`` to AGENT_BROWSER_ENGINE and ``headed`` as config OR env, and its
    `hermes tools` Local Browser row wrote ``engine: auto``: those values do not block the move. A
    non-default engine won on core and stays."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    env = "AGENT_BROWSER_ENGINE=lightpanda\nAGENT_BROWSER_HEADED=true\n"
    defaults = _home(tmp_path / "a", {"_config_version": 51, "browser": {"engine": "auto", "headed": False}}, env)
    chosen = _home(tmp_path / "b", {"_config_version": 51, "browser": {"engine": "chrome", "headed": "false"}}, env)
    _migrate(defaults, quiet=True)
    _migrate(chosen, quiet=True)
    assert _raw(defaults)["browser"] == {"engine": "lightpanda", "headed": True}
    assert _raw(chosen)["browser"] == {"engine": "chrome", "headed": True}




def test_relative_profile_keeps_the_directory_core_used(tmp_path, monkeypatch):
    """agent-browser resolved a relative AGENT_BROWSER_PROFILE against Hermes's cwd; browser.user_data_dir
    anchors relative paths at HERMES_HOME, so the migration pins the absolute directory."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    home = _home(tmp_path / "a", {"_config_version": 51}, "AGENT_BROWSER_PROFILE=prof/rel\n")
    _migrate(home, quiet=True)
    assert _raw(home)["browser"]["user_data_dir"] == str(cwd / "prof" / "rel")


def test_a_pinned_relative_profile_names_the_absolute_path_on_update_and_boot(tmp_path, monkeypatch):
    """The relative AGENT_BROWSER_PROFILE was resolved against whatever directory the migrating process ran
    in: `hermes update` and the first agent after a boot migration both name the absolute path pinned."""
    import hermes_cli.left_core_migration as lcm

    monkeypatch.delenv("CAMOFOX_URL", raising=False)
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    pinned = str(cwd / "prof" / "rel")
    updated = _home(tmp_path / "a", {"_config_version": 51}, "AGENT_BROWSER_PROFILE=prof/rel\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    results = _migrate(updated, quiet=True)
    assert any(pinned in note and "prof/rel" in note for note in results["config_added"])

    booted = _home(tmp_path / "b", {"_config_version": 51}, "AGENT_BROWSER_PROFILE=prof/rel\n")
    said = _boot_agents(booted, monkeypatch, starts=1)
    assert _raw(booted)["browser"]["user_data_dir"] == pinned
    assert len(said) == 1 and pinned in said[0] and "prof/rel" in said[0]


def test_falsy_headed_and_unknown_engine_carry_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    home = _home(tmp_path / "a", {"_config_version": 51},
                 "AGENT_BROWSER_HEADED=0\nAGENT_BROWSER_ENGINE=webkit\n")
    _migrate(home, quiet=True)
    assert not _raw(home).get("browser")


def test_pm_store_chromium_path_is_not_pinned(tmp_path, monkeypatch):
    """pm (and the Docker image) exported its own Chromium under AGENT_BROWSER_EXECUTABLE_PATH; that is the
    default now, and a copied versioned store path would break at the next Chromium update."""
    from pm import paths

    store = tmp_path / "tools"
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(store))
    pm_binary = paths.store_root() / "chromium-1208" / "chrome-linux64" / "chrome"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "a"))
    home = _home(tmp_path / "a", {"_config_version": 51})
    monkeypatch.setenv("AGENT_BROWSER_EXECUTABLE_PATH", str(pm_binary))
    _migrate(home, quiet=True)
    assert "executable_path" not in (_raw(home).get("browser") or {})


def test_profiles_are_islands_a_b_a(tmp_path, monkeypatch, capsys):
    """Each home is decided by its own files; the process env counts for the launch home only."""
    a = _home(tmp_path / "a", {"_config_version": 51, "browser": {"backend": "off"}},
              "AGENT_BROWSER_EXECUTABLE_PATH=/opt/a/chrome\n")
    b = _home(tmp_path / "b", {"_config_version": 51, "browser": {"inactivity_timeout": 300}})
    monkeypatch.setenv("HERMES_HOME", str(a))  # A is the launch home
    monkeypatch.setenv("AGENT_BROWSER_HEADED", "1")  # e.g. a systemd unit for A

    _migrate(a)
    out_a = capsys.readouterr().out
    _migrate(b)
    out_b = capsys.readouterr().out
    _migrate(a)
    out_a2 = capsys.readouterr().out

    assert _raw(a)["browser"] == {"backend": "off", "executable_path": "/opt/a/chrome", "headed": True}
    assert "browser_exec is the browser now" not in out_a
    assert _raw(b)["browser"] == {"inactivity_timeout": 300}
    assert "browser_exec" not in out_b and "agent-browser" not in out_b
    assert "browser_exec" not in out_a2 and "agent-browser" not in out_a2


@pytest.mark.parametrize("stamp", [51, None])
def test_native_backend_and_extension_consent_survive_migration(tmp_path, monkeypatch, stamp):
    """The maintained native browser still consumes backend choice and controller consent."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    config = {"browser": {"backend": "camofox", "extension_control": {"enabled": True, "developer_mode": False}, "headed": True}}
    if stamp is not None:
        config["_config_version"] = stamp
    home = _home(tmp_path / "a", config)
    results = _migrate(home, quiet=True)
    assert _raw(home)["browser"] == config["browser"]
    assert not any("removed browser." in item for item in results["config_added"])

@pytest.mark.parametrize("backend", ["off", False, "browser-use", "agent-browser", "local"])
def test_native_backend_selection_is_not_replaced_at_boot(tmp_path, monkeypatch, backend):
    home = _home(tmp_path / "a", {"_config_version": 51, "browser": {"backend": backend}})
    monkeypatch.delenv("CAMOFOX_URL", raising=False)
    assert _boot_agents(home, monkeypatch, starts=2) == []
    assert _raw(home)["browser"]["backend"] == backend
    assert _raw(home)["_config_version"] == DEFAULT_CONFIG["_config_version"]
