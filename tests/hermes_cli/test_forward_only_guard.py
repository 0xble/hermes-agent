import json
import subprocess

import pytest

from hermes_cli import forward_only_guard as guard


_LABEL = "ai.hermes.gateway.g-0123456789abcdef0123456789abcdef"


def _fake_launchctl(owner_home, *, print_rc=0):
    def fake_run(argv, **kwargs):
        if argv == ["launchctl", "list"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"123\t0\t{_LABEL}\n", stderr="")
        assert argv[:2] == ["launchctl", "print"] and argv[2].endswith(f"/{_LABEL}")
        stdout = (
            f"{_LABEL} = {{\n\tenvironment = {{\n\t\tHERMES_HOME => {owner_home}\n"
            "\t\tPATH => /usr/bin\n\t}\n}\n"
        )
        return subprocess.CompletedProcess(argv, print_rc, stdout=stdout if print_rc == 0 else "", stderr="")
    return fake_run


@pytest.mark.platforms("macos")
def test_loaded_forward_label_is_detected_from_launchctl_list(tmp_path, monkeypatch):
    monkeypatch.setattr(guard.sys, "platform", "darwin")
    monkeypatch.setattr(guard.subprocess, "run", _fake_launchctl(tmp_path))
    assert guard.leftover_forward_only_state(tmp_path) == [f"loaded launchd label {_LABEL}"]


@pytest.mark.platforms("macos")
def test_other_installations_generation_label_does_not_block_this_home(tmp_path, monkeypatch):
    monkeypatch.setattr(guard.sys, "platform", "darwin")
    other = tmp_path / "other-home"
    other.mkdir()
    mine = tmp_path / "mine"
    mine.mkdir()
    monkeypatch.setattr(guard.subprocess, "run", _fake_launchctl(other))
    assert guard.leftover_forward_only_state(mine) == []
    guard.refuse_if_forward_only_leftovers(mine)


@pytest.mark.platforms("macos")
def test_generation_label_with_unreadable_owner_stays_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(guard.sys, "platform", "darwin")
    monkeypatch.setattr(guard.subprocess, "run", _fake_launchctl(tmp_path, print_rc=113))
    assert guard.leftover_forward_only_state(tmp_path) == [
        f"loaded launchd label {_LABEL} (owner HERMES_HOME unreadable)"
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


def test_withdrawal_guard_blocks_start_restart_and_activation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from hermes_cli import gateway, update_cmd

    def refuse(_home):
        raise RuntimeError("withdrawn handover state remains")

    monkeypatch.setattr(gateway, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(guard, "refuse_if_forward_only_leftovers", refuse)
    args = SimpleNamespace(system=False, all=False, force=False)
    with pytest.raises(SystemExit):
        gateway._cmd_start(args)
    with pytest.raises(SystemExit):
        gateway._cmd_restart(args)

    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(update_cmd, "_require_immutable_launchd", lambda: None)
    monkeypatch.setattr(update_cmd, "_immutable_release_enabled", lambda paths: True)
    monkeypatch.setattr(update_cmd, "refuse_if_forward_only_leftovers", refuse)
    assert update_cmd._activate_immutable_release() is False
