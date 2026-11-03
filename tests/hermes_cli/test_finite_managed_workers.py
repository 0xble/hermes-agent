"""Finite auto-starts must serve their own workers, including delegated child stores."""
import json
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.e2e.core.parity._helpers import build_parity_home, hermes_argv, stop_profile_gateway
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
from tests.waiting import wait_until


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("tamper", [None, "secret", "session_id", "execution_id", "generation", "pid", "birth",
                                   "profile_id", "principal", "terminal"])
def test_private_worker_discovery_verifies_the_reserved_adoption_claim(tmp_path, tamper):
    import psutil
    from gateway.session_worker_discovery import worker_discovery_handler
    from gateway.session_admission import admission_fingerprint
    from gateway.session_authorities import SessionAuthorities
    from hermes_cli.process_identity import process_birth
    from hermes_state import SessionDB
    from hermes_state_runtime import RuntimeStoreError, register_worker_execution, mutate_worker_execution
    from tests.fakes.runtime_owner import begin_runtime_epoch

    with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
                          stdin=subprocess.PIPE) as worker, SessionDB(db_path=tmp_path / "state.db") as db:
        db.create_session("session", source="cli")
        epoch = begin_runtime_epoch(db, instance_id="finite")
        scope = dict(profile_id=str(tmp_path.resolve()), session_id="session", execution_id="worker",
                     generation=0, pid=worker.pid, birth=process_birth(psutil.Process(worker.pid)),
                     secret="owner-issued-private-secret")
        principal = "uid:" + str(os.getuid())
        claim = admission_fingerprint(canonical_target="session", payload=scope | {"principal": principal})
        register_worker_execution(db, epoch=epoch, execution_id="worker", session_id="session",
                                  generation=0, kind="compute", adoption_secret=claim)
        authority = SimpleNamespace(db=db, epoch=epoch, profile_id=scope["profile_id"])
        registry = SessionAuthorities(tmp_path)
        registry.add(tmp_path, authority)
        handler = worker_discovery_handler(SimpleNamespace(session_authorities=registry), lambda: {"instance_id": "finite"})
        if tamper == "terminal":
            mutate_worker_execution(db, epoch=epoch, execution_id="worker", session_id="session", generation=0,
                                    sequence=1, operation="execution.finish", payload={})
        elif tamper == "principal":
            principal += "-other"
        elif tamper is not None:
            value = scope[tamper]
            scope[tamper] = value + 1 if isinstance(value, (float, int)) else value + "-wrong"
        try:
            if tamper is None:
                assert handler(scope, principal) == {"instance_id": "finite"}
            else:
                with pytest.raises((RuntimeStoreError, PermissionError)):
                    handler(scope, principal)
        finally:
            worker.stdin.close()
            worker.wait(timeout=5)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("safe_mode", [True, False])
def test_finite_cli_managed_worker_and_delegated_child(tmp_path, safe_mode):
    script = [Text("FINITE-WORKER-DONE")] if safe_mode else [
        ToolCall("delegate_task", {"tasks": [{"goal": "Reply CHILD-DONE"}]}),
        Text("CHILD-DONE"), Text("FINITE-WORKER-DONE")]
    with FakeLLMServer(script) as server:
        ph = build_parity_home(tmp_path, server.base_url, grandchild=False)
        ph.update_config(lambda cfg: cfg.update(gateway={"managed_workers": True},
                                                mcp_servers={}, plugins={"enabled": []}, hooks={}))
        args = ["-z", "Perform the finite worker turn", "--provider", "custom", "--model", "fake-model"]
        if safe_mode:
            args.append("--safe-mode")
        # Safe mode deliberately ignores the profile endpoint. Supply the fake
        # provider explicitly through the chat parser while -z remains finite.
        args.extend(["chat", "--base-url", server.base_url, "--api-key", "fixture"])
        try:
            proc = subprocess.run(hermes_argv(*args), cwd=ph.project,
                                  env={**ph.env(), "OPENAI_BASE_URL": server.base_url, "OPENAI_API_KEY": "fixture"},
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                  encoding="utf-8", timeout=65)
            logs = '\n'.join(p.read_text(encoding="utf-8", errors="replace")[-8000:]
                             for p in ph.hermes_home.rglob("*.log"))
            assert proc.returncode == 0, (proc.stdout, proc.stderr, logs)
            assert "FINITE-WORKER-DONE" in proc.stdout, (proc.stdout, proc.stderr)
            from gateway.status import live_gateway_pid_for_home
            assert wait_until(lambda: live_gateway_pid_for_home(ph.hermes_home) is None, timeout=10)
            with sqlite3.connect(ph.hermes_home / "state.db") as db:
                workers = db.execute("SELECT kind,status FROM worker_executions").fetchall()
                assert workers and all(status == "terminal" for _, status in workers), workers
                assert any(kind == "compute" for kind, _ in workers), workers
                if not safe_mode:
                    assert any(kind == "child" for kind, _ in workers), workers
                    children = db.execute("SELECT id FROM sessions WHERE source='subagent'").fetchall()
                    assert children, workers
                    messages = db.execute("SELECT content FROM messages WHERE session_id=?", children[0]).fetchall()
                    assert "CHILD-DONE" in json.dumps(messages), messages
        finally:
            stop_profile_gateway(ph)
