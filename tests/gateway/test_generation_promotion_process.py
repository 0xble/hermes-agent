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


def _handover_with_diagnostics(home, successor_id, processes, stderr_paths=(), **kwargs):
    try:
        return handover_to_generation(home, successor_id, **kwargs)
    except Exception as exc:
        diagnostics = []
        for path in stderr_paths:
            diagnostics.append(f"{path.name}:\n{path.read_text()[-12000:]}")
        for proc in processes:
            if proc.stderr is not None:
                os.set_blocking(proc.stderr.fileno(), False)
                chunks = []
                while True:
                    try:
                        chunk = os.read(proc.stderr.fileno(), 65536)
                    except BlockingIOError:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
                diagnostics.append(f"pid={proc.pid} exit={proc.poll()} stderr:\n" +
                                   b"".join(chunks).decode(errors="replace")[-12000:])
        log = home / "logs" / "gateway.log"
        diagnostics.append("gateway.log tail:\n" + (log.read_text()[-24000:] if log.exists() else "missing"))
        coordinator = GenerationCoordinator(home)
        diagnostics.append(f"generations={coordinator.generations()!r}\nleases={coordinator.leases()!r}")
        raise AssertionError(f"handover failed: {exc!r}\n" + "\n".join(diagnostics)) from exc


def _worker(standby: bool):
    from gateway.config import load_gateway_config
    from gateway.run import start_gateway
    if os.environ.get("TEST_SHORT_TRANSFER_WINDOW") and not standby:
        from gateway import run_generation
        run_generation.HANDOVER_REQUEST_TIMEOUT = 2
    if not standby and os.environ.get("TEST_PAUSE_TRANSFER"):
        from gateway.run_generation import ActiveGeneration
        async def paused_transfer(self, new_id: str, **kwargs) -> dict:
            Path(os.environ["TEST_PAUSE_TRANSFER"]).touch()
            await asyncio.Event().wait()
            return {}
        ActiveGeneration.transfer_requested = paused_transfer
    if not standby and os.environ.get("TEST_BUFFER_PHOTO"):
        from gateway.run_generation import ActiveGeneration
        from gateway.platforms.event import MessageEvent, MessageType
        original_transfer = ActiveGeneration.transfer_requested
        async def transfer_with_photo(self, new_id, **kwargs):
            for adapter in self._telegram_adapters().values():
                adapter._media_batch_delay_seconds = 120
                source = adapter.build_source(chat_id="1", chat_type="dm", user_id="1")
                adapter._canonicalize(source)
                event = MessageEvent(text="/status", source=source, message_type=MessageType.PHOTO,
                                     platform_update_id=1099)
                batch_key = adapter._photo_batch_key(event, SimpleNamespace(media_group_id=None))
                adapter._pending_photo_batches[batch_key] = event
                adapter._pending_photo_batch_tasks[batch_key] = asyncio.create_task(adapter._flush_photo_batch(batch_key))
            return await original_transfer(self, new_id, **kwargs)
        from types import SimpleNamespace
        ActiveGeneration.transfer_requested = transfer_with_photo
    print(f"WORKER:{'B' if standby else 'A'}", flush=True)
    success = asyncio.run(start_gateway(load_gateway_config(), standby=standby,
                                        force=bool(os.environ.get("TEST_THIRD_FORCE"))))
    print(f"EXIT:{success}", flush=True)
    return 0 if success else 1


@pytest.mark.integration
@pytest.mark.spawns_gateway_lookalike
@pytest.mark.parametrize("approval_route", ["text", "callback", "stop", "steer", "clarify"])
@pytest.mark.asyncio
async def test_two_gateway_processes_promote_without_overlapping_pollers(tmp_path, monkeypatch, approval_route):
    monkeypatch.setenv("HERMES_LAUNCHD_LABEL", "ai.hermes.gateway")
    monkeypatch.setenv("HERMES_RELEASE_SHA", "inherited-release")
    api = BotAPI()
    started = tmp_path / "tool-running"
    tool_duration = 60
    # chmod 777 intentionally triggers the dangerous-command approval detector.
    command = (f"chmod 777 {shlex.quote(str(tmp_path))} && "
               f"touch {shlex.quote(str(started))} && sleep {tool_duration}")
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
    home = Path(tempfile.mkdtemp(prefix="hermes-p3-", dir="/tmp"))  # UNIX socket path limit
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
           "OPENAI_API_KEY": "local-test-key", "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS": "1",
           "TEST_BUFFER_PHOTO": "1"}
    processes = []
    stderr_paths = []
    worker_path = tmp_path / "gateway" / "run.py"
    worker_path.parent.mkdir()
    worker_path.symlink_to(Path(__file__).resolve())
    try:
        for standby in (False, True):
            stderr_path = tmp_path / ("standby.stderr" if standby else "active.stderr")
            stderr_paths.append(stderr_path)
            with stderr_path.open("w") as stderr_file:
                proc = subprocess.Popen([sys.executable, str(worker_path),
                                         "worker", "standby" if standby else "active"],
                                        env=env, stdout=subprocess.PIPE, stderr=stderr_file,
                                        text=True, bufsize=1)
            processes.append(proc)
            deadline = time.monotonic() + 35
            expected = 2 if standby else 1
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise AssertionError(f"gateway exit {proc.returncode}: {stderr_paths[-1].read_text()}")
                if len(GenerationCoordinator(home).generations()) >= expected:
                    rows = GenerationCoordinator(home).generations()
                    if standby and any(row["state"] == "standby" and row["label"] == "ai.hermes.gateway-b"
                                       for row in rows):
                        break
                    if not standby and any(row["state"] in {"standby", "serving"} and
                                           row["label"] == "ai.hermes.gateway" for row in rows):
                        break
                await asyncio.sleep(.1)
            else:
                raise AssertionError(f"gateway did not register in time: rows={GenerationCoordinator(home).generations()}; "
                                     f"pid={proc.pid} alive={proc.poll() is None}; "
                                     f"files={[p.name for p in home.iterdir()]}")
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
                                         f"offsets={api.offsets}, stderr={stderr_paths[-1].read_text()[-4000:]}")
                assert not started.exists(), "dangerous command ran before approval"
                await asyncio.sleep(.3)
                with api.lock:
                    assert not any("old-turn-complete" in item["text"] for item in api.sent)
        db = GenerationCoordinator(home)
        successor = next(row for row in db.generations() if row["state"] == "standby" and row["label"] == "ai.hermes.gateway-b")
        with api.lock:
            before = len(api.offsets)
            assert before > 0, "A never entered a real getUpdates loop"
        old = next(row for row in db.generations() if row["label"] == "ai.hermes.gateway")
        old_identity = GenerationIdentity(**{key: old[key] for key in
            ("id", "release_sha", "label", "pid", "started_at", "boot_id", "start_fingerprint")})
        old_socket = generation_paths(home, old_identity)["socket"]
        assert old_socket.exists(), (
            f"A control socket disappeared: {old_socket}; root={list(old_socket.parent.glob('*'))}; "
            f"stderr={stderr_paths[0].read_text() if processes[0].returncode is not None else ''}")
        assert processes[0].poll() is None, (
            f"A exited before handover: code={processes[0].returncode}, "
            f"stderr={stderr_paths[0].read_text() if processes[0].returncode is not None else ''}")
        try:
            result = await asyncio.to_thread(_handover_with_diagnostics, home, successor["id"], processes, stderr_paths, timeout=35)
        except Exception as exc:
            from gateway.run_generation import _generation_request
            successor_socket = generation_paths(home, GenerationIdentity(**{key: successor[key] for key in
                ("id", "release_sha", "label", "pid", "started_at", "boot_id", "start_fingerprint")}))["socket"]
            try:
                status = await asyncio.to_thread(_generation_request, successor_socket, "polling_status", timeout=1)
            except Exception as status_exc:
                status = repr(status_exc)
            raise AssertionError(f"handover failed: {exc!r}; B status={status}; "
                                 f"B exit={processes[1].poll()}; B stderr={stderr_paths[1].read_text()[-8000:]}") from exc
        assert result > 1
        with db.connect() as conn:
            photo_rows = conn.execute("SELECT owner_id,state FROM inbox WHERE source_event_id='1099'").fetchall()
        assert [(row["owner_id"], row["state"]) for row in photo_rows] == [(old["id"], "accepted")]
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
            assert processes[0].returncode == 0, stderr_paths[0].read_text()
            # B must own the real singleton resources after A exits, not merely
            # appear in projected runtime status.
            from gateway.status import get_running_pid_identity_strict
            deadline = time.monotonic() + 10
            status = None
            lock_owned = False
            while time.monotonic() < deadline:
                status = await asyncio.to_thread(query_gateway_control, home, "status", timeout=.5)
                lock_owned = is_gateway_runtime_lock_active(home / "gateway.lock")
                if lock_owned and status is not None and status.get("answering_pid") == processes[1].pid:
                    break
                await asyncio.sleep(.1)
            assert lock_owned and (home / "gateway.sock").exists(), status
            assert status is not None and status.get("generation_id") == successor["id"], status
            owner = get_running_pid_identity_strict(home / "gateway.pid")
            assert owner is not None and owner[0] == processes[1].pid
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
                    or "already owns this host" in third_output)
            assert "EXIT:False" in third.stdout, third.stdout + third.stderr
            assert processes[1].poll() is None, "third start displaced promoted B"
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
            assert processes[0].returncode == 0, stderr_paths[0].read_text()
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
        # The follow-up redirects A's running turn rather than letting its
        # original terminal call finish.
        end = time.monotonic() + tool_duration + 35
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
        assert processes[0].returncode == 0, stderr_paths[0].read_text()
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
async def test_split_text_batch_flushed_by_old_process_during_promotion(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_LAUNCHD_LABEL", "ai.hermes.gateway")
    monkeypatch.setenv("HERMES_RELEASE_SHA", "inherited-release")
    api = BotAPI()
    llm = FakeLLMServer(lambda record: Text("split-batch-complete"))
    llm.__enter__()
    home = Path(tempfile.mkdtemp(prefix="hermes-p3-batch-", dir="/tmp"))
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: fake-model\n"
        f"  base_url: {llm.base_url}\n  key_env: OPENAI_API_KEY\n"
        "updates:\n  check: false\n"
        "gateway:\n  overlap_handover:\n    enabled: true\n"
        "platforms:\n  telegram:\n    enabled: true\n    token: '" + TOKEN + "'\n"
        "    extra:\n      base_url: '" + api.url + "'\n"
        "      base_file_url: '" + api.url + "'\n"
        "      allow_from: ['1']\n      text_batch_split_delay_seconds: 4\n"
        "      drop_pending_on_cold_boot: false\n")
    env = {**{key: value for key, value in os.environ.items() if not key.startswith("HERMES_")},
           "HERMES_HOME": str(home), "PYTHONPATH": str(Path.cwd()),
           "HERMES_GATEWAY_LOCK_DIR": str(tmp_path / "locks"),
           "OPENAI_API_KEY": "local-test-key", "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS": "1"}
    worker_path = tmp_path / "gateway" / "run.py"
    worker_path.parent.mkdir()
    worker_path.symlink_to(Path(__file__).resolve())
    processes = []
    stderr_paths = []
    try:
        for standby in (False, True):
            error_path = tmp_path / ("batch-b.stderr" if standby else "batch-a.stderr")
            stderr_paths.append(error_path)
            with error_path.open("w") as stderr_file:
                process = subprocess.Popen([sys.executable, str(worker_path), "worker",
                    "standby" if standby else "active"], env=env, stdout=subprocess.PIPE,
                    stderr=stderr_file, text=True, bufsize=1)
            processes.append(process)
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                rows = GenerationCoordinator(home).generations()
                if any(row["label"] == ("ai.hermes.gateway-b" if standby else "ai.hermes.gateway")
                       and row["state"] in ({"standby"} if standby else {"serving"}) for row in rows):
                    break
                if process.poll() is not None:
                    raise AssertionError(error_path.read_text())
                await asyncio.sleep(.1)
            else:
                raise AssertionError(f"batch gateway not ready: {rows}")
        db = GenerationCoordinator(home)
        poll_deadline = time.monotonic() + 25
        while time.monotonic() < poll_deadline:
            with api.lock:
                if api.offsets:
                    break
            await asyncio.sleep(.1)
        else:
            raise AssertionError("old gateway did not begin polling")
        old = next(row for row in db.generations() if row["label"] == "ai.hermes.gateway")
        new = next(row for row in db.generations() if row["label"] == "ai.hermes.gateway-b")
        from gateway.run_generation import _generation_request
        old_socket = generation_paths(home, GenerationIdentity(**{key: old[key] for key in
            ("id", "release_sha", "label", "pid", "started_at", "boot_id", "start_fingerprint")}))["socket"]
        ready_deadline = time.monotonic() + 20
        status = None
        while time.monotonic() < ready_deadline:
            status = await asyncio.to_thread(_generation_request, old_socket, "polling_status", timeout=1)
            if status.get("polling"):
                break
            await asyncio.sleep(.1)
        else:
            raise AssertionError(f"old runner never became ready: {status}")
        api.add(1, 1, text="split-batch " + "x" * 4000)
        # The stub accepts a real near-limit split chunk. Ensure it is in the
        # Telegram journal but has not hit the model before handover starts.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            from plugins.platforms.telegram.polling_transfer import PollingJournal
            journal = PollingJournal(db, TOKEN)
            if not journal.pending():
                with journal._connect() as conn:
                    seen = conn.execute("SELECT state FROM telegram_updates WHERE update_id=1").fetchone()
                if seen:
                    break
            await asyncio.sleep(.05)
        else:
            raise AssertionError("split chunk did not reach old poller")
        with api.lock:
            assert not any("split-batch-complete" in row["text"] for row in api.sent)
        await asyncio.to_thread(_handover_with_diagnostics, home, new["id"], processes, stderr_paths, timeout=35)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            with api.lock:
                replies = [row for row in api.sent if "split-batch-complete" in row["text"].replace("\\", "")]
            if replies:
                break
            await asyncio.sleep(.1)
        if len(replies) != 1:
            with db.connect() as conn:
                inbox = [dict(row) for row in conn.execute("SELECT owner_id,state,source_event_id FROM inbox")]
            log = home / "logs" / "gateway.log"
            raise AssertionError(f"split reply count={len(replies)}; inbox={inbox}; "
                f"model={len(llm.main_requests())}; sent={api.sent}; "
                f"log={log.read_text()[-5000:] if log.exists() else None}; "
                f"stderr={[path.read_text()[-3000:] for path in stderr_paths]}")
        with db.connect() as conn:
            accepted = conn.execute("SELECT owner_id,state FROM inbox WHERE source_event_id='1'").fetchone()
        assert accepted and accepted["owner_id"] == old["id"] and accepted["state"] == "accepted"
        await asyncio.to_thread(processes[0].wait, 30)
        assert processes[0].returncode == 0, stderr_paths[0].read_text()
        await asyncio.sleep(.5)
        with api.lock:
            assert sum("split-batch-complete" in row["text"].replace("\\", "") for row in api.sent) == 1, api.sent
        assert len(llm.main_requests()) == 1, "the successor regenerated the old owner's turn"
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, 8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, 5)
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
                if len(rows) == len(processes) and rows[-1]["state"] in ({"standby"} if standby else {"serving", "standby"}):
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
        assert next(row for row in db.generations() if row["id"] == successor["id"])["state"] == "standby"
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
@pytest.mark.parametrize("abort_before_kill", [False, True])
@pytest.mark.asyncio
async def test_driver_killed_after_stop_receipt_rearms_old_and_retry_succeeds(tmp_path, monkeypatch, abort_before_kill):
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
                        {"standby"} if standby else {"serving", "standby"}):
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
                                   successor["id"], str(marker), str(int(abort_before_kill))], env=env,
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
        assert next(row for row in db.generations() if row["id"] == successor["id"])["state"] == "standby"
        assert await asyncio.to_thread(_handover_with_diagnostics, home, successor["id"], processes, timeout=15) > 1
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
        if sys.argv[5] == "1":
            nonce = self.transfer_attempt_nonce(args[0], args[2])
            assert self.abort_transfer(*args[:3], attempt_nonce=nonce)
        ack_marker.touch()
        time.sleep(60)
    GenerationCoordinator.commit_transfer = pause_commit
    handover_to_generation(Path(sys.argv[2]), sys.argv[3], timeout=20)

if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "worker":
    raise SystemExit(_worker(sys.argv[2] == "standby"))
