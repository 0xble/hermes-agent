"""Native launchd overlap rehearsal with disposable labels and a local Bot API."""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from gateway.generation import GenerationCoordinator
from gateway.run_generation import handover_to_generation
from hermes_cli.gateway_overlap import rollback_overlap
from hermes_cli.immutable_releases import ReleasePaths, activate_release
from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
from tests.hermes_cli.immutable_launchd_cleanup import register_disposable_label, sweep_prior_sessions
from tests.plugins.telegram_polling_stub import BotAPI


@pytest.fixture(scope="module", autouse=True)
def _sweep(request):
    sweep_prior_sessions(request)


def _wait_for(predicate, seconds: float, message: str) -> float:
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        if predicate():
            return time.monotonic() - start
        time.sleep(.15)
    raise AssertionError(message)


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
    api = BotAPI()
    request.addfinalizer(api.close)
    import shlex
    command = f"touch {shlex.quote(str(marker))} && sleep 60"

    def model(record):
        messages = record["body"]["messages"]
        if messages and messages[-1].get("role") == "tool":
            return Text("old-turn-complete")
        user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        if "old-boundary" in str(user.get("content", "")):
            return ToolCall("terminal", {"command": command})
        return Text("new-turn-complete")

    llm = FakeLLMServer(model)
    llm.__enter__()
    request.addfinalizer(lambda: llm.__exit__(None, None, None))
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: fake-model\n"
        f"  base_url: {llm.base_url}\n  key_env: OPENAI_API_KEY\n"
        "agent:\n  api_max_retries: 1\n"
        "approvals:\n  mode: 'off'\n"
        "updates:\n  check: false\n"
        "gateway:\n  overlap_handover:\n    enabled: true\n"
        "platforms:\n  telegram:\n    enabled: true\n    token: '123456:LOCAL_STUB_ONLY'\n"
        f"    extra:\n      base_url: '{api.url}'\n      base_file_url: '{api.url}'\n"
        "      allow_from: ['1', '2']\n      drop_pending_on_cold_boot: false\n")
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
            _wait_for(lambda: len(rows()) == index + 1 and all(row["state"] == "ready" for row in rows()),
                      35, f"generation {index} not ready; log: {root / f'{labels[index]}.err'}")
            if not index:
                api.add(1001, 1001, text="old-boundary")
                _wait_for(marker.exists, 25, "A did not start the long tool")
        old, new = rows()
        assert [old["release_sha"], new["release_sha"]] == shas
        assert [old["label"], new["label"]] == labels
        transfer_start = time.monotonic()
        epoch = handover_to_generation(home, new["id"], timeout=35)
        transfer_seconds = time.monotonic() - transfer_start
        assert epoch > 1
        assert old["state"] == "ready" and marker.exists()
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
        api.add(1002, 1002, text="new-boundary", chat_id=2)
        b_reply_seconds = _wait_for(lambda: len(sent("new-turn-complete")) == 1, 25,
                                    "B did not answer a fresh session")
        a_reply_seconds = _wait_for(lambda: len(sent("old-turn-complete")) == 1, 90,
                                    "A's long tool did not complete")
        _wait_for(lambda: next(row for row in rows() if row["id"] == old["id"])["state"] == "exited",
                  20, "A did not exit after draining")
        old_job = subprocess.run(["launchctl", "print", f"{domain}/{labels[0]}"],
                                 capture_output=True, text=True, timeout=5)
        assert old_job.returncode == 0 and "last exit code = 0" in old_job.stdout, old_job.stdout[-3000:]
        assert len(sent("new-turn-complete")) == len(sent("old-turn-complete")) == 1
        with api.lock:
            assert api.maximum == 1 and not api.errors
            assert not any("⏳ Gateway" in item["text"] or "Operation interrupted" in item["text"]
                           for item in api.sent)
        assert next(row for row in rows() if row["id"] == new["id"])["state"] == "ready"
        assert next(row for row in rows() if row["id"] == old["id"])["release_sha"] == shas[0]
        assert next(row for row in rows() if row["id"] == new["id"])["release_sha"] == shas[1]
        subprocess.run(["launchctl", "bootout", f"{domain}/{labels[0]}"], check=True, timeout=15)
        assert subprocess.run(["launchctl", "print", f"{domain}/{labels[0]}"],
                              capture_output=True, timeout=5).returncode != 0
        print(f"NATIVE_LAUNCHD overlap={transfer_seconds:.2f}s B_reply={b_reply_seconds:.2f}s "
              f"A_reply_wait={a_reply_seconds:.2f}s old_sha={shas[0]} new_sha={shas[1]} "
              f"max_pollers={api.maximum}")
    finally:
        for label in labels:
            subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, timeout=15)
