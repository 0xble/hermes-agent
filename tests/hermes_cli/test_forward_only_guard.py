import json
import subprocess

import pytest

from hermes_cli import forward_only_guard as guard


def test_loaded_forward_label_is_detected_from_launchctl_list(tmp_path, monkeypatch):
    monkeypatch.setattr(guard.sys, "platform", "darwin")

    def fake_run(argv, **kwargs):
        assert argv == ["launchctl", "list"]
        return subprocess.CompletedProcess(
            argv, 0,
            stdout="123\t0\tai.hermes.gateway.g-0123456789abcdef0123456789abcdef\n",
            stderr="",
        )

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    assert guard.leftover_forward_only_state(tmp_path) == [
        "loaded launchd label ai.hermes.gateway.g-0123456789abcdef0123456789abcdef"
    ]


def test_nonterminal_forward_update_is_detected_in_temp_home(tmp_path, monkeypatch):
    monkeypatch.setattr(guard.sys, "platform", "linux")
    path = tmp_path / "forward-update.json"
    path.write_text(json.dumps({"outcome": "running"}), encoding="utf-8")
    assert guard.leftover_forward_only_state(tmp_path) == [f"file {path}"]
    with pytest.raises(RuntimeError, match="Leftovers after withdrawal"):
        guard.refuse_if_forward_only_leftovers(tmp_path)


def test_terminal_forward_update_is_not_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(guard.sys, "platform", "linux")
    (tmp_path / "forward-update.json").write_text(
        json.dumps({"outcome": "success"}), encoding="utf-8"
    )
    assert guard.leftover_forward_only_state(tmp_path) == []
