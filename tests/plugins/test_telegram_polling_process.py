"""Two-process handover against PTB, TelegramAdapter and a local Bot API."""
import asyncio
import json
import os
import queue
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
pytest.importorskip("telegram")
from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter
from plugins.platforms.telegram.polling_transfer import PollingJournal
from telegram_polling_stub import BotAPI

TOKEN = "123456:LOCAL_STUB_ONLY"


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_off", [False, True])
async def test_flag_off_keeps_ptb_updater_and_no_coordinator(tmp_path, monkeypatch, configured_off):
    from gateway.config import load_gateway_config
    api = BotAPI()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_TELEGRAM_DISABLE_FALLBACK_IPS", "1")
    if configured_off:
        (tmp_path / "config.yaml").write_text("gateway:\n  overlap_handover:\n    enabled: false\n")
    assert not load_gateway_config().overlap_handover_enabled
    adapter = TelegramAdapter(PlatformConfig(
        enabled=True, token=TOKEN, extra={"base_url": api.url, "base_file_url": api.url,
                                          "drop_pending_on_cold_boot": False}))
    adapter._start_post_connect_housekeeping = lambda: None
    try:
        assert await adapter.connect()
        assert adapter._app.updater.running
        assert adapter._controlled_journal is None
        assert adapter._controlled_poller is None
        assert not (tmp_path / "gateway-coordinator.db").exists()
    finally:
        await adapter.disconnect()
        api.close()


def counts(home):
    with sqlite3.connect(home / "handlers.db") as db:
        db.execute("CREATE TABLE IF NOT EXISTS calls(id INTEGER PRIMARY KEY, count INTEGER NOT NULL)")
        return db.execute("SELECT COUNT(*),COALESCE(SUM(count-1),0) FROM calls").fetchone()


async def worker(url, home, phase, threshold):
    import plugins.platforms.telegram.polling_transfer as journal_module

    original = PollingJournal.record_response
    emitted = False

    def observe(self, payload):
        nonlocal emitted
        envelope = json.loads(payload)
        if envelope.get("result") and not emitted and max(item["update_id"] for item in envelope["result"]) >= threshold:
            emitted = True
            if phase == "after_response":
                print("BOUNDARY:after_response", flush=True)
                while True:
                    time.sleep(0.02)
        original(self, payload)
        if emitted and phase == "after_journal":
            print("BOUNDARY:after_journal", flush=True)
            while True:
                time.sleep(0.02)

    journal_module.PollingJournal.record_response = observe
    adapter = TelegramAdapter(PlatformConfig(
        enabled=True, token=TOKEN,
        extra={"base_url": url, "base_file_url": url,
               "allow_from": ["1"], "drop_pending_on_cold_boot": True}))
    # Housekeeping is unrelated to ingress and would require menus/topic endpoints.
    adapter._start_post_connect_housekeeping = lambda: None

    async def gateway_message_handler(event):
        with sqlite3.connect(home / "handlers.db", timeout=5) as db:
            db.execute("INSERT INTO calls VALUES (?,1) ON CONFLICT(id) DO UPDATE SET count=count+1",
                       (event.platform_update_id,))
        if phase == "after_dispatch" and counts(home)[0] >= threshold:
            print("BOUNDARY:after_dispatch", flush=True)
            while True:
                await asyncio.sleep(0.02)
        return None

    adapter.set_message_handler(gateway_message_handler)
    if not await adapter.connect(polling_standby=(phase == "standby")):
        raise RuntimeError("real adapter connect failed: " + str(adapter.fatal_error_code))
    print("BOUNDARY:READY", flush=True)
    if phase == "standby":
        receipt_path = home / "transfer.json"
        while not receipt_path.exists():
            await asyncio.sleep(0.02)
        await adapter.start_polling_from_transfer(json.loads(receipt_path.read_text()))
        print("BOUNDARY:ACTIVE", flush=True)
    stopped = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stopped.set)
    await stopped.wait()
    receipt = await adapter.stop_polling_for_transfer()
    if phase == "normal":
        (home / "transfer.json").write_text(json.dumps(receipt))
    print("STOPPED:" + json.dumps(receipt), flush=True)
    await adapter.disconnect()


@pytest.mark.asyncio
async def test_standby_does_not_poll_and_transfer_receipt_fences_successor(tmp_path):
    api = BotAPI()
    home = tmp_path / "profile"
    home.mkdir()
    counts(home)
    (home / "config.yaml").write_text("gateway:\n  overlap_handover:\n    enabled: true\n")
    env = {**os.environ, "HERMES_HOME": str(home),
           "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS": "1", "PYTHONPATH": str(Path.cwd())}
    processes = []

    async def launch(phase):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).resolve()), "worker", api.url,
            str(home), phase, "999", env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        processes.append(proc)
        return proc

    async def wait(proc, prefix):
        deadline = asyncio.get_running_loop().time() + 12
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AssertionError("worker did not reach " + prefix)
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining)
            if not raw:
                raise AssertionError("worker exited: " + (await asyncio.wait_for(proc.stderr.read(), 5)).decode())
            text = raw.decode().strip()
            if text.startswith(prefix):
                return text

    try:
        old = await launch("normal")
        await wait(old, "BOUNDARY:READY")
        standby = await launch("standby")
        await wait(standby, "BOUNDARY:READY")
        with api.lock:
            old_offsets = len(api.offsets)
        await asyncio.sleep(0.25)
        with api.lock:
            assert len(api.offsets) >= old_offsets  # only A continues polling
            assert api.maximum == 1
        api.add(1, 2)
        deadline = time.monotonic() + 5
        while counts(home)[0] != 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert counts(home) == (2, 0)
        old.terminate()
        await wait(old, "STOPPED:")
        await asyncio.wait_for(old.wait(), 5)
        await wait(standby, "BOUNDARY:ACTIVE")
        api.add(3, 4)
        deadline = time.monotonic() + 5
        while counts(home)[0] != 4 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert counts(home) == (4, 0)
        standby.terminate()
        receipt = json.loads((await wait(standby, "STOPPED:")).removeprefix("STOPPED:"))
        await asyncio.wait_for(standby.wait(), 5)
        assert receipt["epoch"] == 2
        with api.lock:
            assert api.maximum == 1 and api.inflight == 0 and not api.errors
    finally:
        for proc in processes:
            if proc.returncode is None:
                proc.kill()
                await asyncio.wait_for(proc.wait(), 5)
        api.close()


@pytest.mark.asyncio
async def test_real_adapter_two_process_five_kill_boundaries(tmp_path):
    # Run all six workers in separate OS processes, not two apps on one event loop.
    api = BotAPI()
    home = tmp_path / "profile"
    home.mkdir()
    counts(home)
    events = queue.Queue()
    processes = []
    phases = ("after_response", "after_journal", "after_dispatch",
              "during_long_poll", "after_response")

    def launch(phase, threshold):
        env = {**os.environ, "HERMES_HOME": str(home),
               "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS": "1", "PYTHONPATH": str(Path.cwd())}
        (home / "config.yaml").write_text("gateway:\n  overlap_handover:\n    enabled: true\n")
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "worker", api.url,
             str(home), phase, str(threshold)], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        processes.append(proc)

        def reader():
            for line in proc.stdout:
                events.put((proc, line.strip()))
        threading.Thread(target=reader, daemon=True).start()
        return proc

    def wait(proc, prefix, seconds=18):
        observed = []
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                owner, line = events.get(timeout=0.1)
            except queue.Empty:
                if proc.poll() is not None:
                    raise AssertionError("worker exited: " + proc.stderr.read())
                continue
            observed.append(line)
            if owner is proc and line.startswith(prefix):
                return line
        proc.kill()
        proc.wait(timeout=5)
        raise AssertionError("missing " + prefix + "; observed=" + repr(observed) + "; stderr=" + proc.stderr.read()
                             + "; offsets=" + repr(api.offsets) + "; errors=" + repr(api.errors))

    try:
        for cycle, phase in enumerate(phases):
            proc = launch(phase, (cycle + 1) * 40)
            await asyncio.to_thread(wait, proc, "BOUNDARY:READY")
            if phase == "during_long_poll":
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with api.lock:
                        if api.inflight:
                            break
                    await asyncio.sleep(0.005)
                else:
                    raise AssertionError("no in-flight long poll")
            api.add(cycle * 40 + 1, (cycle + 1) * 40)
            if phase != "during_long_poll":
                await asyncio.to_thread(wait, proc, "BOUNDARY:" + phase)
            proc.kill()
            await asyncio.to_thread(proc.wait, 5)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with api.lock:
                    if api.inflight == 0:
                        break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("old Bot API request still active")
        proc = launch("none", 200)
        await asyncio.to_thread(wait, proc, "BOUNDARY:READY")
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline and counts(home)[0] < 200:
            await asyncio.sleep(0.04)
        assert counts(home) == (200, 0)
        proc.terminate()
        stopped = await asyncio.to_thread(wait, proc, "STOPPED:")
        await asyncio.to_thread(proc.wait, 5)
        receipt = json.loads(stopped.removeprefix("STOPPED:"))
        with sqlite3.connect(home / "gateway-coordinator.db") as db:
            states = db.execute("SELECT update_id,state FROM telegram_updates").fetchall()
        # The after_dispatch kill can interrupt between an external effect and
        # its durable acceptance. That one row is intentionally terminal, not
        # replayed; all other IDs must be accepted without duplicate effects.
        assert len(states) == 200
        assert all(state == "accepted" or (update_id == 120 and state == "processing")
                   for update_id, state in states)
        assert receipt["safe_offset"] == 201
        with api.lock:
            assert api.maximum == 1
            assert api.inflight == 0
            assert api.errors == []
            assert api.confirmed >= 200
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
                await asyncio.to_thread(proc.wait, 5)
        api.close()


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "worker":
    asyncio.run(worker(sys.argv[2], Path(sys.argv[3]), sys.argv[4], int(sys.argv[5])))
