"""The routing harness owns auto-started gateways, even when pytest is SIGKILLed."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from tests.waiting import wait_until
from ._routing_helpers import REPO_ROOT, hermetic_env


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("mode", ["finalizer", "timeout", "sigkill-pytest", "tui", "idle-exit"])
def test_routing_gateway_dies_with_its_test(tmp_path, mode):
    home = tmp_path / "home"
    home.mkdir()
    # This is a real pytest parent, not a simulated cleanup callback. The single fake host
    # holds the response only in the killed-parent case, giving the outer test a ready witness.
    nested = tmp_path / "test_owner.py"
    nested.write_text('''
import os
import threading
from pathlib import Path
from tests.fakes.fake_llm_provider import FakeLLMServer, Text
from tests.e2e.core.tenancy._routing_helpers import TuiGateway, run_hermes, write_home

def test_owner():
    home = Path(os.environ["CLEANUP_HOME"])
    def respond(_record):
        if os.environ["CLEANUP_MODE"] in ("sigkill-pytest", "timeout"):
            threading.Event().wait(120)
        return Text("owned answer")
    server = FakeLLMServer(respond)
    server.start()
    try:
        write_home(home / ".hermes", {
            "model": {"provider": "custom", "base_url": server.base_url,
                      "api_key": "test-only", "default": "fake-model"},
            "gateway": {"unmanaged_idle_exit_seconds":
                        0.5 if os.environ["CLEANUP_MODE"] == "idle-exit" else 3600},
        }, {})
        if os.environ["CLEANUP_MODE"] == "idle-exit":
            import json
            import time
            from hermes_cli.gateway_runtime_start import spawn_unmanaged_gateway
            os.environ.update(XPC_SERVICE_NAME="ai.hermes.gateway",
                              HERMES_LAUNCHD_LABEL="ai.hermes.gateway",
                              HERMES_GATEWAY_EXTERNAL_SUPERVISOR="1", HERMES_SUPERVISED_CHILD="1")
            child = spawn_unmanaged_gateway(home / ".hermes", deadline=time.monotonic() + 30,
                                            idle_exit=True)
            try:
                assert child.wait(timeout=45) == 0
                state = json.loads((home / ".hermes/gateway_state.json").read_text())
                assert "auto-started gateway idle" in state["exit_reason"]
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=10)
            return
        if os.environ["CLEANUP_MODE"] == "tui":
            import psutil
            client = TuiGateway(home)
            try:
                created = client.call("session.create", {"cols": 100})
                sid = created["result"]["session_id"]
                # The stdio backend executes turns in-process. It is the guardian's
                # worker, not a messaging gateway publishing gateway_state.json.
                workers = psutil.Process(client.proc.pid).children()
                assert len(workers) == 1, workers
                assert workers[0].pid != client.proc.pid
                (home / "tui.pid").write_text(str(workers[0].pid))
                done = client.turn(sid, "hello")
                assert client.event("message.complete", sid)(done), done
            finally:
                client.close()
            return
        timed_out = os.environ["CLEANUP_MODE"] == "timeout"
        result = run_hermes(["chat", "-q", "hello"], home, timeout=20 if timed_out else 120)
        assert result.rc == (-9 if timed_out else 0), result.stderr
    finally:
        server.stop()
''', encoding="utf-8")
    env = hermetic_env(home)
    env.update(CLEANUP_HOME=str(home), CLEANUP_MODE=mode)
    with (tmp_path / "pytest.log").open("w") as output:
        parent = subprocess.Popen([sys.executable, "-m", "pytest", "-q", str(nested)],
                                  cwd=REPO_ROOT, env=env, stdout=output, stderr=output,
                                  start_new_session=True)
        gateway = None
        try:
            state_path = home / ".hermes" / "gateway_state.json"

            def ready():
                assert parent.poll() is None, (tmp_path / "pytest.log").read_text()
                try:
                    if mode == "tui":
                        return {"pid": int((home / "tui.pid").read_text())}
                    state = json.loads(state_path.read_text())
                    return state if state.get("gateway_state") == "running" else None
                except (OSError, ValueError):
                    return None

            state = wait_until(ready, timeout=90, interval=0.1, what="nested gateway ready")
            assert state is not None
            gateway = psutil.Process(state["pid"])
            if mode == "sigkill-pytest":
                parent.kill()
                assert parent.wait(timeout=10) == -signal.SIGKILL
            else:
                assert parent.wait(timeout=90) == 0, (tmp_path / "pytest.log").read_text()
            wait_until(lambda: not gateway.is_running() or gateway.status() == psutil.STATUS_ZOMBIE,
                       timeout=10, interval=0.1, what="test-owned gateway exit")
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=10)
            # Never leave a new orphan when running the failing-first version of this test.
            if gateway is not None:
                try:
                    if gateway.is_running() and gateway.status() != psutil.STATUS_ZOMBIE:
                        os.killpg(os.getpgid(gateway.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
