"""The maintained native Camofox owner is not migrated into a second plugin."""

import pytest

import hermes_cli.left_core_migration as lcm
import hermes_yaml as yaml


@pytest.mark.parametrize("stamp", [None, 51, 52])
def test_camofox_migration_preserves_each_home_and_installs_nothing(tmp_path, monkeypatch, stamp):
    from hermes_cli import tools_config

    monkeypatch.setattr(lcm, "_attempted", set())
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "launch"))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "managed"))
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    homes = []
    for name, backend, disabled in [("a", "camofox", []), ("b", "local", ["browser"])]:
        home = tmp_path / name
        home.mkdir()
        config = {
            "browser": {"backend": backend},
            "platform_toolsets": {"cli": ["terminal", "browser", "file"]},
            "agent": {"disabled_toolsets": disabled},
        }
        if stamp is not None:
            config["_config_version"] = stamp
        (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        (home / ".env").write_text("CAMOFOX_URL=http://localhost:9377\n", encoding="utf-8")
        homes.append((home, config, (home / "config.yaml").read_bytes()))

    for home, config, before in [*homes, homes[0]]:
        said = []
        assert lcm.migrate_home(home, install=lambda name: pytest.fail(f"unexpected install: {name}"), say=said.append) == []
        assert said == []
        assert (home / "config.yaml").read_bytes() == before
        enabled = tools_config._get_platform_tools(config, "cli", include_default_mcp_servers=False)
        assert ("browser" in enabled) is ("browser" not in config["agent"]["disabled_toolsets"])
        assert not (home / "plugins").exists()


def test_camofox_env_stays_out_of_children():
    """Native credentials remain private even without a plugin manifest declaring them."""
    from tools.environments import local_env_policy as policy

    assert "CAMOFOX_URL" in policy._build_provider_env_blocklist()
    assert "CAMOFOX_API_KEY" in policy._ALWAYS_STRIP_KEYS
