"""Guardian self-heal, the planned-reload fence and the out-of-band alert.

Regression context: on 2026-10-05 a guardian tick landed in a planned restart's reload window,
deleted the service plist and could only alert into a log nobody reads. Every launchctl, Bot API
and service-file call here is a fake; nothing touches a live launchd domain or Telegram chat.
"""
import json
import os
import plistlib
import subprocess
import time
from types import SimpleNamespace

import pytest

from hermes_cli import gateway, gateway_guardian as guardian, gateway_guardian_alert as alert
from hermes_cli import gateway_launchd
from hermes_cli.gateway_launchd_records import reload_pending, write_reload_pending


def service_layout(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    label = "ai.hermes.gateway"
    plist = tmp_path / "LaunchAgents" / f"{label}.plist"
    current = home / "releases" / ("b" * 40)
    (current / ".venv/bin").mkdir(parents=True)
    (current / ".venv/bin/python").write_text("fixture", encoding="utf-8")
    for marker in (".release-ready", ".hermes_build_sha"):
        (current / marker).write_text(current.name + "\n", encoding="utf-8")
    (home / "current").symlink_to(current)
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: label)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: plist)
    # Install's temp-home refusal and launcher publication guard real installs, not this fixture.
    monkeypatch.setattr(gateway, "_refuse_temp_home_service_write", lambda *a: False)
    monkeypatch.setattr(gateway, "_prepare_service_launcher", lambda **k: None)
    sent = []
    monkeypatch.setattr(alert, "notify", lambda home_arg, payload: sent.append(payload) or "sent")
    return SimpleNamespace(home=home, label=label, plist=plist, current=current, sent=sent)


def launchctl(monkeypatch):
    state = {"loaded": False, "calls": []}
    def run(argv, **kwargs):
        state["calls"].append(argv)
        if argv[1] == "print":
            loaded = state["loaded"] and argv[2].startswith(f"gui/{os.getuid()}/")
            return subprocess.CompletedProcess(argv, 0 if loaded else 113, stdout="pid = 123\n" if loaded else "",
                                               stderr="" if loaded else "Could not find service")
        if argv[1] == "bootstrap":
            state["loaded"] = True
        return subprocess.CompletedProcess(argv, 0, stdout="Aqua", stderr="")
    monkeypatch.setattr(guardian.subprocess, "run", run)
    return state


def receipts(home):
    return [json.loads(path.read_text()) for path in sorted((home / "logs/guardian").glob("*.json"))]


@pytest.mark.platforms("macos")
def test_missing_service_plist_is_regenerated_bootstrapped_and_capped(tmp_path, monkeypatch):
    rig = service_layout(tmp_path, monkeypatch)
    state = launchctl(monkeypatch)
    monkeypatch.setattr(guardian, "healthy", lambda *a, **k: state["loaded"])

    assert guardian.run_once(rig.home, rig.plist, rig.label, grace=180) == "repaired"
    definition = plistlib.loads(rig.plist.read_bytes())
    assert definition["Label"] == rig.label
    assert definition["EnvironmentVariables"]["HERMES_HOME"] == str(rig.home.resolve())
    assert [argv[1] for argv in state["calls"]].count("bootstrap") == 1
    assert ("regenerate", "written") in {(row["action"], row["outcome"]) for row in receipts(rig.home)}

    # Three regenerations per hour, then one capped alert, never another write or bootstrap.
    for _ in range(2):
        rig.plist.unlink()
        state["loaded"] = False
        assert guardian.run_once(rig.home, rig.plist, rig.label, grace=180) == "repaired"
    rig.plist.unlink()
    state["loaded"] = False
    assert guardian.run_once(rig.home, rig.plist, rig.label, grace=180) == "capped"
    assert guardian.run_once(rig.home, rig.plist, rig.label, grace=180) == "capped"
    assert not rig.plist.exists()
    assert [argv[1] for argv in state["calls"]].count("bootstrap") == 3
    assert [(p["action"], p["outcome"]) for p in rig.sent] == [("regenerate", "capped")]


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("fence", ["stopped", "reload_pending"])
def test_stop_intent_and_pending_reload_fence_every_repair(tmp_path, monkeypatch, fence):
    rig = service_layout(tmp_path, monkeypatch)
    state = launchctl(monkeypatch)
    if fence == "stopped":
        guardian.set_intent(rig.home, stopped=True)
    else:
        write_reload_pending(rig.home, label=rig.label, generation_id="g-old", seconds=60)
    expected = "stopped" if fence == "stopped" else "waiting"
    assert guardian.run_once(rig.home, rig.plist, rig.label, grace=180) == expected
    assert not rig.plist.exists() and state["calls"] == [] and rig.sent == []


def test_expired_or_cleared_reload_record_no_longer_fences(tmp_path):
    from hermes_cli.gateway_launchd_records import clear_reload_pending
    nonce = write_reload_pending(tmp_path, label="ai.hermes.gateway", generation_id=None, seconds=60)
    clear_reload_pending(tmp_path, "someone-else")
    assert reload_pending(tmp_path)["nonce"] == nonce
    clear_reload_pending(tmp_path, nonce)
    assert reload_pending(tmp_path) is None
    write_reload_pending(tmp_path, label="ai.hermes.gateway", generation_id=None, seconds=-1)
    assert reload_pending(tmp_path) is None


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("registers", [True, False])
def test_reload_helper_logs_bootstrap_failures_and_clears_its_fence(tmp_path, monkeypatch, registers):
    """Run the real generated helper against a fake launchctl: no host launchd is touched."""
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    plist = tmp_path / "ai.hermes.disposable.plist"
    plist.write_bytes(plistlib.dumps({"Label": plist.stem, "EnvironmentVariables": {"HERMES_HOME": str(home)}}))
    monkeypatch.setattr(gateway_launchd, "_launchd_reload_budget", lambda: 1)
    captured = {}
    real_run = subprocess.run
    def submit(argv, **kwargs):
        assert argv[:2] == ["launchctl", "submit"]
        captured["script"] = argv[-1]
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(gateway_launchd.subprocess, "run", submit)
    assert gateway_launchd._spawn_deferred_launchd_reload(
        domain="gui/fixture", label=plist.stem, target=f"gui/fixture/{plist.stem}", plist_path=plist,
        gateway_pid=99999999, generation_id="g-old")
    assert reload_pending(home)["generation_id"] == "g-old"
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "launchctl").write_text(
        "#!/bin/bash\n"
        "case $1 in\n"
        "bootstrap) n=$(cat \"$SHIM/n\" 2>/dev/null || echo 0); echo $((n+1)) > \"$SHIM/n\";\n"
        "  if [ $n -eq 0 ]; then echo 'Bootstrap failed: 5: Input/output error' >&2; exit 5; fi; exit 0;;\n"
        "list) test \"$REGISTERS\" = 1 && test -f \"$SHIM/n\" && test $(cat \"$SHIM/n\") -ge 2 "
        "&& printf '\"PID\" = 12345;\\n' && exit 0; exit 1;;\n"
        "esac\nexit 0\n", encoding="utf-8")
    (shim / "sleep").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    for tool in ("launchctl", "sleep"):
        (shim / tool).chmod(0o755)
    real_run(["/bin/bash", "-c", captured["script"]], timeout=30, check=True,
             env={**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", "SHIM": str(shim),
                  "REGISTERS": "1" if registers else "0"})
    log = (home / "logs/launchd-reload.log").read_text(encoding="utf-8")
    assert "Bootstrap failed: 5: Input/output error" in log and "exited 5" in log
    assert ("helper succeeded" in log) is registers
    assert ("FAILED launchd reload" in log) is not registers
    assert reload_pending(home) is None


def test_explicit_uninstall_still_removes_the_service_definition_and_records_it(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    plist = tmp_path / "ai.hermes.gateway.plist"
    plist.write_bytes(plistlib.dumps({"Label": "ai.hermes.gateway"}))
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: plist)
    monkeypatch.setattr(gateway_launchd, "_launchd_domain", lambda: "gui/fixture")
    monkeypatch.setattr(gateway_launchd.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(argv, 0))
    gateway_launchd.launchd_uninstall()
    assert not plist.exists()
    log = (home / "logs/launchd-reload.log").read_text(encoding="utf-8")
    assert "caller=gateway_launchd.launchd_uninstall" in log and "reason=explicit gateway uninstall" in log


def _alert_home(tmp_path):
    home = tmp_path / "profile"
    (home / "logs/guardian").mkdir(parents=True)
    return home


def test_alert_notifies_once_per_reason_per_hour_and_keeps_the_token_out(tmp_path, monkeypatch):
    home = _alert_home(tmp_path)
    token = "123456:fixture-token-never-in-receipts-aaaaaaaaaaaa"
    posted = []
    def post(url, body):
        posted.append((url, body))
        return 200, {"ok": True}
    monkeypatch.setattr(alert, "notify", lambda h, payload, real=alert.notify: real(
        h, payload, resolve=lambda _h: (token, "424242", None), post=post))
    guardian.receipt(home, "inspect", "alert", reason="gateway plist missing")
    guardian.receipt(home, "inspect", "alert", reason="gateway plist missing")
    guardian.receipt(home, "inspect", "alert", reason="corrupt current pointer; no source fallback")
    assert [body["chat_id"] for _, body in posted] == [424242, 424242]
    assert "gateway plist missing" in posted[0][1]["text"]
    stored = "".join(path.read_text() for path in (home / "logs/guardian").glob("*.json"))
    assert token not in stored and '"notify": "sent"' in stored
    # Free-text reasons (exception messages) cannot flood the chat: a total hourly cap applies.
    for attempt in range(4):
        guardian.receipt(home, "inspect", "alert", reason=f"launchctl timed out after {attempt}.5s")
    assert len(posted) == guardian.MAX_ALERTS_PER_HOUR
    assert '"notify": "hourly-cap"' in "".join(p.read_text() for p in (home / "logs/guardian").glob("*.json"))


def test_alert_respects_the_persisted_flood_deadline_and_records_a_new_one(tmp_path, monkeypatch):
    from plugins.platforms.telegram import flood_state
    home = _alert_home(tmp_path)
    posted = []
    def post(url, body):
        posted.append(body)
        return 429, {"ok": False, "parameters": {"retry_after": 600}}
    resolve = lambda _h: ("123456:fixture", "424242", None)
    flood_state.record_deadline(home, "424242", 30)
    assert alert.notify(home, {"outcome": "alert"}, resolve=resolve, post=post) == "flood"
    assert posted == []
    monkeypatch.setattr(time, "time", lambda real=time.time: real() + 31)
    assert alert.notify(home, {"outcome": "alert"}, resolve=resolve, post=post) == "flood"
    assert len(posted) == 1 and flood_state.remaining_seconds(home, "424242") > 500
    # A flood-suppressed alert is retried on a later tick instead of being deduplicated.
    monkeypatch.setattr(alert, "notify", lambda *a, **k: "flood")
    guardian.receipt(home, "inspect", "alert", reason="gateway plist missing")
    monkeypatch.setattr(alert, "notify", lambda *a, **k: "sent")
    path = guardian.receipt(home, "inspect", "alert", reason="gateway plist missing")
    assert json.loads(path.read_text())["notify"] == "sent"


def test_alert_target_resolves_like_the_gateway_from_the_profile(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Registered with monkeypatch so the dotenv load's process-env writes are undone.
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_HOME_CHANNEL", "TELEGRAM_HOME_CHANNEL_THREAD_ID"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    (home / ".env").write_text("TELEGRAM_BOT_TOKEN=123456:fixture-token-aaaaaaaaaaaaaaaaaaaaaaaaa\n"
                               "TELEGRAM_HOME_CHANNEL=424242\n", encoding="utf-8")
    assert alert.resolve_target(home)[:2] == ("123456:fixture-token-aaaaaaaaaaaaaaaaaaaaaaaaa", "424242")


def test_uninstall_stops_the_owning_guardian_before_the_plist_disappears(tmp_path, monkeypatch):
    """A guardian tick between removal and its own uninstall must see a stopped service."""
    from hermes_cli import uninstall
    home = tmp_path / "profile"
    home.mkdir()
    plist = tmp_path / "ai.hermes.gateway.plist"
    plist.write_bytes(plistlib.dumps({"Label": "ai.hermes.gateway",
                                      "EnvironmentVariables": {"HERMES_HOME": str(home)}}))
    seen = []
    def run(argv, **kwargs):
        seen.append((argv[1], guardian.intent_path(home).exists(), plist.exists()))
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(uninstall, "_launchd_gateway_plists", lambda: [plist])
    monkeypatch.setattr(uninstall.subprocess, "run", run)
    assert uninstall._remove_launchd_gateway()
    assert seen and all(stopped and present for _, stopped, present in seen)
    assert not plist.exists()
