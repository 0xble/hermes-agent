from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import build_resume_recovery_note, resolve_restart_resume_policy


def _adapter(*, interactive: bool = True, override: str | None = None):
    extra = {} if override is None else {"restart_resume_policy": override}
    return SimpleNamespace(
        interactive_resume=interactive,
        config=PlatformConfig(extra=extra),
    )


def test_global_continue_policy_applies_to_interactive_adapter():
    config = GatewayConfig(restart_resume_policy="continue")

    assert resolve_restart_resume_policy(config, _adapter()) == "continue"


def test_platform_override_wins_over_global_policy():
    config = GatewayConfig(restart_resume_policy="continue")

    assert resolve_restart_resume_policy(config, _adapter(override="ask")) == "ask"


def test_noninteractive_adapter_keeps_safe_continue_default():
    config = GatewayConfig(restart_resume_policy="ask")

    assert resolve_restart_resume_policy(config, _adapter(interactive=False)) == "continue"


@pytest.mark.parametrize("value", ["", "discard", True])
def test_invalid_global_policy_fails_at_config_construction(value):
    with pytest.raises(ValueError, match="restart_resume_policy"):
        GatewayConfig(restart_resume_policy=value)


def test_invalid_platform_override_fails_at_config_construction():
    platform = PlatformConfig(extra={"restart_resume_policy": "discard"})

    with pytest.raises(ValueError, match="restart_resume_policy"):
        GatewayConfig(platforms={Platform.TELEGRAM: platform})


def _assert_neutral_resume_note(note: str) -> None:
    from gateway.run import RESUME_NOTE_PREFIX
    assert note.startswith(RESUME_NOTE_PREFIX)
    assert "Any restart, update, or shutdown command in the history has already run" in note
    assert "Do NOT re-run tool calls whose results are recorded" in note
    assert "Before retrying a non-idempotent effect without a recorded result" in note
    assert "send, payment, push, or external write" in note
    assert "reconcile its current state first" in note
    assert "Do not announce a resumed session" in note
    assert "Do not mention this recovery to the user" in note
    assert "back online" not in note
    assert "session was restored" not in note
    assert "previous turn was interrupted" not in note


@pytest.mark.parametrize("policy", ["ask", "continue"])
@pytest.mark.parametrize("message", ["", "Please finish the deployment."])
def test_resume_note_is_neutral_and_keeps_recovery_safety(policy, message):
    note = build_resume_recovery_note(
        "restart_timeout", message, restart_resume_policy=policy,
    )

    _assert_neutral_resume_note(note)
    if message:
        # A new message means the user moved on: stale pending work is skipped, not resumed.
        assert "Address the user's NEW message below FIRST" in note
        assert "Skip unfinished work from the conversation history" in note
        assert "if it does, resume from the first step without a recorded result" in note
        assert "resuming from the first step" not in note
        assert message in note
    elif policy == "continue":
        assert "continue the pending task to completion" in note.lower()
        assert "do not emit an acknowledgement" in note.lower()
        assert "resuming from the first step without a recorded result" in note
    else:
        assert "do not run tools or continue the pending task until the user replies" in note.lower()
        assert "ask whether to carry on with the pending step" in note
        assert "resuming from the first step" not in note


def test_gateway_config_round_trips_global_policy():
    config = GatewayConfig.from_dict({"restart_resume_policy": "continue"})

    assert config.restart_resume_policy == "continue"
    assert config.to_dict()["restart_resume_policy"] == "continue"

@pytest.mark.parametrize(
    ("yaml_text", "expected"),
    [
        ("gateway:\n  restart_resume_policy: continue\n", "continue"),
        ("restart_resume_policy: continue\n", "continue"),
        ("restart_resume_policy: ask\ngateway:\n  restart_resume_policy: continue\n", "ask"),
        ("restart_resume_policy: continue\ngateway:\n  restart_resume_policy: ask\n", "continue"),
        ("{}\n", "ask"),
    ],
)
def test_yaml_startup_preserves_restart_policy(tmp_path, monkeypatch, yaml_text, expected):
    from gateway.config import load_gateway_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml_text)
    config = load_gateway_config()
    policy = resolve_restart_resume_policy(config, _adapter())
    assert policy == expected
    note = build_resume_recovery_note("restart_timeout", restart_resume_policy=policy)
    assert ("continue the pending task to completion" in note.lower()) == (expected == "continue")
    assert ("until the user replies" in note.lower()) == (expected == "ask")


@pytest.mark.parametrize("value", ["discard", "true", "''"])
def test_yaml_startup_rejects_invalid_restart_policy(tmp_path, monkeypatch, value):
    from gateway.config import load_gateway_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(f"gateway:\n  restart_resume_policy: {value}\n")
    with pytest.raises(ValueError, match="restart_resume_policy"):
        load_gateway_config()


# ---------------------------------------------------------------------------
# Runner call site: TurnRunner._prepare_turn_message must consult the resolved policy
# ---------------------------------------------------------------------------


def _turn_runner(policy_config, adapter, *, message="", resume_pending=True):
    """Build a TurnRunner over a fresh resume_pending session whose adapter is *adapter*."""
    from datetime import datetime
    from unittest.mock import MagicMock

    from gateway.run_turn_runner import TurnRunner

    entry = SimpleNamespace(
        resume_pending=resume_pending, resume_reason="restart_timeout",
        last_resume_marked_at=datetime.now(),
    )
    runner = MagicMock()
    runner.config = policy_config
    runner._delivery_adapter_for.return_value = adapter
    runner.session_store._entries = {"sess": entry}
    runner._pending_model_notes = {}
    runner._pending_skills_reload_notes = {}
    ctx = SimpleNamespace(
        message=message, session_key="sess", history=[], persist_user_message=None,
        persist_user_timestamp=None, source=object(),
    )
    return TurnRunner(runner, ctx), ctx


def test_turn_runner_continue_policy_reaches_the_recovery_note():
    """The gateway's real call site must pass the resolved policy through: with the global
    policy set to ``continue`` on an interactive adapter, the startup auto-resume turn gets the
    continue guidance instead of the upstream ask default."""
    config = GatewayConfig(restart_resume_policy="continue")
    adapter = SimpleNamespace(interactive_resume=True, config=PlatformConfig())
    turn, ctx = _turn_runner(config, adapter)

    persisted, _ = turn._prepare_turn_message(agent_history=[])

    assert "continue the pending task to completion" in ctx.message.lower()
    assert "until the user replies" not in ctx.message
    assert persisted == ctx.message  # the empty auto-resume turn persists the note itself


def test_turn_runner_default_policy_still_asks_on_interactive_adapter():
    config = GatewayConfig()
    adapter = SimpleNamespace(interactive_resume=True, config=PlatformConfig())
    turn, ctx = _turn_runner(config, adapter)

    turn._prepare_turn_message(agent_history=[])

    assert "until the user replies" in ctx.message
    assert "continue the pending task to completion" not in ctx.message.lower()


def test_turn_runner_platform_override_beats_global_at_the_call_site():
    config = GatewayConfig(restart_resume_policy="ask")
    adapter = SimpleNamespace(
        interactive_resume=True, config=PlatformConfig(extra={"restart_resume_policy": "continue"}),
    )
    turn, ctx = _turn_runner(config, adapter)

    turn._prepare_turn_message(agent_history=[])

    assert "continue the pending task to completion" in ctx.message.lower()



@pytest.mark.parametrize(("yaml_text", "expected"), [
    ("gateway:\n  auto_resume_on_boot: false\n", False),
    ("auto_resume_on_boot: false\n", False),
    ("gateway:\n  restart_resume_policy: ask\n", True),
])
def test_yaml_startup_honours_auto_resume_on_boot(tmp_path, monkeypatch, yaml_text, expected):
    """The real config.yaml path, not GatewayConfig.from_dict: an operator's `false` must survive
    the YAML bridge, or interrupted delegations still queue recovery turns on boot."""
    from gateway.config import load_gateway_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml_text)
    assert load_gateway_config().auto_resume_on_boot is expected


def test_telegram_trigger_classifier_matches_the_resume_note_prefix():
    """The classifier copies the prefix as a literal; keep it in lockstep with the builder."""
    from gateway.run import RESUME_NOTE_PREFIX
    from plugins.platforms.telegram.chat_budget import _TRIGGER_PREFIXES

    assert (RESUME_NOTE_PREFIX, "restart") in _TRIGGER_PREFIXES
