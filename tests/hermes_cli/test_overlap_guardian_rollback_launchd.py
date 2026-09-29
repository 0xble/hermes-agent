"""Rollback supervision regressions for the disposable overlap coordinator."""
from __future__ import annotations

import os
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from gateway.generation import GenerationCoordinator, GenerationIdentity
from gateway.status import _get_process_start_time
from hermes_cli import gateway_guardian as guardian, gateway_overlap as overlap


def _committed(home: Path):
    coordinator = GenerationCoordinator(home)
    pid = os.getpid()
    fingerprint = f"{pid}:{_get_process_start_time(pid)}"
    a = GenerationIdentity.create(release_sha="a" * 40, label="ai.hermes.gateway-a",
                                  pid=pid, start_fingerprint=fingerprint)
    b = GenerationIdentity.create(release_sha="b" * 40, label="ai.hermes.gateway-b",
                                  pid=pid, start_fingerprint=fingerprint)
    coordinator.register(a, state="serving")
    coordinator.register(b, state="ready")
    epoch = coordinator.acquire_lease("active_generation", a.id)
    coordinator.request_transfer(a.id, b.id, epoch, set())
    promoted = coordinator.commit_transfer(a.id, b.id, epoch)
    return coordinator, a, b, promoted


@pytest.mark.macos_only
def test_late_poller_failure_remains_eligible_for_guarded_rollback(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    coordinator, a, b, epoch = _committed(home)
    with coordinator.connect() as conn:
        conn.execute("UPDATE generations SET transferred_at=? WHERE id=?", (time.time() - 75, a.id))
    from gateway.generation import generation_paths
    from gateway.run_generation import _generation_request
    # The real control endpoint is unavailable: guardian must attempt a rollback,
    # whose wire-stop proof rejects the operation without changing the lease.
    monkeypatch.setattr(guardian, "_gateway_domain", lambda *args: f"gui/{os.getuid()}")
    monkeypatch.setattr(guardian, "_launch_state", lambda *args: "unloaded")
    outcome = guardian._run_overlap(home)
    assert outcome == "alert"
    reasons = [p.read_text() for p in (home / "logs/guardian").glob("*.json")]
    assert any("successor" in reason or "rollback" in reason for reason in reasons)
    assert coordinator.leases()[0]["generation_id"] == b.id


@pytest.mark.macos_only
def test_rollback_refusal_rearms_stopped_successor(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    coordinator, a, b, epoch = _committed(home)
    releases = home / "releases"
    for identity in (a, b):
        release = releases / identity.release_sha
        release.mkdir(parents=True)
        for marker in (".release-ready", ".hermes_build_sha"):
            (release / marker).write_text(identity.release_sha)
    (home / "current").symlink_to(releases / b.release_sha)
    calls = []

    def request(path, verb, *, params=None, timeout=0):
        calls.append((verb, params))
        if verb == "stop_for_rollback":
            return {"generation_id": b.id, "epoch": epoch, "poller_stopped": True}
        if verb == "resume_uncommitted_transfer":
            assert coordinator.leases()[0]["generation_id"] == b.id
            return {"generation_id": b.id, "epoch": epoch, "polling": True}
        raise AssertionError(verb)

    monkeypatch.setattr(overlap, "_generation_request", request)
    def refuse(*args, **kwargs):
        raise RuntimeError("storage refused transfer")

    monkeypatch.setattr(GenerationCoordinator, "rollback_transfer", refuse)
    with pytest.raises(RuntimeError, match="storage refused transfer"):
        overlap.rollback_overlap(home, b.id, a.id, epoch)
    assert calls[-1] == ("resume_uncommitted_transfer", {"epoch": epoch})


@pytest.mark.macos_only
def test_unready_successor_is_booted_out_before_next_promotion(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    coordinator = GenerationCoordinator(home)
    pid = os.getpid()
    old = GenerationIdentity.create(release_sha="a" * 40, label="ai.hermes.gateway-a",
                                    pid=pid, start_fingerprint=f"{pid}:{_get_process_start_time(pid)}")
    coordinator.register(old, state="serving")
    epoch = coordinator.acquire_lease("active_generation", old.id)
    releases = home / "releases"
    for sha in (old.release_sha, "b" * 40):
        release = releases / sha
        release.mkdir(parents=True)
        for marker in (".release-ready", ".hermes_build_sha"):
            (release / marker).write_text(sha)
    (home / "current").symlink_to(releases / old.release_sha)
    monkeypatch.setattr(guardian, "_gateway_domain", lambda *args: f"gui/{os.getuid()}")
    monkeypatch.setattr(guardian, "_launch_state", lambda *args: "unloaded")
    monkeypatch.setattr(overlap, "render_generation_launchd_plist", lambda **kwargs: "test")
    monkeypatch.setattr(overlap, "bootstrap_generation_plist", lambda **kwargs: None)
    monkeypatch.setattr(overlap, "_ready_successor", lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("pinned standby did not report a live ready identity")))
    bootouts = []
    def launchctl(args, **kwargs):
        bootouts.append(args)
        return type("Result", (), {"returncode": 0})()
    monkeypatch.setattr(overlap.subprocess, "run", launchctl)
    with pytest.raises(RuntimeError, match="pinned standby"):
        overlap.promote_overlap(home, releases / ("b" * 40), "b" * 40)
    assert bootouts == [["launchctl", "bootout", f"gui/{os.getuid()}/ai.hermes.gateway-b"]]
    assert coordinator.leases()[0]["generation_id"] == old.id
    assert (home / "current").resolve().name == old.release_sha


@pytest.mark.integration
@pytest.mark.macos_only
def test_guardian_restores_drainer_after_successor_poller_stops(tmp_path):
    """Real gateway processes and Bot API, through the unpatched guardian tick."""
    import shlex
    import subprocess
    import sys
    from gateway.run_generation import _generation_request, handover_to_generation
    from hermes_cli.immutable_releases import ReleasePaths, activate_release
    from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
    from tests.plugins.telegram_polling_stub import BotAPI

    home = tmp_path / "profile"
    home.mkdir()
    api = BotAPI()
    marker_a, marker_b = tmp_path / "a-running", tmp_path / "b-running"
    def model(record):
        messages = record["body"]["messages"]
        user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        content = str(user.get("content", ""))
        if messages and messages[-1].get("role") == "tool":
            return Text("a-finished" if "long-a" in content else "b-finished")
        if "long-a" in content:
            return ToolCall("terminal", {"command": f"touch {shlex.quote(str(marker_a))} && sleep 55"})
        if "long-b" in content:
            return ToolCall("terminal", {"command": f"touch {shlex.quote(str(marker_b))} && sleep 65"})
        return Text("restored-a-answer")
    llm = FakeLLMServer(model)
    llm.__enter__()
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: fake-model\n"
        f"  base_url: {llm.base_url}\n  key_env: OPENAI_API_KEY\n"
        "agent:\n  api_max_retries: 1\n"
        "approvals:\n  mode: 'off'\nupdates:\n  check: false\n"
        "gateway:\n  overlap_handover:\n    enabled: true\n"
        "platforms:\n  telegram:\n    enabled: true\n    token: '123456:LOCAL_STUB_ONLY'\n"
        f"    extra:\n      base_url: '{api.url}'\n      base_file_url: '{api.url}'\n"
        "      allow_from: ['1', '2', '3']\n      drop_pending_on_cold_boot: false\n")
    from gateway.generation import generation_paths
    paths = ReleasePaths.for_home(home)
    shas = ["a" * 40, "b" * 40]
    for sha in shas:
        release = paths.releases / sha
        release.mkdir(parents=True)
        for filename in (".release-ready", ".hermes_build_sha"):
            (release / filename).write_text(sha)
    paths.current.symlink_to(paths.releases / shas[0])
    repo = Path(__file__).resolve().parents[2]
    worker = repo / "tests/gateway/test_generation_promotion_process.py"
    processes = []
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(repo),
           "HERMES_GATEWAY_LOCK_DIR": str(tmp_path / "locks"),
           "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS": "1", "OPENAI_API_KEY": "local-test-key"}
    def rows():
        return GenerationCoordinator(home).generations()
    def sent(text):
        with api.lock:
            return [entry for entry in api.sent if text in entry["text"].replace("\\", "")]
    def wait(predicate, timeout, reason):
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if predicate():
                return time.monotonic() - start
            time.sleep(.15)
        raise AssertionError(reason)
    try:
        for slot in "ab":
            proc = subprocess.Popen([sys.executable, str(worker), "worker",
                                     "active" if slot == "a" else "standby"],
                                    env={**env, "HERMES_LAUNCHD_LABEL": f"ai.hermes.gateway-{slot}",
                                         "HERMES_RELEASE_SHA": shas[0 if slot == "a" else 1]},
                                    cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            processes.append(proc)
            wait(lambda: len(rows()) == len(processes) and all(r["state"] == "ready" for r in rows()),
                 30, f"{slot} not ready")
            if slot == "a":
                api.add(1001, 1001, text="long-a")
                wait(marker_a.exists, 20, "A did not start a long turn")
        a, b = rows()
        epoch = handover_to_generation(home, b["id"], timeout=35)
        activate_release(home, paths.releases / shas[1])
        api.add(1002, 1002, text="long-b", chat_id=2)
        wait(marker_b.exists, 20, "B did not start a long turn")
        identity_b = GenerationIdentity(**{key: b[key] for key in GenerationIdentity.__dataclass_fields__})
        stopped = _generation_request(generation_paths(home, identity_b)["socket"],
                                      "stop_for_rollback", timeout=15)
        assert stopped["poller_stopped"] is True
        failure_at = time.monotonic()
        def restored():
            return guardian.run_once(home, home / "unused.plist", a["label"]) == "rolled_back"
        recovery = wait(restored, 50, "guardian did not roll back B's stopped poller")
        assert recovery < 60
        lease = GenerationCoordinator(home).leases()[0]
        assert lease["generation_id"] == a["id"] and lease["epoch"] == epoch + 1
        assert paths.current.resolve().name == shas[0]
        api.add(1003, 1003, text="new-a", chat_id=3)
        wait(lambda: len(sent("restored-a-answer")) == 1, 20, "A did not answer fresh chat")
        wait(lambda: len(sent("a-finished")) == 1, 80, "A did not finish in-flight turn")
        with api.lock:
            assert api.maximum <= 1 and not api.errors
        assert len(sent("restored-a-answer")) == len(sent("a-finished")) == 1
        print(f"GUARDIAN_PROBE recovery={recovery:.2f}s max_pollers={api.maximum} "
              f"lease_epoch={lease['epoch']} elapsed={time.monotonic() - failure_at:.2f}s", flush=True)
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=8)
        llm.__exit__(None, None, None)
        api.close()
