"""Strict cooldown reads distinguish no outage from unavailable durable state."""
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import shared_primary_cooldown as cooldown
from agent.agent_runtime_helpers import _refresh_from_shared_cooldown


@pytest.mark.parametrize("failure", ["permission", "malformed", "wrong_shape"])
def test_unavailable_state_preserves_cached_deadline(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    route = ("custom", "https://primary.invalid/v1", "primary")
    record = cooldown.arm_cooldown(route, reason="rate_limit", reset_at=time.time() + 7200)
    assert record is not None
    path = cooldown._state_path()
    if failure == "permission":
        real_read = Path.read_text
        def denied(self, *args, **kwargs):
            if self == path:
                raise PermissionError("unavailable state")
            return real_read(self, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", denied)
    else:
        path.write_text("{" if failure == "malformed" else '[]', encoding="utf-8")
    deadline = time.monotonic() + 7200
    agent = SimpleNamespace(_primary_runtime=dict(zip(("provider", "base_url", "model"), route)),
                            _shared_primary_cooldown_record=record, _rate_limited_until=deadline)
    assert _refresh_from_shared_cooldown(agent) == "unavailable"
    assert agent._rate_limited_until == deadline
    assert agent._shared_primary_cooldown_record == record


@pytest.mark.parametrize("failure", ["permission", "malformed", "wrong_shape"])
def test_fresh_agent_never_probes_primary_while_state_is_unavailable(tmp_path, monkeypatch, failure):
    from run_agent import AIAgent
    from tests.agent._shared_cooldown_stub import StubProvider, write_home_config

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    write_home_config(tmp_path)
    with StubProvider() as stub:
        stub.primary_available = True  # a mistaken probe would succeed and mask the outage
        record = cooldown.arm_cooldown(
            ("custom", stub.url, "primary-model"), reason="rate_limit", reset_at=time.time() + 7200,
        )
        assert record is not None
        path = cooldown._state_path()
        real_read = Path.read_text
        if failure == "permission":

            def denied(self, *args, **kwargs):
                if self == path:
                    raise PermissionError("unavailable state")
                return real_read(self, *args, **kwargs)

            monkeypatch.setattr(Path, "read_text", denied)
        else:
            path.write_text("{" if failure == "malformed" else '[]', encoding="utf-8")
        agent = AIAgent(
            api_key="fixture", base_url=stub.url, provider="custom", model="primary-model",
            api_mode="chat_completions", quiet_mode=True, skip_context_files=True, skip_memory=True,
            enabled_toolsets=[], max_iterations=3, fallback_model=[{
                "provider": "custom", "model": "fallback-model", "base_url": stub.url,
                "api_key": "fixture", "api_mode": "chat_completions",
            }],
        )
        try:
            for message in ("hello", "another turn"):
                result = agent.run_conversation(message)
                assert result["final_response"] == "OK from fallback-model"
                assert stub.primary_requests() == []
            # A genuine successful read of cleared state releases the fail-closed hold.
            if failure == "permission":
                monkeypatch.setattr(Path, "read_text", real_read)
            cooldown.clear_cooldowns(all_routes=True)  # operator recovery also accepts corrupt state
            result = agent.run_conversation("recovered state")
            assert result["final_response"] == "OK from primary-model"
            assert len(stub.primary_requests()) == 1
        finally:
            agent.close()


def test_unavailable_state_refuses_turn_when_fallback_cannot_bind(tmp_path, monkeypatch):
    from unittest.mock import patch
    from tests.agent.test_shared_primary_cooldown import _CHAIN, _agent_with_chain

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    agent = _agent_with_chain(_CHAIN)
    path = cooldown._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{", encoding="utf-8")
    primary = agent.client
    try:
        with patch("agent.auxiliary_client.resolve_provider_client", side_effect=ValueError("unconfigured")):
            with pytest.raises(OSError, match="Malformed primary cooldown state"):
                agent._restore_primary_runtime()
        primary.chat.completions.create.assert_not_called()
    finally:
        agent.close()


def test_unavailable_adoption_keeps_cached_record_and_deadline(tmp_path, monkeypatch):
    from unittest.mock import patch
    from tests.agent.test_shared_primary_cooldown import _CHAIN, _agent_with_chain, _fallback_client
    from agent.agent_runtime_helpers import _adopt_shared_primary_cooldown

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    agent = _agent_with_chain(_CHAIN)
    record = cooldown.arm_cooldown(cooldown.route_from_agent(agent), reason="rate_limit", reset_at=time.time() + 7200)
    deadline = time.monotonic() + 7200
    agent._shared_primary_cooldown_record = record
    agent._rate_limited_until = deadline
    path = cooldown._state_path()
    path.write_text("{", encoding="utf-8")
    try:
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(
            _fallback_client("https://one.invalid/v1"), "fallback-one",
        )):
            assert _adopt_shared_primary_cooldown(agent)
        assert agent.model == "fallback-one"
        assert agent._shared_primary_cooldown_record == record
        assert agent._rate_limited_until == deadline
        notices = getattr(agent, "_pending_fallback_notice", [])
        assert any("cooldown state unreadable" in notice for notice in notices)
        assert not any("provider failure" in notice for notice in notices)
    finally:
        agent.close()


def test_missing_state_is_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert cooldown.read_cooldown(("custom", "https://primary.invalid/v1", "primary")) is None
