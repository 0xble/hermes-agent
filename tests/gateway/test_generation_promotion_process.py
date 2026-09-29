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
@pytest.mark.parametrize("approval_route", ["text", "callback", "stop", "steer", "clarify"])
@pytest.mark.asyncio
async def test_two_gateway_processes_promote_without_overlapping_pollers(tmp_path, monkeypatch, approval_route):
    monkeypatch.setenv("HERMES_LAUNCHD_LABEL", "ai.hermes.gateway")
    monkeypatch.setenv("HERMES_RELEASE_SHA", "inherited-release")
    api = BotAPI()
    started = tmp_path / "tool-running"
    command = f"chmod 777 {shlex.quote(str(tmp_path))} && touch {shlex.quote(str(started))} && sleep 60"
    def model(record):
        messages = record["body"]["messages"]
        if messages and messages[-1].get("role") == "tool":
            return Text("clarify-complete" if approval_route == "clarify" else "old-turn-complete")
        user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        content = str(user.get("content", ""))
        if "old-boundary" in content:
            if approval_route == "clarify":
                return ToolCall("clarify", {"question": "Which colour?", "choices": ["red", "blue"]})
            return ToolCall("terminal", {"command": command})
        if "old-followup" in content:
            return Text("old-followup-complete")
        return Text("new-turn-complete")
    llm = FakeLLMServer(model)
    llm.__enter__()
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: fake-model\n"
        f"  base_url: {llm.base_url}\n  key_env: OPENAI_API_KEY\n"
        "agent:\n  api_max_retries: 1\n"
        "approvals:\n  mode: manual\n  timeout: 120\n"
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
                while time.monotonic() < end:
                    with api.lock:
                        approval_prompt = any(("Which colour?" in item["text"] if approval_route == "clarify"
                                               else "needs your OK" in item["text"] and "chmod" in item["text"])
                                              for item in api.sent)
                    if approval_prompt:
                        break
                    await asyncio.sleep(.1)
                else:
                    proc.kill()
                    await asyncio.to_thread(proc.wait, 8)
                    raise AssertionError(f"A did not request approval: model calls={len(llm.main_requests())}, sent={api.sent}, "
                                         f"offsets={api.offsets}, stderr={proc.stderr.read()[-4000:] if proc.stderr else ''}")
                assert not started.exists(), "dangerous command ran before approval"
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
        for update_id, command_text in ((1002, "/status"), (1003, "/queue")):
            api.add(update_id, update_id, text=command_text)
            end = time.monotonic() + 10
            row = None
            while time.monotonic() < end:
                with db.connect() as conn:
                    row = conn.execute("SELECT owner_id,state FROM inbox WHERE source_event_id=?",
                                       (str(update_id),)).fetchone()
                if row and row["state"] != "pending":
                    break
                await asyncio.sleep(.1)
            assert row and row["owner_id"] != successor["id"] and row["state"] == "accepted", command_text
        callback_data = ""
        if approval_route == "clarify":
            api.add(1004, 1004, text="red")
            end = time.monotonic() + 20
            answer_row = None
            completed = False
            while time.monotonic() < end:
                with db.connect() as conn:
                    answer_row = conn.execute("SELECT owner_id,state FROM inbox WHERE source_event_id='1004'").fetchone()
                with api.lock:
                    completed = any("clarify-complete" in item["text"].replace("\\", "") for item in api.sent)
                if answer_row and answer_row["state"] == "accepted" and completed:
                    break
                await asyncio.sleep(.1)
            assert answer_row and answer_row["owner_id"] != successor["id"] and answer_row["state"] == "accepted"
            assert completed, "A did not consume the clarify answer"
            await asyncio.to_thread(processes[0].wait, 30)
            assert processes[0].returncode == 0, processes[0].stderr.read()
            return
        if approval_route == "callback":
            with api.lock:
                prompt = next(item for item in api.sent if "needs your OK" in item["text"])
                buttons = json.loads(prompt["reply_markup"])["inline_keyboard"]
                callback_data = next(button["callback_data"] for row in buttons for button in row
                                     if button["callback_data"].startswith("ea:once:"))
            api.add_callback(1004, callback_data)
        else:
            api.add(1004, 1004, text="/approve")
        end = time.monotonic() + 20
        while not started.exists() and time.monotonic() < end:
            await asyncio.sleep(.1)
        assert started.exists(), "approval via B did not resume A's dangerous tool"
        approval = None
        end = time.monotonic() + 10
        while time.monotonic() < end:
            with db.connect() as conn:
                approval = conn.execute("SELECT owner_id,state FROM inbox WHERE source_event_id='1004'").fetchone()
            if approval and approval["state"] != "pending":
                break
            await asyncio.sleep(.1)
        assert approval and approval["owner_id"] != successor["id"] and approval["state"] == "accepted"
        if approval_route == "stop":
            api.add(1005, 1005, text="/stop")
            end = time.monotonic() + 15
            stop_row = None
            while time.monotonic() < end:
                with db.connect() as conn:
                    stop_row = conn.execute("SELECT owner_id,state FROM inbox WHERE source_event_id='1005'").fetchone()
                if stop_row and stop_row["state"] != "pending":
                    break
                await asyncio.sleep(.1)
            assert stop_row and stop_row["owner_id"] != successor["id"] and stop_row["state"] == "accepted"
            await asyncio.to_thread(processes[0].wait, 30)
            assert processes[0].returncode == 0, processes[0].stderr.read()
            return
        if approval_route == "steer":
            api.add(1005, 1005, text="/steer old-followup")
            end = time.monotonic() + 20
            steer_row = None
            while time.monotonic() < end:
                with db.connect() as conn:
                    steer_row = conn.execute("SELECT owner_id,state FROM inbox WHERE source_event_id='1005'").fetchone()
                if steer_row and steer_row["state"] != "pending":
                    break
                await asyncio.sleep(.1)
            assert steer_row and steer_row["owner_id"] != successor["id"] and steer_row["state"] == "accepted"
            end = time.monotonic() + 80
            while time.monotonic() < end:
                with api.lock:
                    if any("old-followup-complete" in item["text"].replace("\\", "") for item in api.sent):
                        break
                await asyncio.sleep(.1)
            else:
                with api.lock:
                    raise AssertionError(f"A did not consume steer: {api.sent}")
            return
        if approval_route == "callback":
            api.add_callback(1005, callback_data)
        else:
            api.add(1005, 1005, text="/approve")
        duplicate = None
        end = time.monotonic() + 10
        while time.monotonic() < end:
            with db.connect() as conn:
                duplicate = conn.execute("SELECT owner_id,state FROM inbox WHERE source_event_id='1005'").fetchone()
            if duplicate and duplicate["state"] != "pending":
                break
            await asyncio.sleep(.1)
        assert duplicate and duplicate["owner_id"] != successor["id"] and duplicate["state"] == "accepted"
        with api.lock:
            assert sum("needs your OK" in item["text"] for item in api.sent) == 1
        api.add(1006, 1006, text="new-boundary", chat_id=2)
        end = time.monotonic() + 25
        while time.monotonic() < end:
            with api.lock:
                if any("new-turn-complete" in item["text"].replace("\\", "") for item in api.sent):
                    break
            await asyncio.sleep(.1)
        else:
            raise AssertionError("B did not answer new session")
        api.add(1007, 1007, text="old-followup")
        end = time.monotonic() + 12
        while time.monotonic() < end:
            with db.connect() as conn:
                row = conn.execute("SELECT owner_id FROM inbox WHERE source_event_id='1007'").fetchone()
            if row:
                assert row["owner_id"] != successor["id"], "B took A's in-flight session"
                break
            await asyncio.sleep(.1)
        else:
            raise AssertionError("B did not enqueue A's follow-up")
        with api.lock:
            assert not any("old-followup-complete" in item["text"] for item in api.sent), "B ran A's follow-up"
        processes[1].kill()
        await asyncio.to_thread(processes[1].wait, 5)
        with api.lock:
            post_kill_polls = len(api.offsets)
        await asyncio.sleep(1)
        with api.lock:
            assert len(api.offsets) == post_kill_polls, "A resumed polling after B died"
        assert processes[0].poll() is None, "A did not preserve its in-flight turn"
        # The follow-up redirects A's running turn, rather than letting its original
        # terminal call finish. Its reply proves the owning native adapter ran it.
        end = time.monotonic() + 25
        while time.monotonic() < end:
            with api.lock:
                if any("old-followup-complete" in item["text"].replace("\\", "") for item in api.sent):
                    break
            await asyncio.sleep(.1)
        else:
            with api.lock:
                sent = list(api.sent)
            raise AssertionError(f"A did not handle its follow-up; sent={sent}; generations={db.generations()}")
        with api.lock:
            assert sum("old-followup-complete" in item["text"].replace("\\", "") for item in api.sent) == 1
            assert sum("new-turn-complete" in item["text"].replace("\\", "") for item in api.sent) == 1
        with api.lock:
            assert len(api.offsets) > before, "B never entered a real getUpdates loop"
            assert api.maximum == 1 and not api.errors
        await asyncio.to_thread(processes[0].wait, 75)
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
           "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS": "1", "TEST_PAUSE_TRANSFER": str(marker)}
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


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "worker":
    raise SystemExit(_worker(sys.argv[2] == "standby"))
