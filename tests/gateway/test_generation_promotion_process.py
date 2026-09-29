"""Real gateway processes share one Telegram poller during cooperative promotion."""
from __future__ import annotations

import asyncio
import importlib.machinery
import json
import os
import signal
import shlex
import subprocess
import sys
import tempfile
import shutil
import time
from pathlib import Path

import pytest

telegram_spec = importlib.machinery.PathFinder.find_spec("telegram", sys.path)
if telegram_spec is None or not isinstance(telegram_spec.origin, str) or not Path(telegram_spec.origin).is_file():
    pytest.skip("real python-telegram-bot is required for process tests", allow_module_level=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from telegram_polling_stub import BotAPI
from gateway.generation import GenerationCoordinator, GenerationIdentity, generation_paths
from gateway.control_socket import query_gateway_control
from gateway.status import is_gateway_runtime_lock_active
from gateway.run_generation import handover_to_generation
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall

TOKEN = "123456:LOCAL_STUB_ONLY"


def _worker(standby: bool):
    from gateway.config import load_gateway_config
    from gateway.run import start_gateway
    if os.environ.get("TEST_SHORT_TRANSFER_WINDOW") and not standby:
        from gateway import run_generation
        run_generation.HANDOVER_REQUEST_TIMEOUT = 2
    if not standby and os.environ.get("TEST_PAUSE_TRANSFER"):
        from gateway.run_generation import ActiveGeneration
        async def paused_transfer(self, new_id: str) -> dict:
            Path(os.environ["TEST_PAUSE_TRANSFER"]).touch()
            await asyncio.Event().wait()
            return {}
        ActiveGeneration.transfer_requested = paused_transfer
    print(f"WORKER:{'B' if standby else 'A'}", flush=True)
    success = asyncio.run(start_gateway(load_gateway_config(), standby=standby,
                                        force=bool(os.environ.get("TEST_THIRD_FORCE"))))
    print(f"EXIT:{success}", flush=True)
    return 0 if success else 1


@pytest.mark.integration
@pytest.mark.spawns_gateway_lookalike
@pytest.mark.parametrize("kill_b_early", [True, False])
@pytest.mark.asyncio
async def test_two_gateway_processes_promote_without_overlapping_pollers(tmp_path, monkeypatch, kill_b_early):
    monkeypatch.setenv("HERMES_LAUNCHD_LABEL", "ai.hermes.gateway")
    monkeypatch.setenv("HERMES_RELEASE_SHA", "inherited-release")
    api = BotAPI()
    started = tmp_path / "tool-running"
    tool_duration = 60
    command = f"touch {shlex.quote(str(started))} && sleep {tool_duration}"
    def model(record):
        messages = record["body"]["messages"]
        if messages and messages[-1].get("role") == "tool":
            return Text("old-turn-complete")
        user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        content = str(user.get("content", ""))
        if "old-boundary" in content:
            return ToolCall("terminal", {"command": command})
        return Text("new-turn-complete")
    llm = FakeLLMServer(model)
    llm.__enter__()
    home = Path(tempfile.mkdtemp(prefix="hermes-p3-", dir="/tmp"))  # UNIX socket path limit
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: fake-model\n"
        f"  base_url: {llm.base_url}\n  key_env: OPENAI_API_KEY\n"
        "agent:\n  api_max_retries: 1\n"
        "approvals:\n  mode: 'off'\n"
        "updates:\n  check: false\n"
        "gateway:\n  overlap_handover:\n    enabled: true\n"
        "platforms:\n  telegram:\n    enabled: true\n    token: '" + TOKEN + "'\n"
        "    extra:\n      base_url: '" + api.url + "'\n"
        "      base_file_url: '" + api.url + "'\n"
        "      allow_from: ['1', '2']\n      drop_pending_on_cold_boot: false\n")
    env = {**{key: value for key, value in os.environ.items() if not key.startswith("HERMES_")},
           "HERMES_HOME": str(home), "PYTHONPATH": str(Path.cwd()),
           "HERMES_GATEWAY_LOCK_DIR": str(tmp_path / "locks"),
           "OPENAI_API_KEY": "local-test-key", "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS": "1"}
    processes = []
    worker_path = tmp_path / "gateway" / "run.py"
    worker_path.parent.mkdir()
    worker_path.symlink_to(Path(__file__).resolve())
    try:
        for standby in (False, True):
            proc = subprocess.Popen([sys.executable, str(worker_path),
                                     "worker", "standby" if standby else "active"],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1)
            processes.append(proc)
            deadline = time.monotonic() + 35
            expected = 2 if standby else 1
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise AssertionError(f"gateway exit {proc.returncode}: {proc.stderr.read()}")
                if len(GenerationCoordinator(home).generations()) >= expected:
                    rows = GenerationCoordinator(home).generations()
                    if standby and any(row["state"] == "ready" for row in rows):
                        break
                    if not standby and any(row["state"] in {"ready", "serving"} for row in rows):
                        break
                await asyncio.sleep(.1)
            else:
                raise AssertionError(f"gateway did not register in time: rows={GenerationCoordinator(home).generations()}; "
                                     f"pid={proc.pid} alive={proc.poll() is None}; "
                                     f"files={[p.name for p in home.iterdir()]}")
            if not standby:
                api.add(1001, 1001, text="old-boundary")
                end = time.monotonic() + 25
                while not started.exists() and time.monotonic() < end:
                    await asyncio.sleep(.1)
                if not started.exists():
                    proc.kill()
                    await asyncio.to_thread(proc.wait, 8)
                    raise AssertionError(f"A did not launch tool: model calls={len(llm.main_requests())}, sent={api.sent}, "
                                         f"offsets={api.offsets}, stderr={proc.stderr.read()[-4000:] if proc.stderr else ''}")
                await asyncio.sleep(.3)
                with api.lock:
                    assert not any("old-turn-complete" in item["text"] for item in api.sent)
        db = GenerationCoordinator(home)
        successor = next(row for row in db.generations() if row["state"] == "ready" and row["label"] == "ai.hermes.gateway-b")
        with api.lock:
            before = len(api.offsets)
            assert before > 0, "A never entered a real getUpdates loop"
        old = next(row for row in db.generations() if row["label"] == "ai.hermes.gateway")
        old_identity = GenerationIdentity(**{key: old[key] for key in
            ("id", "release_sha", "label", "pid", "started_at", "boot_id", "start_fingerprint")})
        old_socket = generation_paths(home, old_identity)["socket"]
        assert old_socket.exists(), (
            f"A control socket disappeared: {old_socket}; root={list(old_socket.parent.glob('*'))}; "
            f"stderr={processes[0].stderr.read() if processes[0].returncode is not None else ''}")
        assert processes[0].poll() is None, (
            f"A exited before handover: code={processes[0].returncode}, "
            f"stderr={processes[0].stderr.read() if processes[0].returncode is not None else ''}")
        result = await asyncio.to_thread(handover_to_generation, home, successor["id"], timeout=35)
        assert result > 1
        assert processes[0].poll() is None, "A exited before its turn completed"
        api.add(1002, 1002, text="new-boundary", chat_id=2)
        end = time.monotonic() + 25
        while time.monotonic() < end:
            with api.lock:
                if any("new-turn-complete" in item["text"].replace("\\", "") for item in api.sent):
                    break
            await asyncio.sleep(.1)
        else:
            raise AssertionError("B did not answer new session")
        if kill_b_early:
            processes[1].kill()
            await asyncio.to_thread(processes[1].wait, 5)
            with api.lock:
                post_kill_polls = len(api.offsets)
            await asyncio.sleep(1)
            with api.lock:
                assert len(api.offsets) == post_kill_polls, "A resumed polling after B died"
            assert processes[0].poll() is None, "A did not preserve its in-flight turn"
        end = time.monotonic() + tool_duration + 35
        while time.monotonic() < end:
            with api.lock:
                if any("old-turn-complete" in item["text"].replace("\\", "") for item in api.sent):
                    break
            await asyncio.sleep(.1)
        else:
            raise AssertionError("A did not finish old turn")
        with api.lock:
            assert sum("old-turn-complete" in item["text"].replace("\\", "") for item in api.sent) == 1
            assert sum("new-turn-complete" in item["text"].replace("\\", "") for item in api.sent) == 1
        with api.lock:
            assert len(api.offsets) > before, "B never entered a real getUpdates loop"
            assert api.maximum == 1 and not api.errors
        await asyncio.to_thread(processes[0].wait, 15)
        assert processes[0].returncode == 0, processes[0].stderr.read()
        if not kill_b_early:
            # The promoted owner must acquire the real singleton lock and socket,
            # not merely be visible via its projected runtime status.
            from gateway.status import get_running_pid_identity_strict
            deadline = time.monotonic() + 10
            status = None
            lock_owned = False
            while time.monotonic() < deadline:
                status = await asyncio.to_thread(query_gateway_control, home, "status", timeout=.5)
                lock_owned = is_gateway_runtime_lock_active(home / "gateway.lock")
                if (lock_owned and status is not None and status.get("answering_pid") == processes[1].pid):
                    break
                await asyncio.sleep(.1)
            assert lock_owned, ("B did not acquire the singleton runtime lock within 10s: "
                                f"status={status}; pid={ (home / 'gateway.pid').read_text() if (home / 'gateway.pid').exists() else 'missing' }; "
                                f"gateway_log={ (home / 'logs' / 'gateway.log').read_text()[-4000:] if (home / 'logs' / 'gateway.log').exists() else 'none' }")
            assert (home / "gateway.sock").exists()
            assert status is not None and status.get("generation_id") == successor["id"], status
            owner = get_running_pid_identity_strict(home / "gateway.pid")
            assert owner is not None and owner[0] == processes[1].pid
            assert json.loads((home / "gateway.pid").read_text())["pid"] == processes[1].pid
            contender = await asyncio.to_thread(subprocess.run,
                [sys.executable, "-c", "from gateway.status import acquire_gateway_runtime_lock; "
                 "print(acquire_gateway_runtime_lock())"],
                env=env, capture_output=True, text=True, timeout=10)
            assert contender.returncode == 0 and contender.stdout.strip() == "False", contender.stderr
            third = await asyncio.to_thread(subprocess.run,
                [sys.executable, str(worker_path), "worker", "active"],
                env={**env, "TEST_THIRD_FORCE": "1"}, capture_output=True, text=True, timeout=25)
            assert third.returncode != 0, third.stdout + third.stderr
            third_output = third.stdout + third.stderr
            gateway_log = (home / "logs" / "gateway.log").read_text()
            assert ("Gateway runtime lock is already held" in third_output
                    or "Gateway runtime lock is already held" in gateway_log
                    or "already owns the host" in third_output)
            assert "EXIT:False" in third.stdout, third.stdout + third.stderr
            assert processes[1].poll() is None, "third start displaced promoted B"
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
                try:
                    await asyncio.to_thread(proc.wait, 8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    await asyncio.to_thread(proc.wait, 5)
        api.close()
        llm.__exit__(None, None, None)
        shutil.rmtree(home)


@pytest.mark.integration
@pytest.mark.spawns_gateway_lookalike
@pytest.mark.asyncio
async def test_killing_old_before_stop_receipt_never_promotes_standby(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LAUNCHD_LABEL", "ai.hermes.gateway")
    monkeypatch.setenv("HERMES_RELEASE_SHA", "inherited-release")
    api = BotAPI()
    home = Path(tempfile.mkdtemp(prefix="hermes-p3-", dir="/tmp"))  # UNIX socket path limit
    (home / "config.yaml").write_text(
        "gateway:\n  overlap_handover:\n    enabled: true\n"
        "platforms:\n  telegram:\n    enabled: true\n    token: '" + TOKEN + "'\n"
        "    extra:\n      base_url: '" + api.url + "'\n"
        "      base_file_url: '" + api.url + "'\n"
        "      allow_from: ['1']\n      drop_pending_on_cold_boot: false\n")
    marker = tmp_path / "stop-requested"
    env = {**{key: value for key, value in os.environ.items() if not key.startswith("HERMES_")},
           "HERMES_HOME": str(home), "PYTHONPATH": str(Path.cwd()),
           "HERMES_GATEWAY_LOCK_DIR": str(tmp_path / "locks"),
           "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS": "1", "TEST_PAUSE_TRANSFER": str(marker)}
    processes = []
    worker_path = tmp_path / "gateway" / "run.py"
    worker_path.parent.mkdir()
    worker_path.symlink_to(Path(__file__).resolve())
    try:
        for standby in (False, True):
            proc = subprocess.Popen([sys.executable, str(worker_path),
                                     "worker", "standby" if standby else "active"],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(proc)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                rows = GenerationCoordinator(home).generations()
                if len(rows) == len(processes) and rows[-1]["state"] in ({"ready"} if standby else {"serving", "ready"}):
                    break
                assert proc.poll() is None, f"gateway died {proc.returncode}"
                await asyncio.sleep(.1)
            else:
                raise AssertionError("generation did not become ready")
        db = GenerationCoordinator(home)
        assert all(row["release_sha"] != "inherited-release" for row in db.generations())
        successor = next(row for row in db.generations() if row["label"] == "ai.hermes.gateway-b")
        old = next(row for row in db.generations() if row["label"] == "ai.hermes.gateway")
        path = generation_paths(home, GenerationIdentity(**{key: old[key] for key in
            ("id", "release_sha", "label", "pid", "started_at", "boot_id", "start_fingerprint")}))["socket"]
        state = json.loads((home / f"gateway_state.{old['id']}.json").read_text())
        assert path.exists() and state["socket_path"] == str(path)
        request = asyncio.create_task(asyncio.to_thread(handover_to_generation, home, successor["id"], timeout=5))
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            await asyncio.sleep(.1)
        assert marker.exists(), (f"old never entered stop protocol; request={request.exception() if request.done() else 'pending'}; "
                                 f"rows={db.generations()}; api={api.offsets}; stderr={processes[0].stderr.read() if processes[0].poll() is not None else ''}")
        processes[0].kill()
        await asyncio.to_thread(processes[0].wait, 5)
        with pytest.raises(RuntimeError):
            await request
        assert db.leases()[0]["generation_id"] != successor["id"]
        assert next(row for row in db.generations() if row["id"] == successor["id"])["state"] == "ready"
        with api.lock:
            assert api.maximum <= 1
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                await asyncio.to_thread(proc.wait, 5)
        api.close()
        shutil.rmtree(home)


@pytest.mark.integration
@pytest.mark.spawns_gateway_lookalike
@pytest.mark.asyncio
async def test_driver_killed_after_stop_receipt_rearms_old_and_retry_succeeds(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LAUNCHD_LABEL", "ai.hermes.gateway")
    api = BotAPI()
    llm = FakeLLMServer(lambda record: Text("ok"))
    llm.__enter__()
    home = Path(tempfile.mkdtemp(prefix="hermes-p3-", dir="/tmp"))
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: fake-model\n"
        f"  base_url: {llm.base_url}\n  key_env: OPENAI_API_KEY\n"
        "agent:\n  api_max_retries: 1\n"
        "approvals:\n  mode: 'off'\n"
        "gateway:\n  overlap_handover:\n    enabled: true\n"
        "updates:\n  check: false\n"
        "platforms:\n  telegram:\n    enabled: true\n    token: '" + TOKEN + "'\n"
        "    extra:\n      base_url: '" + api.url + "'\n"
        "      base_file_url: '" + api.url + "'\n"
        "      allow_from: ['1']\n      drop_pending_on_cold_boot: false\n")
    env = {**{key: value for key, value in os.environ.items() if not key.startswith("HERMES_")},
           "HERMES_HOME": str(home), "PYTHONPATH": str(Path.cwd()),
           "HERMES_GATEWAY_LOCK_DIR": str(tmp_path / "locks"),
           "OPENAI_API_KEY": "local-test-key", "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS": "1",
           "TEST_SHORT_TRANSFER_WINDOW": "1"}
    worker_path = tmp_path / "gateway" / "run.py"
    worker_path.parent.mkdir()
    worker_path.symlink_to(Path(__file__).resolve())
    processes = []
    marker = tmp_path / "driver-acknowledged"
    try:
        db = GenerationCoordinator(home)
        for standby in (False, True):
            proc = subprocess.Popen([sys.executable, str(worker_path), "worker",
                                     "standby" if standby else "active"],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(proc)
            end = time.monotonic() + 30
            while time.monotonic() < end:
                rows = db.generations()
                if len(rows) == len(processes) and rows[-1]["state"] in (
                        {"ready"} if standby else {"serving", "ready"}):
                    break
                assert proc.poll() is None, f"gateway exited {proc.returncode}: {proc.stderr.read()}"
                await asyncio.sleep(.1)
            else:
                raise AssertionError("generation did not become ready")
        successor = next(row for row in db.generations() if row["label"] == "ai.hermes.gateway-b")
        old = next(row for row in db.generations() if row["label"] == "ai.hermes.gateway")
        old_identity = GenerationIdentity(**{key: old[key] for key in
            ("id", "release_sha", "label", "pid", "started_at", "boot_id", "start_fingerprint")})
        old_socket = generation_paths(home, old_identity)["socket"]
        from gateway.run_generation import _generation_request
        end = time.monotonic() + 25
        while time.monotonic() < end:
            status = await asyncio.to_thread(_generation_request, old_socket, "polling_status", timeout=.5)
            if status and status.get("polling") is True:
                break
            await asyncio.sleep(.1)
        else:
            raise AssertionError("A did not reach dispatch readiness")
        driver = subprocess.Popen([sys.executable, str(worker_path), "driver", str(home),
                                   successor["id"], str(marker)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(driver)
        end = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < end:
            assert driver.poll() is None, (f"driver died: {driver.stderr.read()}; "
                                           f"old_stderr={processes[0].stderr.read() if processes[0].poll() is not None else ''}; "
                                           f"gateway_log={(home / 'logs' / 'gateway.log').read_text()[-5000:] if (home / 'logs' / 'gateway.log').exists() else 'none'}")
            await asyncio.sleep(.1)
        assert marker.exists(), "driver did not reach the post-ack boundary"
        assert processes[0].poll() is None
        receipts = db.transfer_receipts(db.leases()[0]["generation_id"], db.leases()[0]["epoch"])
        assert receipts and all(row["poller_stopped"] for row in receipts)
        driver.kill()
        await asyncio.to_thread(driver.wait, 5)
        with api.lock:
            before = len(api.offsets)
        end = time.monotonic() + 9
        while time.monotonic() < end:
            with api.lock:
                resumed = len(api.offsets) > before
            if resumed:
                break
            await asyncio.sleep(.1)
        assert resumed, "A never resumed real polling after the driver died"
        with api.lock:
            assert api.maximum == 1 and not api.errors
        with db.connect() as conn:
            state = conn.execute("SELECT state FROM generation_transfers").fetchone()[0]
        assert state == "aborted"
        assert db.leases()[0]["generation_id"] != successor["id"]
        assert next(row for row in db.generations() if row["id"] == successor["id"])["state"] == "ready"
        assert await asyncio.to_thread(handover_to_generation, home, successor["id"], timeout=15) > 1
        with api.lock:
            assert api.maximum == 1 and not api.errors
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
                try:
                    await asyncio.to_thread(proc.wait, 8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    await asyncio.to_thread(proc.wait, 5)
        api.close()
        llm.__exit__(None, None, None)
        shutil.rmtree(home)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "driver":
    from gateway.generation import GenerationCoordinator
    from gateway.run_generation import handover_to_generation

    ack_marker = Path(sys.argv[4])
    def pause_commit(self, *args, **kwargs):
        ack_marker.touch()
        time.sleep(60)
    GenerationCoordinator.commit_transfer = pause_commit
    handover_to_generation(Path(sys.argv[2]), sys.argv[3], timeout=20)

if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "worker":
    raise SystemExit(_worker(sys.argv[2] == "standby"))
