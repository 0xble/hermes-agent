"""Regression tests for deferring TUI /moa while a turn owns the live agent."""

from types import SimpleNamespace

from tui_gateway import server


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
    assert session["pending_moa"][0]["queue_token"] == result["result"]["moa_token"]
    assert session["pending_moa"][0]["prompt"] == "compare answers"
    assert session["pending_moa"][0]["preset"] == "default"
    assert session["pending_moa"][0]["restore"] == {
        "override": {"model": "standing", "provider": "openai"},
        "model": "gpt-4", "provider": "openai",
    }


def test_pending_moa_applies_to_matching_next_turn_and_restores(monkeypatch):
    session = _session(running=False)
    token = "moa-token"
    session["pending_moa"] = [{
        "queue_token": token,
        "prompt": "compare answers", "preset": "default",
        "restore": {"override": {"model": "standing", "provider": "openai"},
                    "model": "gpt-4", "provider": "openai"},
    }]
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
    assert session["agent"].model == "default"
    assert session["moa_one_shot_restore"]["override"]["model"] == "standing"

    server._restore_moa_one_shot("sid", session)
    assert calls == ["default --provider moa", "gpt-4 --provider openai"]
    assert session["agent"].model == "gpt-4"
    assert session["model_override"] == {"model": "standing", "provider": "openai"}


def test_two_queued_moa_commands_keep_the_base_restore_snapshot(monkeypatch):
    session = _session(running=True)
    monkeypatch.setattr(server, "_tools_mod", lambda _name: _MoaConfig)
    monkeypatch.setattr(server, "_load_cfg", lambda: {"moa": {}})
    monkeypatch.setattr(server, "_apply_model_switch", lambda *args, **kwargs: None)

    first = server._cmd_moa("r1", {"session_id": "sid"}, session, "moa", "first")
    second = server._cmd_moa("r2", {"session_id": "sid"}, session, "moa", "second")

    assert len(session["pending_moa"]) == 2
    assert first["result"]["moa_token"] != second["result"]["moa_token"]
    assert session["pending_moa"][0]["restore"] == session["pending_moa"][1]["restore"]
    assert session["pending_moa"][0]["restore"]["provider"] == "openai"


def test_moa_queue_token_prevents_same_text_queue_merge():
    from tui_gateway import session_auto_continue

    session = {"queued_prompt": {"text": "same", "transport": "ordinary"}}
    moa = session_auto_continue._enqueue_prompt(
        session, "same", "moa", moa_token="token")

    assert moa is session["queued_prompts"][0]
    assert session["queued_prompt"]["text"] == "same"
    assert moa["moa_token"] == "token"
