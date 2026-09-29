"""Real gateway processes share one Telegram poller during cooperative promotion."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("telegram")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
from telegram_polling_stub import BotAPI
from gateway.generation import GenerationCoordinator, GenerationIdentity, generation_paths
from gateway.run_generation import handover_to_generation
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall

TOKEN = "123456:LOCAL_STUB_ONLY"


def _worker(standby: bool):
    from gateway.config import load_gateway_config
    from gateway.run import start_gateway
    if not standby and os.environ.get("TEST_PAUSE_TRANSFER"):
        from gateway.run_generation import ActiveGeneration
        async def paused_transfer(self, new_id: str) -> dict:
            Path(os.environ["TEST_PAUSE_TRANSFER"]).touch()
            await asyncio.Event().wait()
            return {}
        ActiveGeneration.transfer_requested = paused_transfer
    print(f"WORKER:{'B' if standby else 'A'}", flush=True)
    success = asyncio.run(start_gateway(load_gateway_config(), standby=standby))
    print(f"EXIT:{success}", flush=True)
    return 0 if success else 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_gateway_processes_promote_without_overlapping_pollers(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LAUNCHD_LABEL", "ai.hermes.gateway")
    monkeypatch.setenv("HERMES_RELEASE_SHA", "inherited-release")
    api = BotAPI()
    started = tmp_path / "tool-running"
    command = f"touch {shlex.quote(str(started))} && sleep 60"
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
    home = tmp_path / "home"
    home.mkdir()
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
           "OPENAI_API_KEY": "local-test-key", "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS": "1",
           "HERMES_RELEASE_SHA": "a" * 40}
    processes = []
    try:
        for standby in (False, True):
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
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
                    if not standby and any(row["state"] == "ready" for row in rows):
                        break
                await asyncio.sleep(.1)
            else:
                raise AssertionError("gateway did not register in time")
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
        processes[1].kill()
        await asyncio.to_thread(processes[1].wait, 5)
        with api.lock:
            post_kill_polls = len(api.offsets)
        await asyncio.sleep(1)
        with api.lock:
            assert len(api.offsets) == post_kill_polls, "A resumed polling after B died"
        assert processes[0].poll() is None, "A did not preserve its in-flight turn"
        end = time.monotonic() + 85
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


@pytest.mark.asyncio
async def test_killing_old_before_stop_receipt_never_promotes_standby(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LAUNCHD_LABEL", "ai.hermes.gateway")
    monkeypatch.setenv("HERMES_RELEASE_SHA", "inherited-release")
    api = BotAPI()
    home = tmp_path / "home"
    home.mkdir()
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
           "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS": "1", "TEST_PAUSE_TRANSFER": str(marker),
           "HERMES_RELEASE_SHA": "a" * 40}
    processes = []
    try:
        for standby in (False, True):
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                     "worker", "standby" if standby else "active"],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(proc)
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                rows = GenerationCoordinator(home).generations()
                if len(rows) == len(processes) and rows[-1]["state"] == "ready":
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


@pytest.mark.asyncio
async def test_successor_stops_wire_before_restoring_old_polling(tmp_path):
    """A real Bot API stub sees no concurrent poll even across rollback."""
    from gateway.run_generation import _generation_request
    api = BotAPI()
    started = tmp_path / "old-tool-running"
    command = f"touch {shlex.quote(str(started))} && sleep 35"
    def model(record):
        messages = record["body"]["messages"]
        if messages and messages[-1].get("role") == "tool":
            return Text("old-completed")
        if any("old-work" in str(m.get("content", "")) for m in messages if m.get("role") == "user"):
            return ToolCall("terminal", {"command": command})
        return Text("restored-owner-answered")
    llm = FakeLLMServer(model)
    llm.__enter__()
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: fake-model\n"
        f"  base_url: {llm.base_url}\n  key_env: OPENAI_API_KEY\n"
        "agent:\n  api_max_retries: 1\n"
        "approvals:\n  mode: 'off'\n"
        "gateway:\n  overlap_handover:\n    enabled: true\n"
        "platforms:\n  telegram:\n    enabled: true\n    token: '" + TOKEN + "'\n"
        "    extra:\n      base_url: '" + api.url + "'\n"
        "      base_file_url: '" + api.url + "'\n"
        "      allow_from: ['1', '2']\n      drop_pending_on_cold_boot: false\n")
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(Path.cwd()),
           "HERMES_GATEWAY_LOCK_DIR": str(tmp_path / "locks"),
           "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS": "1", "HERMES_RELEASE_SHA": "a" * 40,
           "OPENAI_API_KEY": "local-test-key"}
    processes = []
    try:
        for standby in (False, True):
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                     "worker", "standby" if standby else "active"],
                                    env={**env, "HERMES_LAUNCHD_LABEL":
                                         "ai.hermes.gateway-b" if standby else "ai.hermes.gateway"},
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(proc)
            end = time.monotonic() + 25
            while time.monotonic() < end:
                rows = GenerationCoordinator(home).generations()
                if len(rows) >= len(processes) and all(row["state"] == "ready" for row in rows):
                    break
                assert proc.poll() is None, proc.stderr.read() if proc.poll() is not None else ""
                await asyncio.sleep(.1)
            else:
                raise AssertionError("gateway readiness timed out")
            if not standby:
                api.add(2001, 2001, text="old-work")
                until = time.monotonic() + 20
                while not started.exists() and time.monotonic() < until:
                    await asyncio.sleep(.1)
                assert started.exists(), "A must hold a native tool obligation during rollback"
        db = GenerationCoordinator(home)
        rows = {row["label"]: row for row in db.generations()}
        assert "ai.hermes.gateway-b" in rows, (rows, [(p.poll(), p.stderr.read() if p.poll() is not None else "") for p in processes])
        old, new = rows["ai.hermes.gateway"], rows["ai.hermes.gateway-b"]
        epoch = await asyncio.to_thread(handover_to_generation, home, new["id"], timeout=35)
        socket_b = generation_paths(home, GenerationIdentity(**{key: new[key] for key in
            ("id", "release_sha", "label", "pid", "started_at", "boot_id", "start_fingerprint")}))["socket"]
        socket_a = generation_paths(home, GenerationIdentity(**{key: old[key] for key in
            ("id", "release_sha", "label", "pid", "started_at", "boot_id", "start_fingerprint")}))["socket"]
        stopped = await asyncio.to_thread(_generation_request, socket_b, "stop_for_rollback", timeout=10)
        assert stopped["poller_stopped"] is True and stopped["tokens"] == 1
        restored = db.rollback_transfer(new["id"], old["id"], epoch, poller_stopped=True)
        assert restored == epoch + 1
        armed = await asyncio.to_thread(_generation_request, socket_a, "restore_after_rollback",
                                        params={"epoch": restored}, timeout=15)
        assert armed["polling"] is True
        with api.lock:
            polls_before = len(api.offsets)
        api.add(2002, 2002, text="restored-work", chat_id=2)
        end = time.monotonic() + 20
        answered = progressed = False
        while time.monotonic() < end:
            with api.lock:
                answered = any("restored-owner-answered" in row["text"].replace("\\", "") for row in api.sent)
                progressed = len(api.offsets) > polls_before
                if answered and progressed:
                    break
            await asyncio.sleep(.1)
        assert answered and progressed, (api.offsets, api.sent)
        assert db.leases()[0]["generation_id"] == old["id"]
        with api.lock:
            assert api.maximum == 1 and not api.errors
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                await asyncio.to_thread(proc.wait, 5)
        api.close()
        llm.__exit__(None, None, None)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "worker":
    raise SystemExit(_worker(sys.argv[2] == "standby"))
