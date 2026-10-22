"""Auxiliary fallback chains are recognized settings without eager config backfill."""

import argparse
import json

import pytest
import hermes_yaml as yaml

from hermes_cli.config import DEFAULT_CONFIG, config_command, migrate_config


@pytest.mark.parametrize("task", ["goal_judge", "compression", "title_generation"])
def test_config_set_auxiliary_fallback_chain_is_recognized(tmp_path, monkeypatch, capsys, task):
    from agent.auxiliary_task_config import _get_auxiliary_task_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").touch()
    chain = [{"provider": "openrouter", "model": "test/model"}]
    config_command(argparse.Namespace(
        config_command="set", key=f"auxiliary.{task}.fallback_chain", value=json.dumps(chain),
    ))

    captured = capsys.readouterr()
    assert "not a recognized config key" not in captured.out + captured.err
    saved = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert saved["auxiliary"][task]["fallback_chain"] == chain
    assert _get_auxiliary_task_config(task)["fallback_chain"] == chain


def test_default_auxiliary_chain_is_inert_and_not_materialized(tmp_path, monkeypatch):
    from agent.auxiliary_client import _try_configured_fallback_chain
    from agent.auxiliary_task_config import _get_auxiliary_task_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").touch()
    config_path = tmp_path / "config.yaml"
    original = f"# Keep user formatting\n_config_version: {DEFAULT_CONFIG['_config_version']}\nmodel: test/model\n"
    config_path.write_text(original, encoding="utf-8")
    for task in ("goal_judge", "title_generation"):
        assert _get_auxiliary_task_config(task)["fallback_chain"] == []
        assert _try_configured_fallback_chain(task, "openrouter") == (None, None, "")

    config_command(argparse.Namespace(config_command="check"))
    results = migrate_config(interactive=False, quiet=True)
    assert not any("fallback_chain" in key for key in results["config_added"])
    assert config_path.read_text(encoding="utf-8") == original
