"""Compression exhaustion pauses the original conversation, it is not /new (#546)."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_state import SessionDB


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [Platform.TELEGRAM, Platform.DISCORD, Platform.LOCAL])
@pytest.mark.parametrize("deferred", [False, True])
async def test_exhaustion_keeps_history_binding_and_scoped_settings(tmp_path, monkeypatch, platform, deferred):
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    source = SessionSource(platform=platform, chat_id="123", user_id="u1", thread_id="456")
    store = runner.session_store
    entry = store.get_or_create_session(source)
    sid, key = entry.session_id, entry.session_key
    db = store._db
    assert isinstance(db, SessionDB)
    db.create_session(sid, platform.value, user_id="u1")
    history = [
        {"role": "user", "content": "Keep the original instructions"},
        {"role": "assistant", "content": "Completed effect", "tool_calls": [
            {"id": "call1", "type": "function", "function": {"name": "write", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call1", "content": "Created exactly once"},
        {"role": "assistant", "content": "Receipt: created exactly once"},
    ]
    db.replace_messages(sid, history)
    db.set_session_title(sid, "Runtime")
    db.set_session_title_source(sid, "user")
    store.set_model_role(key, "fast")
    runner._session_state(key).conversation.model_role = "fast"
    before = store.load_transcript(sid)
    binding = Mock()
    eviction = Mock()
    monkeypatch.setattr(runner, "_sync_telegram_topic_binding", binding)
    monkeypatch.setattr(runner, "_evict_cached_agent", eviction)
    # LOCAL exhaustion used to schedule a blank reset after this admission settled.
    from gateway.session_ingress import executing_admission
    runner.session_authority = SimpleNamespace(deferred_resets={})
    admission = executing_admission.set(True)
    try:
        response, returned = await runner._hmwa_compression_exhaustion_reset(
            {"failed": True, "compression_exhausted": True, "compression_deferred": deferred},
            "Compression could not finish.", entry, key, source,
        )
        assert returned is entry
        # Resolve the next inbound message through the real routing and transcript store.
        resolved = store.get_or_create_session(source)
        assert resolved.session_id == sid
        assert store.load_transcript(resolved.session_id) == before
        assert db.get_session_title(sid) == "Runtime"
        assert db.get_session_title_source(sid) == "user"
        assert store.get_model_role(key) == "fast"
        assert runner._session_state(key).conversation.model_role == "fast"
        binding.assert_not_called()
        eviction.assert_not_called()
        assert not runner.session_authority.deferred_resets
        assert "Compression could not finish." in response
        assert "Session auto-reset" not in response
        assert "next message will start a fresh session" not in response
        if deferred:
            assert response == "Compression could not finish."
        else:
            from agent.i18n import t
            assert response == "Compression could not finish.\n\n" + t("gateway.compress.hygiene_failed")
            assert "kept everything as-is" in response
    finally:
        executing_admission.reset(admission)
        if runner._executor:
            runner._executor.shutdown(wait=True)
        db.close()
