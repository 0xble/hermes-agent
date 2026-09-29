"""Disposable generation boot entries and updater promotion invariants."""
from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from hermes_cli import gateway_overlap as overlap


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


def test_admission_proof_requires_successor_authorization_and_delivered_receipt(tmp_path):
    import json
    from gateway.generation import GenerationCoordinator, GenerationIdentity
    from gateway.outbox import Outbox
    home = tmp_path / "profile"
    home.mkdir()
    coordinator = GenerationCoordinator(home)
    successor = GenerationIdentity.create(release_sha="b" * 40, label="ai.hermes.gateway-b")
    coordinator.register(successor, state="serving")
    epoch = coordinator.acquire_lease("active_generation", successor.id)
    source = json.dumps({"version": 1, "authorized": True, "sender": "2", "home": str(home)}).encode()
    row, fresh = coordinator.enqueue(str(home), "telegram", "chat:new", "2002", "message",
                                     source, b"{}", successor.id, epoch)
    assert fresh
    assert coordinator.disposition(row["id"], successor.id, epoch, "accepted")
    outbox = Outbox(home)
    turn, fresh = outbox.admit("default", "telegram", "update:2002", "message")
    assert fresh
    outbox.enqueue(turn, "send", {"chat_id": "2", "content": "B answered"})
    with pytest.raises(RuntimeError, match="delivered reply"):
        overlap._observe_admission(home, successor.id, epoch, timeout=.01)
    with outbox._connect() as db:
        db.execute("UPDATE outbox SET state='delivered',message_id='telegram:42' WHERE turn_id=?", (turn,))
    proof = overlap._observe_admission(home, successor.id, epoch, timeout=.2)
    assert proof == {"generation_id": successor.id, "epoch": epoch,
                     "source_event_id": "2002", "turn_id": turn, "message_id": "telegram:42"}
    with coordinator.connect() as db:
        db.execute("UPDATE inbox SET authorized_source=? WHERE id=?",
                   (json.dumps({"authorized": False, "home": str(home)}).encode(), row["id"]))
    with pytest.raises(RuntimeError, match="delivered reply"):
        overlap._observe_admission(home, successor.id, epoch, timeout=.01)


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
