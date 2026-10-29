"""A route owner's ``max_retry_wait_seconds`` bounds how long a delegated child sits out a
provider cooldown. A reviewer with fallback routes must fail its attempt at once when the provider
declares a 10-minute reset, so the owner moves to the next route instead of waiting 3 x 600s."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent


def _rate_limit(retry_after: str, status: int = 429):
    class _ProviderError(Exception):
        status_code = status

        def __init__(self):
            super().__init__(f"Error code: {status} - provider cooling down")
            self.response = SimpleNamespace(headers={"retry-after": retry_after})

    return _ProviderError()


@pytest.fixture()
def agent():
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        a = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
                    quiet_mode=True, skip_context_files=True, skip_memory=True)
    a.client = MagicMock()
    a._persist_session = lambda *args, **kwargs: None
    a._save_trajectory = lambda *args, **kwargs: None
    return a


def _drive(agent, retry_after: str, status: int = 429):
    calls, sleeps = [], []

    def _fake_api_call(api_kwargs):
        calls.append(api_kwargs)
        raise _rate_limit(retry_after, status)

    def _fake_sleep(agent_, wait, *args, **kwargs):
        sleeps.append(wait)
        agent_._interrupt_requested = True  # never actually wait; end the turn
        return True

    agent._interruptible_api_call = _fake_api_call
    # The retry backoff and the post-exhaustion auto-recovery ladder each sleep through their own
    # module's reference; patch both so any wait at all is recorded.
    with (
        patch("agent.turn_api_error.interruptible_backoff_sleep", side_effect=_fake_sleep),
        patch("agent.turn_recovery.interruptible_backoff_sleep", side_effect=_fake_sleep),
    ):
        result = agent.run_conversation("hello")
    return result, calls, sleeps


def test_cooldown_longer_than_the_cap_fails_the_attempt_without_waiting(agent):
    agent._max_retry_wait_s = 60.0
    result, calls, sleeps = _drive(agent, "600")
    assert len(calls) == 1 and sleeps == []
    assert result["failed"] is True and not result.get("interrupted")


def test_over_cap_transient_error_skips_the_auto_recovery_ladder(agent):
    """A 503 with a long Retry-After is ladder-eligible; the cap must still end the attempt."""
    agent._max_retry_wait_s = 60.0
    agent._auto_recovery_cycles = 5
    result, calls, sleeps = _drive(agent, "600", status=503)
    assert len(calls) == 1 and sleeps == []
    assert result["failed"] is True and not result.get("interrupted")


@pytest.mark.parametrize(("cap", "retry_after"), [(None, "600"), (60.0, "30")])
def test_uncapped_or_short_cooldowns_keep_the_normal_retry_wait(agent, cap, retry_after):
    agent._max_retry_wait_s = cap
    _result, _calls, sleeps = _drive(agent, retry_after)
    assert sleeps == [float(retry_after)]


def test_the_cap_comes_from_the_route_owner_only(tmp_path, monkeypatch):
    """Through ``_build_child_agent``: the routing owner's cap reaches the child; a child built
    without a routing owner (ordinary delegation) keeps the uncapped retry policy."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  default: anthropic/claude-sonnet-4.6\n", encoding="utf-8")
    from tools import delegate_tool as dt
    import tools.delegate_tool_config as dtc
    monkeypatch.setattr(dt, "_load_config", lambda: {"max_retry_wait_seconds": 5})
    monkeypatch.setattr(dtc, "_load_config", lambda: {})
    kw = dict(api_key="k", base_url="https://openrouter.ai/api/v1", provider="openrouter",
              api_mode="chat_completions", model="anthropic/claude-sonnet-4.6", platform="cli", quiet_mode=True,
              skip_context_files=True, skip_memory=True, save_trajectories=False, enabled_toolsets=["file"])
    parent = AIAgent(session_id="p", **kw)
    build = dict(task_index=0, goal="goal", context=None, toolsets=["file"], model=None,
                 max_iterations=4, task_count=1, parent_agent=parent)
    routed = dt._build_child_agent(**build, routing_cfg={"model": "m", "max_retry_wait_seconds": 60})
    plain = dt._build_child_agent(**build)
    try:
        assert routed._max_retry_wait_s == 60.0
        assert plain._max_retry_wait_s is None
    finally:
        routed.close()
        plain.close()
        parent.close()
