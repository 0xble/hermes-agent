"""Diagnostics must distinguish policy rejection from inspected job properties."""

import json
import plistlib

from tools.terminal_tool_guards import gateway_lifecycle_block


def test_bootstrap_rejection_does_not_invent_keepalive(tmp_path, monkeypatch):
    from tools import process_registry

    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)
    plist = tmp_path / "com.example.schedule.plist"
    plist.write_bytes(plistlib.dumps({
        "Label": "com.example.schedule",
        "ProgramArguments": ["/bin/true"],
        "RunAtLoad": False,
        "StartCalendarInterval": {"Hour": 9, "Minute": 0},
    }))
    blocked = gateway_lifecycle_block(
        command=f"launchctl bootstrap gui/501 {plist}",
        env=None, env_type="local", cwd=str(tmp_path), workdir=None,
        session_key="diagnostic-test",
    )
    assert blocked is not None
    result = json.loads(blocked)
    assert result["exit_code"] == 1
    assert result["error"]


def test_interpreter_kill_rejection_names_the_owned_process_route(tmp_path, monkeypatch):
    """Scheduled-Task topology (#113667): only ``HERMES_SUPERVISED_CHILD`` marks the launch, and an
    image-name kill of the interpreter is refused with the ``proc_*`` / explicit-PID route named.
    Other images and explicit PIDs pass."""
    import os

    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    monkeypatch.setenv("HERMES_SUPERVISED_CHILD", "1")
    for marker in ("INVOCATION_ID", "XPC_SERVICE_NAME", "HERMES_S6_SUPERVISED_CHILD", "HERMES_GATEWAY_EXTERNAL_SUPERVISOR"):
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda *a, **k: os.getpid())

    def run(command):
        return gateway_lifecycle_block(
            command=command, env=None, env_type="local", cwd=str(tmp_path), workdir=None,
            session_key="diagnostic-test",
        )

    blocked = json.loads(run("taskkill /F /IM python.exe 2>/dev/null | head -2"))
    assert blocked["exit_code"] == 1
    assert "proc_" in blocked["error"] and "explicit PID" in blocked["error"]
    assert "proc_" in json.loads(run("pkill -9 python3"))["error"]
    assert run("taskkill /F /IM agent-browser.exe /T") is None
    assert run("taskkill /F /PID 46544") is None
    # Absence on the wrong side: a plain foreground `hermes gateway run` carries no launch marker.
    monkeypatch.delenv("HERMES_SUPERVISED_CHILD")
    assert run("pkill -9 python3") is None


def test_local_ci_gate_missing_relative_references_do_not_spend_remote_budget(tmp_path, monkeypatch):
    """A local ci-gate-like wrapper may mention missing relative scripts while inspecting a tree.

    Those misses are local filesystem results, not remote backend reads. They must not exhaust the
    remote-read budget and turn an otherwise benign gate into a lifecycle-guard refusal.
    """
    from tools import process_registry
    from tools.environments.local import LocalEnvironment

    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)
    wrapper = tmp_path / "ci-gate-like"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        + " && ".join(f"bash missing-{index}.sh" for index in range(70))
        + "\n",
        encoding="utf-8",
    )
    calls = []
    env = object.__new__(LocalEnvironment)
    env.cwd = str(tmp_path)

    def _execute(command):
        calls.append(command)
        return {"returncode": 1, "output": ""}

    monkeypatch.setattr(env, "execute", _execute)

    blocked = gateway_lifecycle_block(
        command=f"{wrapper} expected-sha >| {tmp_path / 'gate.log'} 2>&1",
        env=env,
        env_type="local",
        cwd=str(tmp_path),
        workdir=None,
        session_key="local-ci-gate-regression",
    )

    assert blocked is None
    assert calls == []
