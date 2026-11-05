"""Wait advice follows the owning profile and the actual completion consumer."""

import pytest

from gateway.session_context import preserved_session_context, set_session_vars
from gateway.session_finite import finite_turn_scope
from tools.process_registry_wait import wait_plan


@pytest.mark.parametrize("mode", ["off", "'off'", "concise"])
@pytest.mark.parametrize("surface", ["telegram", "cli", "tui", "desktop", "finite"])
@pytest.mark.parametrize("notify", [False, True])
def test_wait_guidance_matches_consumer_policy(monkeypatch, tmp_path, mode, surface, notify):
    (tmp_path / "config.yaml").write_text(f"display:\n  background_process_notifications: {mode}\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_BACKGROUND_NOTIFICATIONS", raising=False)
    monkeypatch.delenv("HERMES_EXECUTION_MODE", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD", raising=False)
    monkeypatch.delenv("HERMES_TASK_ID", raising=False)
    with preserved_session_context(), finite_turn_scope(surface == "finite"):
        set_session_vars(platform="telegram" if surface in {"telegram", "finite"} else "", source=surface)
        timeout, note = wait_plan(notify, 120)
        if mode != "concise":
            from tools.process_registry_wait_policy import _can_deliver_completion
            assert not _can_deliver_completion(), "off must not enable automatic wait promotion"
    if notify and surface != "finite" and (surface == "telegram" or mode == "concise"):
        assert timeout == 30
        assert "End the turn" in note and "do not wait again" in note
    else:
        assert timeout == 120
        assert "wait again" in note and "kill" in note
        assert "End the turn" not in note and "do not wait again" not in note
