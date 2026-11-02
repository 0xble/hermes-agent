"""Shutdown bounds include provider close, not merely the manager's FIFO drain."""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import agent.redact as redaction
from run_agent import AIAgent

from agent.memory_manager import MemoryManager
from plugins.memory.hindsight import HindsightMemoryProvider


def test_agent_shutdown_does_not_redact_for_inherited_noop_hooks(monkeypatch):
    """The observed hk_1 stack scrubbed the transcript for NO consumers.

    Use the real shutdown_memory_provider -> on_session_end -> redact_for_egress
    -> _redact_assignments chain; Hindsight inherits the base no-op end hook.
    No transcript is sent to a provider, so there is no egress to redact.
    """
    provider = HindsightMemoryProvider()
    manager = MemoryManager()
    manager._providers = [provider]
    assignments = Mock(wraps=redaction._redact_assignments)
    monkeypatch.setattr(redaction, "_redact_assignments", assignments)
    transcript = [{"role": "tool", "content": "blob=" + "a" * 80_000 + "\nconfig password documentation"}]
    agent = SimpleNamespace(_memory_manager=manager)
    AIAgent.shutdown_memory_provider(agent, transcript)
    assert assignments.call_count == 0, "shutdown scrubbed a whole transcript for a no-op hook"
    assert manager._shutdown_started


def test_real_session_end_consumer_still_gets_scrubbed_messages():
    class SessionEndProvider(HindsightMemoryProvider):
        def on_session_end(self, messages):
            self.seen = messages

    provider = SessionEndProvider()
    manager = MemoryManager()
    manager._providers = [provider]
    secret = "sk-test" + "a" * 40
    messages = [{"role": "tool", "content": "OPENAI_API_KEY=" + secret}]
    manager.on_session_end(messages)
    assert secret not in provider.seen[0]["content"]
    assert messages[0]["content"].endswith(secret), "caller transcript was mutated"
    manager.shutdown_all()


@pytest.mark.parametrize("mode", ["cloud", "local_embedded"])
def test_shutdown_all_caps_client_close_and_preserves_siblings(monkeypatch, mode, caplog):
    monkeypatch.setattr("plugins.memory.hindsight._CLIENT_CLOSE_TIMEOUT_S", 0.08, raising=False)
    cancelled = threading.Event()

    async def slow_close():
        try:
            await asyncio.wait_for(asyncio.Event().wait(), timeout=3)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    provider = HindsightMemoryProvider()
    provider._mode = mode
    provider._timeout = 10  # Request timeout must not become the close budget.
    inner = SimpleNamespace(aclose=slow_close)
    provider_client = inner if mode == "cloud" else SimpleNamespace(_client=inner, close=Mock())
    provider._client = provider_client
    sibling = SimpleNamespace(name="builtin", shutdown=Mock())
    manager = MemoryManager()
    manager._providers = [sibling, provider]
    started = time.monotonic()
    manager.shutdown_all()
    elapsed = time.monotonic() - started
    assert elapsed < 2, f"provider close escaped shutdown budget: {elapsed:.3f}s"
    sibling.shutdown.assert_called_once()
    assert cancelled.wait(0.3), "timed-out close was left running on the shared loop"
    assert "client close exceeded 0.08s cap" in caplog.text
    if mode == "local_embedded":
        provider_client.close.assert_not_called()


def test_embedded_wrapper_close_shares_the_same_cap(monkeypatch, caplog):
    monkeypatch.setattr("plugins.memory.hindsight._CLIENT_CLOSE_TIMEOUT_S", 0.08, raising=False)
    entered, release = threading.Event(), threading.Event()
    def blocked_close():
        entered.set()
        release.wait(3)
    provider = HindsightMemoryProvider()
    provider._mode = "local_embedded"
    provider._client = SimpleNamespace(_client=None, close=blocked_close)
    started = time.monotonic()
    try:
        with caplog.at_level("WARNING"):
            provider.shutdown()
        assert entered.is_set()
        assert time.monotonic() - started < 2
        assert "wrapper close exceeded" in caplog.text
    finally:
        release.set()


@pytest.mark.parametrize("active_thread", ["_writer_thread", "_prefetch_thread"])
def test_shutdown_skips_close_when_background_join_times_out(caplog, active_thread):
    provider = HindsightMemoryProvider()
    client = SimpleNamespace(aclose=Mock())
    provider._client = client
    thread = SimpleNamespace(is_alive=lambda: True, join=Mock())
    setattr(provider, active_thread, thread)
    with caplog.at_level("WARNING"):
        provider.shutdown()
    client.aclose.assert_not_called()
    assert provider._client is client
    assert "skipped client close: background writer/prefetch is still active" in caplog.text
    thread.join.assert_called_once()
