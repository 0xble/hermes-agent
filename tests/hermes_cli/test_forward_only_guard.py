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


def _domain_launchctl(*, gui=None, user=None, no_home=False):
    def fake_run(argv, **kwargs):
        if argv == ["launchctl", "list"]:
            return subprocess.CompletedProcess(argv, 0, stdout=f"123\t0\t{_LABEL}\n", stderr="")
        assert argv[:2] == ["launchctl", "print"] and argv[2].endswith(f"/{_LABEL}")
        domain = argv[2].split("/", 1)[0]
        value = gui if domain == "gui" else user
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, int):
            return subprocess.CompletedProcess(argv, value, stdout="", stderr="Could not find service")
        if no_home:
            stdout = f"{_LABEL} = {{\n\tenvironment = {{\n\t\tPATH => /usr/bin\n\t}}\n}}\n"
        else:
            stdout = f"HERMES_HOME => {value}\n"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")
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
def test_nonzero_print_in_both_domains_is_inspection_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(guard.sys, "platform", "darwin")
    monkeypatch.setattr(guard.subprocess, "run", _domain_launchctl(gui=113, user=113))
    with pytest.raises(guard.LeftoverInspectionError, match="both domains"):
        guard.leftover_forward_only_state(tmp_path)


@pytest.mark.platforms("macos")
def test_user_domain_owner_is_used_when_gui_domain_does_not_have_label(tmp_path, monkeypatch):
    monkeypatch.setattr(guard.sys, "platform", "darwin")
    other = tmp_path / "other-home"
    other.mkdir()
    mine = tmp_path / "mine"
    mine.mkdir()
    monkeypatch.setattr(guard.subprocess, "run", _domain_launchctl(gui=113, user=other))
    assert guard.leftover_forward_only_state(mine) == []
    monkeypatch.setattr(guard.subprocess, "run", _domain_launchctl(gui=113, user=mine))
    assert guard.leftover_forward_only_state(mine) == [f"loaded launchd label {_LABEL}"]


@pytest.mark.platforms("macos")
def test_print_timeout_is_inspection_failure_and_supervised_run_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(guard.sys, "platform", "darwin")
    monkeypatch.setattr(
        guard.subprocess,
        "run",
        _domain_launchctl(gui=subprocess.TimeoutExpired(["launchctl", "print"], 5), user=subprocess.TimeoutExpired(["launchctl", "print"], 5)),
    )
    with pytest.raises(guard.LeftoverInspectionError, match="timed out"):
        guard.leftover_forward_only_state(tmp_path)

    from hermes_cli import gateway
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: tmp_path)
    with pytest.raises(SystemExit) as exc:
        gateway._refuse_forward_only_leftovers(retry_on_inspection_error=True)
    assert exc.value.code == 75


@pytest.mark.platforms("macos")
def test_readable_job_without_home_line_remains_a_leftover(tmp_path, monkeypatch):
    monkeypatch.setattr(guard.sys, "platform", "darwin")
    monkeypatch.setattr(guard.subprocess, "run", _domain_launchctl(gui=0, user=0, no_home=True))
    assert guard.leftover_forward_only_state(tmp_path) == [
        f"loaded launchd label {_LABEL} (owner HERMES_HOME unreadable)"
    ]
    with pytest.raises(RuntimeError):
        guard.refuse_if_forward_only_leftovers(tmp_path)


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

    def refuse(_home, **_):
        raise RuntimeError("withdrawn handover state remains")

    monkeypatch.setattr(gateway, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(guard, "refuse_if_forward_only_leftovers", refuse)
    args = SimpleNamespace(system=False, all=False, force=False)
    with pytest.raises(SystemExit):
        gateway._cmd_start(args)
    with pytest.raises(SystemExit):
        gateway._cmd_restart(args)

    ran = []
    monkeypatch.setattr(gateway, "_maybe_redirect_run_to_s6_supervision", lambda _args: False)
    monkeypatch.setattr(gateway, "run_gateway", lambda *a, **k: ran.append(True))
    run_args = SimpleNamespace(verbose=0, quiet=True, replace=False, force=False, external_supervisor=True)
    with pytest.raises(SystemExit):
        gateway._cmd_run(run_args)
    assert ran == []  # the direct/launchd run path never reaches the gateway

    # A transient launchctl failure must not park the supervised run path (exit 78 parks launchd).
    def unreadable(_home):
        raise guard.LeftoverInspectionError("could not inspect loaded launchd jobs: timeout")

    monkeypatch.setattr(guard, "refuse_if_forward_only_leftovers", unreadable)
    with pytest.raises(SystemExit) as relaunch:
        gateway._cmd_run(run_args)
    assert relaunch.value.code == 75  # launchd relaunches 75; 78 would park the job
    assert ran == []
    with pytest.raises(SystemExit) as parked:
        gateway._cmd_start(args)
    assert parked.value.code == 78
    monkeypatch.setattr(guard, "refuse_if_forward_only_leftovers", refuse)

    monkeypatch.setattr(gateway, "is_managed", lambda: False)
    with pytest.raises(SystemExit):
        gateway._cmd_install(SimpleNamespace(if_missing=False, system=False, force=True))
    monkeypatch.setattr(update_cmd, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(update_cmd, "_require_immutable_launchd", lambda: None)
    monkeypatch.setattr(update_cmd, "_immutable_release_enabled", lambda paths: True)
    monkeypatch.setattr(update_cmd, "refuse_if_forward_only_leftovers", refuse)
    assert update_cmd._activate_immutable_release() is False


def test_guardian_refuses_to_bootstrap_beside_withdrawn_leftovers(tmp_path, monkeypatch):
    from hermes_cli import gateway_guardian

    launched = []

    def refuse(_home, **_):
        raise RuntimeError("withdrawn handover state remains")

    monkeypatch.setattr(guard, "refuse_if_forward_only_leftovers", refuse)
    monkeypatch.setattr(gateway_guardian.subprocess, "run",
                        lambda argv, **k: launched.append(argv))
    with pytest.raises(RuntimeError):
        gateway_guardian._refuse_leftovers_before_launch(tmp_path)
    assert launched == []
