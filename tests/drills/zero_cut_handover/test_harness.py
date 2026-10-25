from __future__ import annotations

import dataclasses
from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from tests.drills.zero_cut_handover import harness


@dataclass
class FakeProcess:
    returncode: int | None = 0

    def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def poll(self):
        return self.returncode


class FakeWorld:
    def __init__(self, tmp_path: Path):
        self.home = tmp_path
        self.sent: list[str] = []
        self.killed: list[int] = []
        self.commands: list[tuple[list[str], bool, dict[str, str]]] = []
        self.processes: list[FakeProcess] = []
        self.active = "a"
        self.pids = {"a": 4242, "b": 4343}
        self.standby_booted = True
        self.handover_accepted = True
        self.live_background = False

    def run_command(self, argv, *, env=None, background=False):
        env = dict(env or {})
        self.commands.append((list(argv), background, env))
        if background:
            process = FakeProcess(None if self.live_background else 0)
            self.processes.append(process)
            return process
        return harness.CommandResult()

    def launchctl(self, argv):
        if "print" in argv:
            label = str(argv[-1]).rsplit("/", 1)[-1]
            if label == "ai.hermes.gateway":
                return harness.CommandResult(stdout="pid = 9999\n")
            slot = "b" if label.endswith(".b") else "a"
            if slot == "b" and not self.standby_booted:
                return harness.CommandResult(returncode=1, stderr="not found")
            return harness.CommandResult(stdout=f"pid = {self.pids[slot]}\n")
        return harness.CommandResult()

    def send(self, _peer, text):
        self.sent.append(text)
        tag = text.rsplit(" ", 1)[-1]
        final = "DONE" if "sleep 45" in text else "ACK"
        self.sent.append(f"{final} {tag}")
        return {"id": len(self.sent)}

    def read_chat(self, _peer, _limit):
        return {"messages": [{"text": text} for text in self.sent]}

    def read_state(self, _home):
        rows = []
        for text in self.sent:
            if text.startswith(("ZC ", "run ", "reply ")):
                rows.append({"role": "user", "content": text})
            elif text.startswith(("ACK ", "DONE ")):
                rows.append({"role": "assistant", "content": text})
        slots = [{"slot": slot, "standby": slot != self.active, "owns_telegram": slot == self.active,
                  "pid": self.pids[slot]} for slot in ("a", "b")]
        return {"tables": {"messages": rows, "outbox": [], "gateway_routing": []}, "slots": slots}

    def query_control(self, _home, verb, *, slot):
        if verb == "handover-status":
            return {"accepted": self.handover_accepted}
        if verb == "identify":
            return {"slot": slot, "pid": self.pids[slot], "standby": slot != self.active}
        if verb == "status":
            return {"slot": slot, "pid": self.pids[slot], "standby": slot != self.active,
                    "owns_telegram": slot == self.active}
        return None

    def kill(self, pid):
        self.killed.append(pid)
        for slot, current in list(self.pids.items()):
            if current != pid:
                continue
            if slot == self.active:
                self.active = "b" if slot == "a" else "a"
            else:
                self.standby_booted = False
                self.pids[slot] += 1000
                for process in self.processes:
                    if process.returncode in (None, 0):
                        process.returncode = 1

    def effects(self):
        return harness.Effects(self.run_command, self.launchctl, self.send,
                               self.read_chat, self.read_state, sleep=lambda _seconds: None,
                               clock=lambda: 1_700_000_000.0, kill=self.kill,
                               query_control=self.query_control)


def _config(home: Path, enabled: bool = True):
    home.mkdir(parents=True, exist_ok=True)
    value = "true" if enabled else "false"
    (home / "config.yaml").write_text(
        f"gateway:\n  zero_cut_handover:\n    enabled: {value}\n", encoding="utf-8")


def test_guard_refuses_non_isolated_home(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "LIVE_HOME", tmp_path / "isolated")
    effects = FakeWorld(tmp_path).effects()
    with pytest.raises(ValueError, match="outside"):
        harness.safety_guard(tmp_path / "other", "bot", effects,
                             labels=("safe.a", "safe.b"), peer_resolver=lambda *_: "brianle_hermes_handover_bot")


def test_guard_refuses_production_slot_label(monkeypatch, tmp_path):
    home = tmp_path / "isolated"
    _config(home)
    monkeypatch.setattr(harness, "LIVE_HOME", home)
    effects = FakeWorld(tmp_path).effects()
    with pytest.raises(ValueError, match="production launchd label"):
        harness.safety_guard(home, "bot", effects,
                             labels=("ai.hermes.gateway", "safe.b"), peer_resolver=lambda *_: "brianle_hermes_handover_bot")


def test_guard_refuses_disabled_flag(monkeypatch, tmp_path):
    home = tmp_path / "isolated"
    _config(home, enabled=False)
    monkeypatch.setattr(harness, "LIVE_HOME", home)
    effects = FakeWorld(tmp_path).effects()
    with pytest.raises(ValueError, match="enabled is not true"):
        harness.safety_guard(home, "bot", effects, labels=("safe.a", "safe.b"),
                             peer_resolver=lambda *_: "brianle_hermes_handover_bot")


def test_guard_refuses_wrong_peer(monkeypatch, tmp_path):
    home = tmp_path / "isolated"
    _config(home)
    monkeypatch.setattr(harness, "LIVE_HOME", home)
    effects = FakeWorld(tmp_path).effects()
    with pytest.raises(ValueError, match="does not resolve"):
        harness.safety_guard(home, "wrong", effects, labels=("safe.a", "safe.b"),
                             peer_resolver=lambda *_: "some_other_bot")


def test_guard_refuses_slot_sharing_production_pid(monkeypatch, tmp_path):
    home = tmp_path / "isolated"
    _config(home)
    monkeypatch.setattr(harness, "LIVE_HOME", home)
    world = FakeWorld(tmp_path)
    world.pids["a"] = 9999
    with pytest.raises(ValueError, match="production gateway pid"):
        harness.safety_guard(home, "bot", world.effects(), labels=("safe.a", "safe.b"),
                             peer_resolver=lambda *_: "brianle_hermes_handover_bot")


def test_assertions_use_state_db_and_detect_duplicate_user():
    state = {"tables": {"messages": [
        {"role": "user", "content": "reply exactly ACK tag"},
        {"role": "user", "content": "reply exactly ACK tag"},
        {"role": "assistant", "content": "ACK tag"},
    ]}, "slots": [{"standby": False}]}
    evidence = harness.assert_evidence(["tag"], {"messages": [{"text": "ACK tag"}]}, state,
                                       "no model_turn_start lines", planned=False)
    assert evidence["passed"] is False
    assert evidence["assertions"]["no_auto_replay"]["passed"] is False


def test_assertions_detect_duplicate_final_and_parking():
    state = {"tables": {"messages": [
        {"role": "user", "content": "reply tag"},
        {"role": "assistant", "content": "ACK tag"},
        {"role": "assistant", "content": "ACK tag"},
    ], "outbox": [{"state": "pending"}],
    "gateway_routing": [{"entry_json": json.dumps({"active_turn_token": "t"})}]},
    "slots": [{"standby": False}]}
    evidence = harness.assert_evidence(["tag"], {"messages": [{"text": "ACK tag"}, {"text": "ACK tag"}]}, state, "")
    assert evidence["passed"] is False
    assert evidence["assertions"]["final_reply_exactly_once"]["duplicates"] == {"tag": 2}
    assert evidence["assertions"]["no_parking"]["passed"] is False


def test_planned_drill_uses_checkout_module_and_no_install(tmp_path):
    world = FakeWorld(tmp_path)
    result = harness.run_drill(home=tmp_path, peer="bot", drill="promotion-burst",
                               effects=world.effects())
    assert result["passed"] is True
    assert len(result["tags"]) == 5
    assert not any(argv[-3:] == ["gateway", "install", "--if-missing"] for argv, _bg, _env in world.commands)
    assert all(argv[:3] == [harness.sys.executable, "-m", "hermes_cli.main"] for argv, _bg, _env in world.commands)


def test_planned_drill_works_when_slot_b_starts_active(tmp_path):
    world = FakeWorld(tmp_path)
    world.active = "b"
    result = harness.run_drill(home=tmp_path, peer="bot", drill="promotion-burst",
                               effects=world.effects())
    assert result["passed"] is True
    assert result["starting_active_slot"] == "b"


def test_suite_installs_once_and_checks_slot_a(tmp_path):
    world = FakeWorld(tmp_path)
    results = harness.run_suite(home=tmp_path, peer="bot", drills="planned", repeat=1,
                                effects=world.effects())
    assert len(results) == 4
    installs = [argv for argv, _background, _env in world.commands
                if argv[-3:] == ["gateway", "install", "--if-missing"]]
    assert len(installs) == 1


def test_crash_drill_kills_active_after_acceptance(tmp_path):
    world = FakeWorld(tmp_path)
    result = harness.run_drill(home=tmp_path, peer="bot", drill="kill-a-during-drain",
                               effects=world.effects())
    assert result["passed"] is True
    assert world.killed == [4242]


def test_crash_drill_restarts_promoted_slot(tmp_path):
    world = FakeWorld(tmp_path)
    result = harness.run_drill(home=tmp_path, peer="bot", drill="kill-b-after-promotion",
                               effects=world.effects())
    assert result["passed"] is True
    assert world.killed == [4343]


def test_standby_boot_failure_kills_each_new_pid_and_preserves_active(tmp_path):
    world = FakeWorld(tmp_path)
    world.live_background = True
    result = harness.run_drill(home=tmp_path, peer="bot", drill="standby-fails-to-boot",
                               effects=world.effects())
    assert result["passed"] is True
    assert world.killed == [4343]


def test_peer_resolver_requires_the_drill_bot_id_in_the_real_chat_list_shape(monkeypatch):
    calls = []
    chats = [{"id": 8899335561, "name": "Hermes Handover Test", "type": "user"},
             {"id": 2027045491, "name": "Brian", "type": "user"}]

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return harness.CommandResult(stdout=json.dumps(chats))
    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    monkeypatch.setattr(harness, "_PEER_VERIFIED", set())
    effects = FakeWorld(Path("/tmp")).effects()
    assert harness._peer_username(effects, "8899335561") == "brianle_hermes_handover_bot"
    assert harness._peer_username(effects, "@brianle_hermes_handover_bot") == "brianle_hermes_handover_bot"
    assert calls and calls[0][0] == harness.TELEGRAM
    assert harness._peer_username(effects, "2027045491") is None  # never the live bot or the user
    chats.pop(0)
    harness._PEER_VERIFIED.clear()
    assert harness._peer_username(effects, "8899335561") is None  # id must exist in the account


def test_observer_send_failure_is_a_harness_error_not_lost_inbound(tmp_path):
    """Real drill: the test account's send errored (no message id) and the drill reported the tag lost."""
    effects = dataclasses.replace(FakeWorld(tmp_path).effects(), send_as_user=lambda peer, text: {"error": "x"})
    with pytest.raises(harness.DrillObserverError):
        harness._send(effects, "bot", "promotion-burst", 1, harness.ACK, [], [])


def test_final_matches_several_acks_on_one_line():
    line = "ACK promotion-burst-1-4 ACK promotion-burst-1-5"
    assert harness._final_re("promotion-burst-1-4").search(line)
    assert harness._final_re("promotion-burst-1-5").search(line)
    assert not harness._final_re("promotion-burst-1-").search(line)
    assert not harness._final_re("promotion-burst-1-4", "DONE").search(line)


def test_observer_instruction_text_never_counts_as_a_final():
    assert not harness._final_re("t-1").search("ZC x-1 run terminal sleep 45 then reply exactly DONE t-1")


def test_observer_retries_a_flood_wait_reported_on_stderr(monkeypatch):
    """Real drill: FLOOD_WAIT came back as JSON on stderr with empty stdout and exit 6, so it was
    never retried and the peer check refused the whole run."""
    import subprocess
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            return subprocess.CompletedProcess(argv, 6, "", '{"error":{"code":"rate_limited","retry_after_seconds":0}}\n')
        return subprocess.CompletedProcess(argv, 0, '[{"id": 8899335561}]', "")

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    monkeypatch.setattr(harness.time, "sleep", lambda _s: None)
    assert harness._telegram_json(["--agent", "chats"]) == [{"id": 8899335561}]
    assert len(calls) == 2


def test_standby_kill_retries_a_pid_still_owned_by_launchd_exec(tmp_path):
    """Real drill: launchd lists the job pid while its root xpcproxy still holds it, so the first
    SIGKILL raised EPERM and aborted the whole crash suite."""
    world = FakeWorld(tmp_path)
    world.live_background = True
    effects = world.effects()
    attempts = []

    def kill(pid):
        attempts.append(pid)
        if len(attempts) == 1:
            raise PermissionError(1, "Operation not permitted")
        world.kill(pid)

    result = harness.run_drill(home=tmp_path, peer="bot", drill="standby-fails-to-boot",
                               effects=dataclasses.replace(effects, kill=kill))
    assert result["passed"] is True
    assert attempts == [4343, 4343] and world.killed == [4343]


def test_live_suite_refuses_a_wrong_home_before_installing_anything(monkeypatch, tmp_path):
    """Review: run_suite ran `gateway install` against the supplied home before any guard ran."""
    monkeypatch.setattr(harness, "LIVE_HOME", tmp_path / "isolated")
    world = FakeWorld(tmp_path)
    with pytest.raises(ValueError, match="outside"):
        harness.run_suite(home=tmp_path / "other", peer="bot", drills="planned", repeat=1,
                          effects=world.effects(), live=True)
    assert not any("install" in argv for argv, _bg, _env in world.commands)
