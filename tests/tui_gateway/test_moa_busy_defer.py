"""Regression tests for deferring TUI /moa while a turn owns the live agent."""

from threading import RLock
from types import SimpleNamespace

from tui_gateway import pending_moa, server


class _MoaConfig:
    @staticmethod
    def moa_usage():
        return "usage: /moa <prompt>"

    @staticmethod
    def normalize_moa_config(_config):
        return {"default_preset": "default"}


def _session(*, running=True):
    return {
        "agent": SimpleNamespace(model="gpt-4", provider="openai"),
        "model_override": {"model": "standing", "provider": "openai"},
        "running": running,
    }


def test_moa_during_running_turn_does_not_touch_live_agent(monkeypatch):
    session = _session(running=True)
    switched = []
    monkeypatch.setattr(server, "_tools_mod", lambda _name: _MoaConfig)
    monkeypatch.setattr(server, "_load_cfg", lambda: {"moa": {}})
    monkeypatch.setattr(server, "_apply_model_switch", lambda *args, **kwargs: switched.append(args))

    result = server._cmd_moa("r1", {"session_id": "sid"}, session, "moa", "compare answers")

    assert result["result"]["type"] == "send"
    assert result["result"]["message"] == "compare answers"
    # Ink must queue (not steer/interrupt) this prompt, or a stale pending entry would remain.
    assert result["result"]["queued"] is True
    assert session["agent"].model == "gpt-4"
    assert switched == []
    assert session["pending_moa"][result["result"]["moa_token"]]["token"] == result["result"]["moa_token"]
    assert session["pending_moa"][result["result"]["moa_token"]]["prompt"] == "compare answers"
    assert session["pending_moa"][result["result"]["moa_token"]]["preset"] == "default"
    assert session["pending_moa"][result["result"]["moa_token"]]["status"] == "pending"
    assert session["pending_moa"][result["result"]["moa_token"]]["restore"] == {}


def test_pending_moa_applies_to_matching_next_turn_and_restores(monkeypatch):
    session = _session(running=False)
    token = "moa-token"
    session["pending_moa"] = {token: {
        "token": token,
        "prompt": "compare answers", "preset": "default", "status": "pending",
        "restore": {"override": {"model": "standing", "provider": "openai"},
                    "model": "gpt-4", "provider": "openai"},
    }}
    calls = []

    def apply(_sid, _session, raw, **kwargs):
        calls.append(raw)
        if "--provider moa" in raw:
            _session["agent"].model = "default"
            _session["agent"].provider = "moa"
        else:
            _session["agent"].model = "gpt-4"
            _session["agent"].provider = "openai"
        return {}

    monkeypatch.setattr(server, "_apply_model_switch", apply)
    server._apply_pending_moa("sid", session, "unrelated prompt")
    assert session["agent"].model == "gpt-4"
    assert session["pending_moa"]

    server._apply_pending_moa("sid", session, "compare answers", token)
    assert calls == ["default --provider moa"]
    assert session["pending_moa"][token]["status"] == "claimed"
    assert session["agent"].model == "default"
    assert session["pending_moa"][token]["restore"]["override"]["model"] == "standing"

    server._restore_moa_one_shot("sid", session)
    assert calls == ["default --provider moa", "gpt-4 --provider openai"]
    assert session["agent"].model == "gpt-4"
    assert session["model_override"] == {"model": "standing", "provider": "openai"}
    assert session["pending_moa"][token]["status"] == "consumed"


def test_two_queued_moa_commands_keep_the_base_restore_snapshot(monkeypatch):
    session = _session(running=True)
    monkeypatch.setattr(server, "_tools_mod", lambda _name: _MoaConfig)
    monkeypatch.setattr(server, "_load_cfg", lambda: {"moa": {}})
    monkeypatch.setattr(server, "_apply_model_switch", lambda *args, **kwargs: None)

    first = server._cmd_moa("r1", {"session_id": "sid"}, session, "moa", "first")
    second = server._cmd_moa("r2", {"session_id": "sid"}, session, "moa", "second")

    assert len(session["pending_moa"]) == 2
    assert first["result"]["moa_token"] != second["result"]["moa_token"]
    assert session["pending_moa"][first["result"]["moa_token"]]["restore"] == {}
    assert session["pending_moa"][second["result"]["moa_token"]]["restore"] == {}



def test_claim_time_restore_ignores_expired_model_once_override(monkeypatch):
    session = _session(running=False)
    session["agent"].model = "standing-model"
    session["agent"].provider = "openai"
    token = "queued-after-once"
    session["pending_moa"] = {token: {
        "token": token, "prompt": "compare", "preset": "default", "status": "pending", "restore": {},
    }}
    calls = []

    def apply(_sid, _session, raw, **kwargs):
        calls.append(raw)
        return None

    monkeypatch.setattr(server, "_apply_model_switch", apply)
    assert server._apply_pending_moa("sid", session, "compare", token) is True
    assert session["pending_moa"][token]["restore"]["model"] == "standing-model"
    assert session["pending_moa"][token]["restore"]["model"] != "X"


def test_moa_queue_token_prevents_same_text_queue_merge():
    from tui_gateway import session_auto_continue

    session = {"queued_prompt": {"text": "same", "transport": "ordinary"}}
    moa = session_auto_continue._enqueue_prompt(
        session, "same", "moa", moa_token="token")

    assert moa is session["queued_prompts"][0]
    assert session["queued_prompt"]["text"] == "same"
    assert moa["moa_token"] == "token"


def test_missing_token_drops_prompt_with_visible_notice(monkeypatch):
    session = _session(running=False)
    events = []
    monkeypatch.setattr(server, "_emit", lambda *args: events.append(args))

    assert server._apply_pending_moa("sid", session, "compare answers", "missing") is False
    assert events[-1][0:2] == ("error", "sid")
    assert "prompt dropped" in events[-1][2]["message"]


def test_compute_host_frame_carries_claimed_server_record():
    token = "host-moa"
    session = _session(running=False)
    session.update({"history_lock": RLock(), "session_key": "session-key", "history": []})
    session["pending_moa"] = {token: {
        "token": token, "prompt": "compare answers", "preset": "default", "status": "pending",
        "restore": {"override": None, "model": "gpt-4", "provider": "openai"},
    }}

    frame = server._compute_host_turn_frame("r1", "sid", session, "compare answers", queue_token=token)

    assert frame["pending_moa_record"]["token"] == token
    assert session["pending_moa"][token]["status"] == "claimed"
    assert frame["pending_moa_record"]["preset"] == "default"


def test_cancel_all_marks_pending_and_claimed_records_cancelled():
    session = {"history_lock": RLock(), "pending_moa": {
        "one": {"token": "one", "status": "pending"},
        "two": {"token": "two", "status": "claimed"},
        "three": {"token": "three", "status": "consumed"},
    }}

    assert pending_moa.cancel_all(session) == 2
    assert session["pending_moa"]["one"]["status"] == "cancelled"
    assert session["pending_moa"]["two"]["status"] == "cancelled"
    assert session["pending_moa"]["three"]["status"] == "consumed"
