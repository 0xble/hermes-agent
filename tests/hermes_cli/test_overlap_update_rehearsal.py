"""Disposable generation boot entries and updater promotion invariants."""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from hermes_cli import gateway_overlap as overlap


@pytest.mark.integration
@pytest.mark.macos_only
@pytest.mark.live_system_guard_bypass
def test_updater_promotes_two_pinned_releases_with_native_long_turn(request, tmp_path, monkeypatch):
    """Real release interpreters and launchd jobs; only disposable labels are used."""
    import os
    import subprocess
    import time
    import uuid
    from gateway.generation import GenerationCoordinator
    from hermes_cli import update_cmd, gateway_guardian
    from hermes_cli.immutable_releases import ReleasePaths
    from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall
    from tests.hermes_cli.immutable_launchd_cleanup import register_disposable_label
    from tests.plugins.telegram_polling_stub import BotAPI
    repo = Path(__file__).resolve().parents[2]
    a = "f3dc8da3a94021277a1e46ea430dae88b7ccc5e0"
    b = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                       capture_output=True, text=True).stdout.strip()
    assert a != b
    home = tmp_path / "profile"
    home.mkdir()
    paths = ReleasePaths.for_home(home)
    build_start = time.monotonic()
    for sha in (a, b):
        release = paths.releases / sha
        release.mkdir(parents=True)
        archive = subprocess.run(["git", "archive", sha], cwd=repo, check=True,
                                 capture_output=True, timeout=90).stdout
        subprocess.run(["tar", "-xf", "-", "-C", str(release)], input=archive,
                       check=True, timeout=90)
        subprocess.run(["uv", "sync", "--frozen", "--extra", "messaging", "--no-dev"],
                       cwd=release, check=True, capture_output=True, timeout=240)
        for marker in (".release-ready", ".hermes_build_sha"):
            (release / marker).write_text(sha + "\n")
    build_seconds = time.monotonic() - build_start
    assert (paths.releases / a / ".venv/bin/python").is_file()
    assert (paths.releases / b / ".venv/bin/python").is_file()
    paths.current.symlink_to(paths.releases / a)
    api = BotAPI()
    request.addfinalizer(api.close)
    marker = tmp_path / "tool-running"
    import shlex
    def model(record):
        messages = record["body"]["messages"]
        if messages and messages[-1].get("role") == "tool":
            return Text("A-complete:" + a)
        user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        if "old-boundary" in str(user.get("content", "")):
            return ToolCall("terminal", {"command": f"touch {shlex.quote(str(marker))} && sleep 35"})
        return Text("B-complete:" + b)
    llm = FakeLLMServer(model)
    llm.__enter__()
    request.addfinalizer(lambda: llm.__exit__(None, None, None))
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: fake-model\n"
        f"  base_url: {llm.base_url}\n  key_env: OPENAI_API_KEY\n"
        "agent:\n  api_max_retries: 1\n"
        "approvals:\n  mode: 'off'\nupdates:\n  check: false\n  immutable_releases: true\n"
        "gateway:\n  overlap_handover:\n    enabled: true\n"
        "  durable_outbox:\n    enabled: true\n"
        "platforms:\n  telegram:\n    enabled: true\n    token: '123456:LOCAL_STUB_ONLY'\n"
        f"    extra:\n      base_url: '{api.url}'\n      base_file_url: '{api.url}'\n"
        "      allow_from: ['1', '2']\n      drop_pending_on_cold_boot: false\n")
    nonce = uuid.uuid4().hex
    old_label = f"ai.hermes.p3test-u-{nonce}-a"
    new_label = f"ai.hermes.p3test-u-{nonce}-b"
    domain = f"gui/{os.getuid()}"
    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    monkeypatch.setattr(overlap, "_launch_agents_dir", lambda: agents)
    monkeypatch.setattr(gateway_guardian, "_gateway_domain", lambda *_: domain)
    monkeypatch.setattr(gateway_guardian, "_launch_state", lambda *_: "unloaded")
    monkeypatch.setattr(overlap, "generation_launchd_label", lambda slot: new_label if slot == "b" else old_label)
    original_active = overlap._active_and_prior
    def active_for_disposable(home):
        coordinator, old, prior, epoch = original_active(home)
        assert old["label"] == old_label
        return coordinator, {**old, "label": "ai.hermes.gateway"}, prior, epoch
    monkeypatch.setattr(overlap, "_active_and_prior", active_for_disposable)
    def render(*, slot, release_sha, release_root, interpreter, hermes_home, standby=True):
        return plistlib.dumps({
            "Label": new_label, "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
            "ProgramArguments": [str(interpreter), "-m", "hermes_cli.main", "gateway", "run"] +
                                (["--standby"] if standby else []),
            "WorkingDirectory": str(release_root),
            "EnvironmentVariables": {"HERMES_HOME": str(hermes_home), "PYTHONPATH": str(release_root),
                "HERMES_RELEASE_SHA": release_sha, "HERMES_LAUNCHD_LABEL": new_label,
                "HERMES_GATEWAY_LOCK_DIR": str(tmp_path / "locks"),
                "OPENAI_API_KEY": "local-test-key", "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS": "1"},
            "StandardOutPath": str(tmp_path / "b.out"), "StandardErrorPath": str(tmp_path / "b.err"),
        }).decode()
    monkeypatch.setattr(overlap, "render_generation_launchd_plist", render)
    monkeypatch.setattr(overlap, "bootstrap_generation_plist", lambda **kw: subprocess.run(
        ["launchctl", "bootstrap", domain, str(kw["plist_path"])], check=True, timeout=15))
    a_plist = agents / f"{old_label}.plist"
    a_plist.write_bytes(plistlib.dumps({
        "Label": old_label, "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
        "ProgramArguments": [str(paths.releases / a / ".venv/bin/python"), "-m", "hermes_cli.main", "gateway", "run"],
        "WorkingDirectory": str(paths.releases / a),
        "EnvironmentVariables": {"HERMES_HOME": str(home), "PYTHONPATH": str(paths.releases / a),
            "HERMES_RELEASE_SHA": a, "HERMES_LAUNCHD_LABEL": old_label,
            "HERMES_GATEWAY_LOCK_DIR": str(tmp_path / "locks"),
            "OPENAI_API_KEY": "local-test-key", "HERMES_TELEGRAM_DISABLE_FALLBACK_IPS": "1"},
        "StandardOutPath": str(tmp_path / "a.out"), "StandardErrorPath": str(tmp_path / "a.err"),
    }))
    b_plist = agents / f"{new_label}.plist"
    for label, plist in ((old_label, a_plist), (new_label, b_plist)):
        register_disposable_label(request, label, plist)
    def wait(predicate, seconds, why):
        start = time.monotonic()
        while time.monotonic() - start < seconds:
            if predicate():
                return time.monotonic() - start
            time.sleep(.1)
        logs = []
        for name in ("a.err", "b.err"):
            path = tmp_path / name
            if path.exists():
                logs.append(f"{name}: {path.read_text(errors='replace')[-4000:]}")
        raise AssertionError(f"{why}: {' | '.join(logs)}")
    def sent(needle):
        with api.lock:
            return [item for item in api.sent if needle in item["text"].replace("\\", "")]
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_LAUNCHD_LABEL", "ai.hermes.gateway")
    from hermes_cli import update_receipt
    update_receipt.begin_update_receipt()
    try:
        subprocess.run(["launchctl", "bootstrap", domain, str(a_plist)], check=True, timeout=15)
        wait(lambda: len(GenerationCoordinator(home).generations()) == 1, 30, "A not ready")
        api.add(1001, 1001, text="old-boundary")
        wait(marker.exists, 30, "A long tool did not start")
        import concurrent.futures
        started = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(update_cmd._activate_immutable_release, sha=b, source=repo)
            wait(lambda: len(GenerationCoordinator(home).generations()) == 2 and
                 GenerationCoordinator(home).leases()[0]["generation_id"] !=
                 GenerationCoordinator(home).generations()[0]["id"], 60, "B did not take lease")
            api.add(1002, 1002, text="new-boundary", chat_id=2)
            b_reply_seconds = wait(lambda: len(sent("B-complete:" + b)) == 1, 30, "B did not reply")
            assert (paths.releases / a).exists()
            assert subprocess.run(["launchctl", "print", f"{domain}/{old_label}"],
                                  capture_output=True, timeout=5).returncode == 0
            result = future.result(timeout=60)
            if not result:
                import sqlite3
                with GenerationCoordinator(home).connect() as db:
                    print("INBOX", [dict(row) for row in db.execute("SELECT owner_id,owner_epoch,state,source_event_id,authorized_source FROM inbox")], flush=True)
                    print("JOURNAL", [dict(row) for row in db.execute("SELECT update_id,state,received_at FROM telegram_updates")], flush=True)
                print("AFTER", update_receipt.current_overlap_generation(), flush=True)
                outbox_path = home / "gateway-outbox.db"
                if outbox_path.exists():
                    with sqlite3.connect(outbox_path) as db:
                        print("ADMISSIONS", db.execute("SELECT profile,platform,transport_event_id,event_kind,turn_id,result FROM admissions").fetchall(), flush=True)
                        print("OUTBOX", db.execute("SELECT turn_id,state,message_id,owner_epoch FROM outbox").fetchall(), flush=True)
            assert result is True
        promote_seconds = time.monotonic() - started
        a_reply_seconds = wait(lambda: len(sent("A-complete:" + a)) == 1, 70, "A did not finish")
        rows = GenerationCoordinator(home).generations()
        old = next(row for row in rows if row["release_sha"] == a)
        wait(lambda: next(row for row in GenerationCoordinator(home).generations()
             if row["id"] == old["id"])["state"] == "exited", 20, "A did not exit")
        old_job = subprocess.run(["launchctl", "print", f"{domain}/{old_label}"],
                                 capture_output=True, text=True, timeout=5)
        assert "last exit code = 0" in old_job.stdout
        assert (paths.releases / a).exists() and paths.current.resolve().name == b
        subprocess.run(["launchctl", "bootout", f"{domain}/{old_label}"], check=True, timeout=15)
        assert subprocess.run(["launchctl", "print", f"{domain}/{old_label}"],
                              capture_output=True, timeout=5).returncode != 0
        with api.lock:
            assert api.maximum == 1 and not api.errors
            assert not any("⏳ Gateway" in item["text"] or "Operation interrupted" in item["text"]
                           for item in api.sent)
        assert len(sent("A-complete:" + a)) == len(sent("B-complete:" + b)) == 1
        from hermes_cli.update_receipt import current_overlap_generation
        proof = current_overlap_generation()
        assert proof and proof["admission"]["generation_id"] != old["id"]
        monkeypatch.setattr(overlap, "_active_and_prior", original_active)
        assert overlap.verified_overlap(home, proof)
        print(f"UPDATER_NATIVE build={build_seconds:.2f}s promote={promote_seconds:.2f}s "
              f"B_reply={b_reply_seconds:.2f}s A_reply_wait={a_reply_seconds:.2f}s "
              f"max_pollers={api.maximum}", flush=True)
    finally:
        update_receipt.detach_update_receipt()
        for label in (old_label, new_label):
            subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, timeout=15)


def test_installed_generation_survives_simulated_login(tmp_path, monkeypatch):
    agents = tmp_path / "Library" / "LaunchAgents"
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setattr(overlap, "_launch_agents_dir", lambda: agents)
    label = "ai.hermes.gateway-b"
    body = plistlib.dumps({"Label": label, "EnvironmentVariables": {
        "HERMES_HOME": str(home.resolve()), "HERMES_RELEASE_SHA": "b" * 40,
    }}).decode()
    installed = overlap._install_generation_plist(home, label, body)
    launches = []
    monkeypatch.setattr(overlap, "bootstrap_generation_plist", lambda **kw: launches.append(kw))
    # No resident process survives logout; the only boot input is LaunchAgents.
    for entry in agents.glob("*.plist"):
        plist = plistlib.loads(entry.read_bytes())
        overlap.bootstrap_generation_plist(domain="gui/501", plist_path=entry, label=plist["Label"])
    assert launches == [{"domain": "gui/501", "plist_path": installed, "label": label}]
    assert plistlib.loads(installed.read_bytes())["EnvironmentVariables"]["HERMES_RELEASE_SHA"] == "b" * 40
    foreign = plistlib.dumps({"Label": label, "EnvironmentVariables": {
        "HERMES_HOME": str(tmp_path / "other")}}).decode()
    installed.write_text(foreign)
    with pytest.raises(RuntimeError, match="another installation"):
        overlap._install_generation_plist(home, label, body)


def test_promotion_keeps_old_pointer_until_handover_commits(tmp_path, monkeypatch):
    """The running old release remains the cold-boot target while handover is pending."""
    from gateway.generation import GenerationCoordinator, GenerationIdentity
    from gateway.status import _get_process_start_time
    from dataclasses import asdict
    import os
    home = tmp_path / "profile"
    home.mkdir()
    paths = home / "releases"
    paths.mkdir()
    a, b = "a" * 40, "b" * 40
    for sha in (a, b):
        release = paths / sha
        release.mkdir()
        for name in (".release-ready", ".hermes_build_sha"):
            (release / name).write_text(sha)
    (home / "current").symlink_to(paths / a)
    identity = GenerationIdentity.create(release_sha=a, label="ai.hermes.gateway",
        start_fingerprint=f"{os.getpid()}:{_get_process_start_time(os.getpid())}")
    successor = GenerationIdentity.create(release_sha=b, label="ai.hermes.gateway-b")
    coordinator = GenerationCoordinator(home)
    coordinator.register(identity, state="serving")
    coordinator.register(successor, state="ready")
    epoch = coordinator.acquire_lease("active_generation", identity.id)
    monkeypatch.setattr(overlap, "_active_and_prior", lambda _home: (coordinator, asdict(identity), None, epoch))
    monkeypatch.setattr("hermes_cli.gateway_guardian._gateway_domain", lambda *_: "gui/501")
    monkeypatch.setattr("hermes_cli.gateway_guardian._launch_state", lambda *_: "unloaded")
    agents = tmp_path / "Library" / "LaunchAgents"
    monkeypatch.setattr(overlap, "_launch_agents_dir", lambda: agents)
    def render(*, slot, release_sha, hermes_home, standby=True, **_):
        return plistlib.dumps({"Label": f"ai.hermes.gateway-{slot}",
            "EnvironmentVariables": {"HERMES_HOME": str(hermes_home.resolve()),
                                     "HERMES_RELEASE_SHA": release_sha},
            "RunAtLoad": True, "ProgramArguments": ["python", "gateway", "run"] +
                (["--standby"] if standby else [])}).decode()
    monkeypatch.setattr(overlap, "render_generation_launchd_plist", render)
    overlap._install_generation_plist(home, identity.label, plistlib.dumps({
        "Label": identity.label, "EnvironmentVariables": {"HERMES_HOME": str(home.resolve())},
        "RunAtLoad": True, "ProgramArguments": ["python", "gateway", "run"],
    }).decode())
    monkeypatch.setattr(overlap, "bootstrap_generation_plist", lambda **_: None)
    monkeypatch.setattr(overlap, "_ready_successor", lambda *_args, **_kw: asdict(successor))
    def transfer(*_args, **_kw):
        assert (home / "current").resolve().name == a
        coordinator.request_transfer(identity.id, successor.id, epoch, set())
        return coordinator.commit_transfer(identity.id, successor.id, epoch)
    monkeypatch.setattr(overlap, "handover_to_generation", transfer)
    monkeypatch.setattr(overlap, "_observe_poller", lambda *_args: {"polling": True, "tokens": ["token"]})
    result = overlap.promote_overlap(home, paths / b, b)
    assert result["epoch"] > epoch
    assert (home / "current").resolve().name == b
    assert (home / "previous").resolve().name == a
    boot_entries = [plistlib.loads(entry.read_bytes()) for entry in agents.glob("*.plist")]
    eligible = [entry for entry in boot_entries if entry["RunAtLoad"]]
    assert len(eligible) == 1 and eligible[0]["Label"] == "ai.hermes.gateway-b"
    assert eligible[0]["EnvironmentVariables"]["HERMES_RELEASE_SHA"] == b
    assert "--standby" not in eligible[0]["ProgramArguments"]


def test_admission_proof_requires_fresh_accepted_update_and_delivered_receipt(tmp_path):
    import time
    from gateway.generation import GenerationCoordinator, GenerationIdentity
    from gateway.outbox import Outbox
    home = tmp_path / "profile"
    home.mkdir()
    coordinator = GenerationCoordinator(home)
    successor = GenerationIdentity.create(release_sha="b" * 40, label="ai.hermes.gateway-b")
    coordinator.register(successor, state="serving")
    epoch = coordinator.acquire_lease("active_generation", successor.id)
    after = time.time()
    with coordinator.connect() as db:
        db.execute("CREATE TABLE telegram_updates (token_hash TEXT,update_id INTEGER,raw_update BLOB,"
                   "state TEXT,received_at REAL)")
        db.execute("INSERT INTO telegram_updates VALUES (?,?,?,?,?)",
                   ("token", 2002, b"{}", "accepted", after + 1))
    outbox = Outbox(home)
    turn, fresh = outbox.admit("default", "telegram", "update:2002", "text")
    assert fresh
    outbox.enqueue(turn, "send", {"chat_id": "2", "content": "B answered"})
    with pytest.raises(RuntimeError, match="delivered reply"):
        overlap._observe_admission(home, successor.id, epoch, after=after, timeout=.01)
    with outbox._connect() as db:
        db.execute("UPDATE outbox SET state='delivered',message_id='telegram:42' WHERE turn_id=?", (turn,))
    proof = overlap._observe_admission(home, successor.id, epoch, after=after, timeout=.2)
    assert proof == {"generation_id": successor.id, "epoch": epoch,
                     "source_event_id": "2002", "turn_id": turn, "message_id": "telegram:42"}
    with pytest.raises(RuntimeError, match="delivered reply"):
        overlap._observe_admission(home, successor.id, epoch, after=after + 2, timeout=.01)
    with coordinator.connect() as db:
        db.execute("UPDATE telegram_updates SET state='quarantined' WHERE update_id=2002")
    with pytest.raises(RuntimeError, match="delivered reply"):
        overlap._observe_admission(home, successor.id, epoch, after=after, timeout=.01)


def test_failed_successor_readiness_boots_out_disposable_label(tmp_path, monkeypatch):
    from gateway.generation import GenerationCoordinator, GenerationIdentity
    from gateway.status import _get_process_start_time
    from dataclasses import asdict
    import os
    home = tmp_path / "profile"
    home.mkdir()
    sha = "a" * 40
    release = home / "releases" / sha
    release.mkdir(parents=True)
    for name in (".release-ready", ".hermes_build_sha"):
        (release / name).write_text(sha)
    (home / "current").symlink_to(release)
    old = GenerationIdentity.create(release_sha=sha, label="ai.hermes.gateway",
        start_fingerprint=f"{os.getpid()}:{_get_process_start_time(os.getpid())}")
    coordinator = GenerationCoordinator(home)
    coordinator.register(old, state="serving")
    epoch = coordinator.acquire_lease("active_generation", old.id)
    monkeypatch.setattr(overlap, "_active_and_prior", lambda _: (coordinator, asdict(old), None, epoch))
    monkeypatch.setattr("hermes_cli.gateway_guardian._gateway_domain", lambda *_: "gui/501")
    monkeypatch.setattr("hermes_cli.gateway_guardian._launch_state", lambda *_: "unloaded")
    monkeypatch.setattr(overlap, "render_generation_launchd_plist", lambda **_: "body")
    monkeypatch.setattr(overlap, "_install_generation_plist", lambda *_: tmp_path / "disposable.plist")
    monkeypatch.setattr(overlap, "_set_boot_active", lambda *_: None)
    monkeypatch.setattr(overlap, "bootstrap_generation_plist", lambda **_: None)
    monkeypatch.setattr(overlap, "_ready_successor", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("not ready")))
    bootouts = []
    monkeypatch.setattr(overlap, "_bootout_generation", lambda *args: bootouts.append(args))
    with pytest.raises(RuntimeError, match="not ready"):
        overlap.promote_overlap(home, home / "releases" / ("b" * 40), "b" * 40)
    assert bootouts == [("gui/501", "ai.hermes.gateway-b")]
    assert (home / "current").resolve() == release
