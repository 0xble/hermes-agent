from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from tests.drills.zero_cut_handover import harness


@dataclass
class FakeProcess:
    returncode: int = 0

    def wait(self):
        return self.returncode


class FakeWorld:
    def __init__(self, tmp_path: Path):
        self.home = tmp_path
        self.sent: list[str] = []
        self.killed: list[int] = []
        self.commands: list[tuple[list[str], bool, dict[str, str]]] = []

    def run_command(self, argv, *, env=None, background=False):
        env = dict(env or {})
        self.commands.append((list(argv), background, env))
        if env.get("ZERO_CUT_DRILL_INVALID_CONFIG") == "1":
            return harness.CommandResult(returncode=1, stderr="invalid slot")
        return FakeProcess() if background else harness.CommandResult()

    def launchctl(self, argv):
        if "print" in argv:
            return harness.CommandResult(stdout="pid = 4242\n")
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
        return {"tables": {}, "slots": [{"standby": False, "owns_telegram": True}]}

    def effects(self):
        return harness.Effects(self.run_command, self.launchctl, self.send,
                               self.read_chat, self.read_state, sleep=lambda _seconds: None,
                               clock=lambda: 1_700_000_000.0, kill=self.killed.append)


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


def test_assertions_detect_duplicate_final():
    evidence = harness.assert_evidence(
        ["tag"], {"messages": [{"text": "ACK tag"}, {"text": "ACK tag"}]},
        {"tables": {}, "slots": [{"standby": False}]}, "")
    assert evidence["passed"] is False
    assert evidence["assertions"]["final_reply_exactly_once"]["duplicates"] == {"tag": 2}


def test_assertions_detect_lost_inbound():
    evidence = harness.assert_evidence(
        ["lost"], {"messages": []}, {"tables": {}, "slots": [{"standby": False}]}, "")
    assert evidence["assertions"]["final_reply_exactly_once"]["lost"] == ["lost"]


def test_assertions_detect_double_turn_start_and_parked_residue():
    state = {"tables": {"outbox": [{"state": "pending"}]}, "slots": [{"standby": False}]}
    logs = "model_turn_start tag\nmodel_turn_start tag\n"
    evidence = harness.assert_evidence(["tag"], {"messages": [{"text": "ACK tag"}]}, state, logs)
    assert evidence["assertions"]["no_auto_replay"]["passed"] is False
    assert evidence["assertions"]["no_parking"]["passed"] is False


def test_planned_drill_sequence_is_fake_only(tmp_path):
    world = FakeWorld(tmp_path)
    result = harness.run_drill(home=tmp_path, peer="bot", drill="promotion-burst",
                               effects=world.effects())
    assert result["passed"] is True
    assert len(result["tags"]) == 5
    assert len(result["message_ids"]) == 5
    assert any(background for _argv, background, _env in world.commands)
    assert not world.killed
    assert list((tmp_path / "drills").glob("*.json"))


def test_crash_drill_sequence_kills_a_and_allows_interrupted(tmp_path):
    world = FakeWorld(tmp_path)
    result = harness.run_drill(home=tmp_path, peer="bot", drill="kill-a-during-drain",
                               effects=world.effects())
    assert result["passed"] is True
    assert world.killed == [4242]
    assert result["assertions"]["no_cut"]["passed"] is True


def test_peer_resolver_requires_a_matching_username():
    world = FakeWorld(Path("/tmp"))
    world.run_command = lambda argv, **kwargs: harness.CommandResult(
        stdout=json.dumps({"chats": [{"id": "123", "username": "brianle_hermes_handover_bot"}]})
    )
    assert harness._peer_username(world.effects(), "123") == "brianle_hermes_handover_bot"
    assert harness._peer_username(world.effects(), "other") is None
