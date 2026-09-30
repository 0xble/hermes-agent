"""Native launchd overlap rehearsal with disposable labels and a local Bot API."""
from __future__ import annotations

import json
import os
import plistlib
import signal
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Callable

import pytest

from gateway.generation import GenerationCoordinator
from cron.jobs import create_job, load_jobs, save_jobs, use_cron_store
from gateway.run_generation import handover_to_generation
from hermes_cli.gateway_overlap import rollback_overlap
from hermes_cli.immutable_releases import ReleasePaths, activate_release
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
from tests.hermes_cli.immutable_launchd_cleanup import register_disposable_label, sweep_prior_sessions
from tests.plugins.telegram_polling_stub import BotAPI


@pytest.fixture(scope="module", autouse=True)
def _sweep(request):
    sweep_prior_sessions(request)


def _wait_for(predicate, seconds: float, message: str | Callable[[], str]) -> float:
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        if predicate():
            return time.monotonic() - start
        time.sleep(.15)
    raise AssertionError(message() if callable(message) else message)


def _inbox_rows(home: Path, update_id: str) -> list[dict]:
    with GenerationCoordinator(home).connect() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT owner_id,state FROM inbox WHERE source_event_id=?", (update_id,))]


@pytest.mark.integration
@pytest.mark.macos_only
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize("rollback_scenario", [False, True])
def test_long_turn_survives_native_launchd_overlap(request, rollback_scenario):
    """A's long tool survives promotion or guarded rollback with one wire poller."""
    root = Path(tempfile.mkdtemp(prefix="p3overlap-", dir="/tmp"))
    request.addfinalizer(lambda: shutil.rmtree(root, ignore_errors=True))
    home = root / "profile"
    home.mkdir()
    marker = root / "tool-running"
    calls = root / "tool-calls"
    child_started = threading.Event()
    child_release = threading.Event()
    request.addfinalizer(child_release.set)
    api = BotAPI()
    request.addfinalizer(api.close)
    import shlex
    command = (f"chmod 777 {shlex.quote(str(root))} && "
               f"printf 'called\\n' >> {shlex.quote(str(calls))} && "
               f"touch {shlex.quote(str(marker))} && sleep 60")
    cron_marker = root / "cron-calls"
    cron_job_id = ""
    if not rollback_scenario:
        cron_script = home / "scripts" / "overlap-cron.sh"
        cron_script.parent.mkdir()
        cron_script.write_text("#!/bin/sh\n"
                               f"sleep 25\nprintf '%s\\n' \"$HERMES_RELEASE_SHA\" >> {shlex.quote(str(cron_marker))}\n")
        cron_script.chmod(0o700)
        with use_cron_store(home):
            cron_job_id = create_job(None, "every 1m", name="overlap cron ownership",
                                     script=str(cron_script), no_agent=True, deliver="local")["id"]
            stored = load_jobs()
            stored[0]["next_run_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            save_jobs(stored)

    def cron_rows():
        db = home / "cron" / "executions.db"
        if not db.exists():
            return []
        with sqlite3.connect(db) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(
                "SELECT id,job_id,pid,status,claimed_at,finished_at,error FROM executions WHERE job_id=? ORDER BY claimed_at,id",
                (cron_job_id,))]

    def model(record):
        messages = record["body"]["messages"]
        if messages and messages[-1].get("role") == "tool":
            if "delegate_task" in str(messages[-1].get("name", "")) or "subagent" in str(messages[-1].get("content", "")):
                return Text("delegation-started")
            return Text("old-turn-complete")
        user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        content = str(user.get("content", ""))
        if "[ASYNC DELEGATION BATCH COMPLETE" in content:
            return Text("delegation-complete")
        if "child-marker" in content and "delegation-boundary" not in content:
            child_started.set()
            if not child_release.wait(timeout=40):
                return Text("child-timed-out")
            return Text("child-finished")
        if "delegation-boundary" in content:
            return ToolCall("delegate_task", {"tasks": [{"goal": "Reply child-marker then finish."}],
                                              "background": True})
        if "delegation" in content.lower() and "completed" in content.lower():
            return Text("delegation-complete")
        if "old-boundary" in content:
            return ToolCall("terminal", {"command": command})
        if "old-followup" in content:
            return Text("old-followup-complete")
        return Text("new-turn-complete")

    llm = FakeLLMServer(model)
    llm.__enter__()
    request.addfinalizer(lambda: llm.__exit__(None, None, None))
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: fake-model\n"
        f"  base_url: {llm.base_url}\n  key_env: OPENAI_API_KEY\n"
        "agent:\n  api_max_retries: 1\n"
        f"approvals:\n  mode: {'off' if rollback_scenario else 'manual'}\n  timeout: 120\n"
        "updates:\n  check: false\n"
        "display:\n  busy_input_mode: queue\n"
        "gateway:\n  overlap_handover:\n    enabled: true\n"
        "platforms:\n  telegram:\n    enabled: true\n    token: '123456:LOCAL_STUB_ONLY'\n"
        f"    extra:\n      base_url: '{api.url}'\n      base_file_url: '{api.url}'\n"
        "      allow_from: ['1', '2', '3']\n      drop_pending_on_cold_boot: false\n")
    repo = Path(__file__).resolve().parents[2]
    python = Path(sys.executable)
    domain = f"gui/{os.getuid()}"
    labels = [f"ai.hermes.p3test-{uuid.uuid4().hex}-{slot}" for slot in "ab"]
    shas = ["a" * 40, "b" * 40]
    paths = ReleasePaths.for_home(home)
    for sha in shas:
        release = paths.releases / sha
        release.mkdir(parents=True)
        for marker_name in (".release-ready", ".hermes_build_sha"):
            (release / marker_name).write_text(sha)
    paths.current.symlink_to(paths.releases / shas[0])
    plists = []
    for index, label in enumerate(labels):
        plist = root / f"{label}.plist"
        plist.write_bytes(plistlib.dumps({
            "Label": label, "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
            "ExitTimeOut": 60,
            "ProgramArguments": [str(python), "-m", "hermes_cli.main", "gateway", "run"] +
                                (["--standby"] if index else []),
            "WorkingDirectory": str(repo),
            "EnvironmentVariables": {
                "HERMES_HOME": str(home), "PYTHONPATH": str(repo),
                "HERMES_RELEASE_SHA": shas[index], "HERMES_LAUNCHD_LABEL": label,
                "HERMES_GATEWAY_LOCK_DIR": str(root / "locks"),
                "OPENAI_API_KEY": "local-test-key", "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS": "1",
            },
            "StandardOutPath": str(root / f"{label}.out"),
            "StandardErrorPath": str(root / f"{label}.err"),
        }))
        register_disposable_label(request, label, plist)
        plists.append(plist)

    def rows():
        return GenerationCoordinator(home).generations()

    def sent(needle):
        with api.lock:
            return [item for item in api.sent if needle in item["text"].replace("\\", "")]

    try:
        for index, plist in enumerate(plists):
            subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True, timeout=15)
            _wait_for(lambda: len(rows()) == index + 1 and all(
                row["state"] in ({"ready", "serving"} if row["label"] == labels[0] else {"ready"})
                for row in rows()),
                      35, f"generation {index} not ready; log: {root / f'{labels[index]}.err'}")
            if not index:
                api.add(1001, 1001, text="old-boundary")
                if rollback_scenario:
                    _wait_for(marker.exists, 25, "A did not start the long tool")
                else:
                    _wait_for(lambda: len(sent("needs your OK")) == 1, 25,
                              "A did not show the approval prompt before handover")
                    assert not marker.exists(), "tool ran before manual approval"
                    api.add(1002, 1002, text="delegation-boundary", chat_id=3)
                    _wait_for(child_started.is_set, 30, "A did not launch a native async child")
                    _wait_for(lambda: len(cron_rows()) == 1 and cron_rows()[0]["status"] == "running",
                              25, lambda: f"A cron did not start: {cron_rows()}")
        old, new = rows()
        assert [old["release_sha"], new["release_sha"]] == shas
        assert [old["label"], new["label"]] == labels
        transfer_start = time.monotonic()
        epoch = handover_to_generation(home, new["id"], timeout=35)
        transfer_seconds = time.monotonic() - transfer_start
        assert epoch > 1
        assert old["state"] in {"ready", "serving"} and (marker.exists() or not rollback_scenario)
        activate_release(home, paths.releases / shas[1])
        if rollback_scenario:
            rollback_start = time.monotonic()
            restored = rollback_overlap(home, new["id"], old["id"], epoch)
            rollback_seconds = time.monotonic() - rollback_start
            assert rollback_seconds < 60
            assert restored["to_id"] == old["id"] and restored["to_sha"] == shas[0]
            assert paths.current.resolve().name == shas[0]
            api.add(1002, 1002, text="new-boundary", chat_id=2)
            restored_reply_seconds = _wait_for(lambda: len(sent("new-turn-complete")) == 1, 25,
                                               "restored A did not answer a new chat")
            with api.lock:
                assert api.maximum == 1 and not api.errors
            print(f"NATIVE_LAUNCHD guarded_rollback={rollback_seconds:.2f}s "
                  f"restored_reply={restored_reply_seconds:.2f}s max_pollers={api.maximum}", flush=True)
            return
        child_release.set()
        child_seconds = _wait_for(
            lambda: any("child-finished" in str(m.get("content", ""))
                        for r in llm.main_requests() for m in r["messages"]),
            25, "native child did not finish across handover")
        child_contexts = [r["messages"] for r in llm.main_requests()
                          if any("[ASYNC DELEGATION BATCH COMPLETE" in str(m.get("content", ""))
                                 and "child-finished" in str(m.get("content", ""))
                                 for m in r["messages"])]
        assert len(child_contexts) == 1, child_contexts
        assert any("delegation-boundary" in str(m.get("content", ""))
                   for m in child_contexts[0]), "completion did not re-enter A's originating session"
        completion_seconds = _wait_for(lambda: len(sent("delegation-complete")) == 1,
                                       25, lambda: f"completion not delivered; sent={api.sent}, "
                                       f"tail={child_contexts[0][-2:]}, "
                                       f"a-log={(root / f'{labels[0]}.err').read_text()[-4000:]}")
        api.add(1003, 1003, text="/approve")
        approval_seconds = _wait_for(marker.exists, 25, "approval via B did not run A's tool")
        with GenerationCoordinator(home).connect() as conn:
            routed = conn.execute("SELECT owner_id,state FROM inbox WHERE source_event_id='1003'").fetchone()
        assert routed is not None and (routed["owner_id"], routed["state"]) == (old["id"], "accepted")
        api.add(1004, 1004, text="old-followup")
        _wait_for(lambda: any(row["state"] == "accepted" for row in _inbox_rows(home, "1004")),
                  15, "A did not accept queued follow-up routed from B")
        api.add(1005, 1005, text="new-boundary", chat_id=2)
        b_reply_seconds = _wait_for(lambda: len(sent("new-turn-complete")) == 1, 25,
                                    lambda: f"B did not answer: sent={api.sent}, "
                                    f"models={len(llm.main_requests())}, "
                                    f"b-log={(root / f'{labels[1]}.err').read_text()[-3000:]}")
        a_reply_seconds = _wait_for(lambda: len(sent("old-turn-complete")) == 1, 70,
                                    lambda: f"A tool turn incomplete: sent={api.sent}, "
                                    f"tool={calls.read_text() if calls.exists() else 'none'}, "
                                    f"models={[r['messages'][-1] for r in llm.main_requests()]}, "
                                    f"a-log={(root / f'{labels[0]}.err').read_text()[-3000:]}")
        followup_seconds = _wait_for(lambda: len(sent("old-followup-complete")) == 1, 25,
                                     "A did not finish queued follow-up")
        _wait_for(lambda: next(row for row in rows() if row["id"] == old["id"])["state"] == "exited",
                  20, "A did not exit after draining")
        old_job = subprocess.run(["launchctl", "print", f"{domain}/{labels[0]}"],
                                 capture_output=True, text=True, timeout=5)
        assert old_job.returncode == 0 and "last exit code = 0" in old_job.stdout, old_job.stdout[-3000:]
        assert len(sent("new-turn-complete")) == len(sent("old-turn-complete")) == 1
        assert len(sent("old-followup-complete")) == len(sent("delegation-complete")) == 1
        cron_first_seconds = _wait_for(
            lambda: cron_rows()[0]["status"] == "completed" if cron_rows() else False,
            30, lambda: f"A cron did not finish: {cron_rows()}")
        assert cron_marker.read_text().splitlines() == [shas[0]]
        cron_next_seconds = _wait_for(
            lambda: len(cron_rows()) >= 2 and cron_rows()[1]["status"] == "completed",
            100, lambda: f"B did not finish next cron tick: {cron_rows()}")
        assert len(cron_rows()) == 2 and [r["status"] for r in cron_rows()] == ["completed", "completed"], cron_rows()
        assert cron_marker.read_text().splitlines() == shas, cron_rows()
        assert calls.read_text().splitlines() == ["called"]
        assert _inbox_rows(home, "1004") == [{"owner_id": old["id"], "state": "accepted"}]
        with api.lock:
            assert api.maximum == 1 and not api.errors
            assert not any("⏳ Gateway" in item["text"] or "Operation interrupted" in item["text"]
                           for item in api.sent)
        assert next(row for row in rows() if row["id"] == new["id"])["state"] == "serving"
        assert next(row for row in rows() if row["id"] == old["id"])["release_sha"] == shas[0]
        assert next(row for row in rows() if row["id"] == new["id"])["release_sha"] == shas[1]
        subprocess.run(["launchctl", "bootout", f"{domain}/{labels[0]}"], check=True, timeout=15)
        assert subprocess.run(["launchctl", "print", f"{domain}/{labels[0]}"],
                              capture_output=True, timeout=5).returncode != 0
        print(f"NATIVE_LAUNCHD overlap={transfer_seconds:.2f}s child_finish={child_seconds:.2f}s "
              f"child_delivery={completion_seconds:.2f}s cron_A_finish={cron_first_seconds:.2f}s "
              f"cron_B_next={cron_next_seconds:.2f}s approval={approval_seconds:.2f}s "
              f"B_reply={b_reply_seconds:.2f}s A_reply_wait={a_reply_seconds:.2f}s "
              f"followup={followup_seconds:.2f}s old_sha={shas[0]} new_sha={shas[1]} "
              f"max_pollers={api.maximum}")
    finally:
        for label in labels:
            subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, timeout=15)


@pytest.mark.integration
@pytest.mark.macos_only
@pytest.mark.live_system_guard_bypass
def test_launchd_guardian_rolls_back_keepalive_successor_failure(request, tmp_path):
    """The scheduled guardian fences a KeepAlive successor and restores A."""
    root = tmp_path / "guardian-failure"
    root.mkdir()
    home = root / "profile"
    home.mkdir()
    api = BotAPI()
    llm = FakeLLMServer(lambda record: (
        Text("old-turn-complete") if record["body"]["messages"][-1].get("role") == "tool"
        else ToolCall("terminal", {"command": f"touch {root / 'old-running'} && sleep 40"})
        if "old-boundary" in str(record["body"]["messages"][-1].get("content", ""))
        else Text("restored-a-answer")
    ))
    llm.__enter__()
    request.addfinalizer(api.close)
    request.addfinalizer(lambda: llm.__exit__(None, None, None))
    repo = Path(__file__).resolve().parents[2]
    python = Path(sys.executable)
    domain = f"gui/{os.getuid()}"
    nonce = uuid.uuid4().hex
    labels = [f"ai.hermes.p3test-{nonce}-{slot}" for slot in ("a", "b", "guardian")]
    shas = ("a" * 40, "b" * 40)
    paths = ReleasePaths.for_home(home)
    for sha in shas:
        release = paths.releases / sha
        release.mkdir(parents=True)
        for marker in (".release-ready", ".hermes_build_sha"):
            (release / marker).write_text(sha)
    paths.current.symlink_to(paths.releases / shas[0])
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: fake-model\n"
        f"  base_url: {llm.base_url}\n  key_env: OPENAI_API_KEY\n"
        "agent:\n  api_max_retries: 1\n"
        "approvals:\n  mode: off\nupdates:\n  check: false\n"
        "gateway:\n  overlap_handover:\n    enabled: true\n"
        "  guardian:\n    enabled: true\n"
        "platforms:\n  telegram:\n    enabled: true\n    token: '123456:LOCAL_STUB_ONLY'\n"
        f"    extra:\n      base_url: '{api.url}'\n      base_file_url: '{api.url}'\n"
        "      allow_from: ['1', '2', '3']\n      drop_pending_on_cold_boot: false\n")
    plists = {}
    for index, label in enumerate(labels[:2]):
        plist = root / f"{label}.plist"
        plist.write_bytes(plistlib.dumps({
            "Label": label, "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False}, "ExitTimeOut": 60,
            "ProgramArguments": [str(python), "-m", "hermes_cli.main", "gateway", "run"]
                                + (["--standby"] if index else []),
            "WorkingDirectory": str(repo),
            "EnvironmentVariables": {
                "HERMES_HOME": str(home), "PYTHONPATH": str(repo),
                "HERMES_RELEASE_SHA": shas[index], "HERMES_LAUNCHD_LABEL": label,
                "HERMES_GATEWAY_LOCK_DIR": str(root / "locks"),
                "OPENAI_API_KEY": "local-test-key", "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS": "1",
            },
            "StandardOutPath": str(root / f"{label}.out"),
            "StandardErrorPath": str(root / f"{label}.err"),
        }))
        register_disposable_label(request, label, plist)
        plists[label] = plist
    guardian_plist = root / f"{labels[2]}.plist"
    guardian_plist.write_bytes(plistlib.dumps({
        "Label": labels[2], "RunAtLoad": True, "StartInterval": 5,
        "KeepAlive": False,
        "ProgramArguments": [str(python), "-m", "hermes_cli.gateway_guardian", "run",
                             "--gateway-plist", str(plists[labels[1]]),
                             "--gateway-label", labels[1], "--domain", domain],
        "WorkingDirectory": str(repo),
        "EnvironmentVariables": {"HERMES_HOME": str(home), "PYTHONPATH": str(repo)},
        "StandardOutPath": str(root / "guardian.out"),
        "StandardErrorPath": str(root / "guardian.err"),
    }))
    register_disposable_label(request, labels[2], guardian_plist)

    def rows():
        return GenerationCoordinator(home).generations()

    def sent(needle):
        with api.lock:
            return [item for item in api.sent if needle in item["text"].replace("\\\\", "")]

    try:
        subprocess.run(["launchctl", "bootstrap", domain, str(plists[labels[0]])], check=True, timeout=15)
        _wait_for(lambda: any(row["label"] == labels[0] and row["state"] in {"serving", "ready"}
                           for row in rows()), 35, "A did not become ready")
        api.add(4001, 4001, text="old-boundary")
        _wait_for(lambda: (root / "old-running").exists(), 20, "A did not start in-flight work")
        subprocess.run(["launchctl", "bootstrap", domain, str(plists[labels[1]])], check=True, timeout=15)
        _wait_for(lambda: any(row["label"] == labels[1] and row["state"] == "ready" for row in rows()),
                  35, "B did not become ready")
        old, successor = (next(row for row in rows() if row["label"] == label)
                          for label in (labels[0], labels[1]))
        epoch = handover_to_generation(home, successor["id"], timeout=35)
        activate_release(home, paths.releases / shas[1])
        subprocess.run(["launchctl", "bootstrap", domain, str(guardian_plist)], check=True, timeout=15)
        failure_at = time.monotonic()
        os.kill(successor["pid"], signal.SIGKILL)
        _wait_for(lambda: any(json.loads(path.read_text())["outcome"] == "rolled_back"
                              for path in (home / "logs/guardian").glob("*.json")),
                  60, "scheduled guardian did not restore A after B failure")
        elapsed = time.monotonic() - failure_at
        lease = GenerationCoordinator(home).leases()[0]
        assert elapsed < 60
        assert lease["generation_id"] == old["id"] and lease["epoch"] == epoch + 1
        assert paths.current.resolve().name == shas[0]
        assert subprocess.run(["launchctl", "print", f"{domain}/{labels[1]}"],
                              capture_output=True, timeout=5).returncode != 0
        api.add(4002, 4002, text="new-a", chat_id=3)
        _wait_for(lambda: len(sent("restored-a-answer")) == 1, 20, "restored A did not answer fresh chat")
        assert time.monotonic() - failure_at < 60, "fresh restored answer exceeded the 60-second recovery bound"
        _wait_for(lambda: len(sent("old-turn-complete")) == 1, 20, "A in-flight result was lost")
        assert time.monotonic() - failure_at < 60, "in-flight result exceeded the 60-second recovery bound"
        with api.lock:
            assert api.maximum == 1 and not api.errors
            assert not any("Operation interrupted" in item["text"] for item in api.sent)
        assert len(sent("restored-a-answer")) == len(sent("old-turn-complete")) == 1
        print(f"NATIVE_LAUNCHD guardian_failure={elapsed:.2f}s max_pollers={api.maximum} "
              f"lease_epoch={lease['epoch']}", flush=True)
    finally:
        for label in labels:
            subprocess.run(["launchctl", "bootout", f"{domain}/{label}"],
                           capture_output=True, timeout=15)
