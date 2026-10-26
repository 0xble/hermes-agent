"""A cron run's own result survives a SessionDB title-cleanup timeout.

The finalizer bounds every SessionDB call; after one times out the proxy fails every later call.
The title fallback path must absorb that, and finalization as a whole must never replace the run's
result or skip the agent teardown that follows it.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from cron.scheduler import run_job
from hermes_state import SessionDB
from tests.cron.conftest import owner_execution_runner

_RUNTIME = {
    "api_key": "test-key",
    "base_url": "https://example.invalid/v1",
    "provider": "openrouter",
    "api_mode": "chat_completions",
}


class TitleHangingSessionDB(SessionDB):
    """A real SessionDB whose title write stalls past the cleanup timeout, like a held writer lock."""

    def __init__(self, *args, release: threading.Event, **kwargs):
        super().__init__(*args, **kwargs)
        self.release = release
        self.title_entered = threading.Event()
        self.calls: list[str] = []

    def set_session_title(self, session_id, title):
        self.calls.append("set_session_title")
        self.title_entered.set()
        self.release.wait(timeout=10)
        return super().set_session_title(session_id, title)

    def get_next_title_in_lineage(self, base_title):
        self.calls.append("get_next_title_in_lineage")
        return super().get_next_title_in_lineage(base_title)

    def end_session(self, session_id, end_reason):
        self.calls.append("end_session")
        return super().end_session(session_id, end_reason)


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_run_result_and_teardown_survive_title_cleanup_timeout(tmp_path, outcome, monkeypatch):
    release = threading.Event()
    db = TitleHangingSessionDB(db_path=tmp_path / "state.db", release=release)
    job = {"id": f"title-timeout-{outcome}", "name": "title timeout", "prompt": "hello"}
    torn_down = threading.Event()

    def _run_conversation(*_args, **_kwargs):
        if outcome == "failure":
            raise RuntimeError("original agent failure")
        return {"final_response": "original answer"}

    try:
        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state_registry.acquire", return_value=db), \
             patch("hermes_state_registry.release", return_value=False), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=_RUNTIME), \
             patch("run_agent.AIAgent") as mock_agent_cls, \
             patch("cron.scheduler._teardown_cron_agent", side_effect=lambda *_a, **_k: torn_down.set()), \
             patch("cron.scheduler._cron_cleanup_timeout_seconds", return_value=0.05):
            # Cron runs inside its profile owner, which requires the canonical owner store; build
            # the owner seam under the acquire patch so the hanging store is that owner store.
            monkeypatch.setattr("cron.scheduler_authority.run_canonical_job", owner_execution_runner())
            agent = MagicMock()
            agent.session_id = None
            agent.run_conversation.side_effect = _run_conversation
            mock_agent_cls.return_value = agent

            success, output, final_response, error = run_job(job)

        assert db.title_entered.wait(timeout=2.0)
        # The title write timed out and disabled the bounded proxy; the fallback was attempted
        # through the guard instead of escaping from the fallback tuple.
        assert db.calls[0] == "set_session_title"
        if outcome == "success":
            assert success is True
            assert final_response == "original answer"
            assert error is None
        else:
            assert success is False
            assert "original agent failure" in (error or "")
            assert "original agent failure" in output
            assert "session finalization disabled" not in (error or "")
        # Remaining teardown still ran after the title failure.
        assert torn_down.is_set()
    finally:
        release.set()
