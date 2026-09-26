"""Tests for the Hindsight memory provider plugin.

Tests cover config loading, tool handlers (tags, max_tokens, types),
prefetch (auto_recall, preamble, query truncation), sync_turn (auto_retain,
turn counting, tags), and schema completeness.
"""

import importlib.util
import asyncio
import json
import os
import re
import stat
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from hermes_cli.memory_setup import _CANCELLED
from plugins.memory.hindsight import (
    HindsightMemoryProvider,
    RECALL_SCHEMA,
    REFLECT_SCHEMA,
    RETAIN_SCHEMA,
    _load_config,
    _load_simple_env,
    _build_embedded_profile_env,
    _normalize_observation_scopes,
    _normalize_retain_tags,
    _resolve_bank_id_template,
    _WRITER_SENTINEL,
    filter_retain_messages,
)
from plugins.memory.hindsight.settings import _sanitize_bank_segment


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


import tools.lazy_deps as _lazy_deps_at_import

_REAL_INSTALL_SPECS = _lazy_deps_at_import.install_specs


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    """Ensure no stale env vars or Windows home state leak between tests."""
    for key in (
        "HINDSIGHT_API_KEY", "HINDSIGHT_API_URL", "HINDSIGHT_BANK_ID",
        "HINDSIGHT_BUDGET", "HINDSIGHT_MODE", "HINDSIGHT_TIMEOUT",
        "HINDSIGHT_IDLE_TIMEOUT", "HINDSIGHT_LLM_API_KEY",
        "HINDSIGHT_RETAIN_TAGS", "HINDSIGHT_RETAIN_OBSERVATION_SCOPES",
        "HINDSIGHT_RETAIN_SOURCE",
        "HINDSIGHT_RETAIN_USER_PREFIX", "HINDSIGHT_RETAIN_ASSISTANT_PREFIX",
    ):
        monkeypatch.delenv(key, raising=False)

    # On Windows pathlib.Path.home() resolves USERPROFILE/HOMEDRIVE+HOMEPATH,
    # not the POSIX HOME alias that these tests historically monkeypatched.
    # Patch the actual API and keep all legacy profile writes in tmp_path.
    isolated_home = tmp_path / "user-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: isolated_home))

    # These tests provide client doubles, so they must not attempt a network
    # install merely because the optional SDK is absent from the test env.
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *args, **kwargs: None)
    # initialize() auto-upgrades an outdated installed SDK through install_specs; a mocked test must
    # never download or mutate the running environment. The dedicated upgrade tests override this.
    # (Setup-wizard tests reach install_specs too; they get a successful no-op, not a real install.)
    import tools.lazy_deps as _lazy_deps

    monkeypatch.setattr(_lazy_deps, "install_specs",
                        lambda *args, **kwargs: _lazy_deps.InstallSpecsResult(ok=True))

    # The update_mode='append' capability is cached process-wide per (API URL, key), and every
    # fixture here shares one URL and key: a capability test's mocked answer would otherwise decide
    # later tests' document IDs. Give each test a fresh cache, and a default probe that reports a
    # legacy API instead of contacting whatever listens on the fixture URL. Tests that need a modern
    # API patch the probe themselves.
    monkeypatch.setattr("plugins.memory.hindsight._append_capability_cache", {})
    monkeypatch.setattr("plugins.memory.hindsight._fetch_hindsight_api_version", lambda *a, **kw: None)

    # The retain-operation path imports this exception solely to classify a
    # fake client's response. Supply the smallest matching SDK surface so the
    # mocked tests remain runnable without the optional Hindsight extra.
    # Only when the real SDK is absent: shadowing an installed SDK with a
    # fake (no ``__path__``) breaks ``import hindsight_client`` and turns the
    # pinned-client test into a permanent skip.
    if importlib.util.find_spec("hindsight_client_api") is not None:
        return
    client_api = ModuleType("hindsight_client_api")
    exceptions = ModuleType("hindsight_client_api.exceptions")

    class NotFoundException(Exception):
        def __init__(self, *args, **kwargs):
            super().__init__(*args)

    exceptions.NotFoundException = NotFoundException
    client_api.exceptions = exceptions
    monkeypatch.setitem(sys.modules, "hindsight_client_api", client_api)
    monkeypatch.setitem(sys.modules, "hindsight_client_api.exceptions", exceptions)


@pytest.fixture(autouse=True)
def _stop_retain_writers(monkeypatch):
    """Join every provider's writer at teardown, while this test's patches still apply."""
    # Every provider a test builds may start a retain writer thread; stop them all while this
    # test's patches are still active, so no writer keeps retrying with restored globals or
    # leaks queued jobs into later tests (the retry backlog makes that leak observable).
    created: list = []
    original_init = HindsightMemoryProvider.__init__

    def _tracking_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(HindsightMemoryProvider, "__init__", _tracking_init)
    yield
    leaked = []
    for provider in created:
        provider._shutting_down.set()
        writer = provider._writer_thread
        if writer is not None and writer.is_alive():
            provider._retain_queue.put(_WRITER_SENTINEL)
            writer.join(timeout=5.0)
            if writer.is_alive():
                leaked.append(writer.name)
        provider._join_prefetch(5.0)
    assert not leaked, f"retain writer(s) still running after teardown: {leaked}"


def _make_mock_client():
    """Create a mock Hindsight client with async methods."""
    async def _aretain(
        bank_id,
        content,
        timestamp=None,
        context=None,
        document_id=None,
        metadata=None,
        entities=None,
        tags=None,
        update_mode=None,
        retain_async=None,
    ):
        return SimpleNamespace(ok=True)

    client = MagicMock()
    client.aretain = AsyncMock(side_effect=_aretain)
    client.arecall = AsyncMock(
        return_value=SimpleNamespace(
            results=[
                SimpleNamespace(text="Memory 1"),
                SimpleNamespace(text="Memory 2"),
            ]
        )
    )
    client.areflect = AsyncMock(
        return_value=SimpleNamespace(text="Synthesized answer")
    )
    client.aretain_batch = AsyncMock()
    client.aclose = AsyncMock()
    return client


def _provider_for_mode(tmp_path, monkeypatch, mode: str):
    """Create an initialized provider without pre-seeding its client."""
    config = {
        "mode": mode,
        "apiKey": "test-key",
        "api_url": "http://localhost:9999",
        "bank_id": "test-bank",
        "budget": "mid",
        "memory_mode": "hybrid",
    }
    config_path = tmp_path / "hindsight" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config))

    monkeypatch.setattr(
        "plugins.memory.hindsight.get_hermes_home", lambda: tmp_path
    )

    provider = HindsightMemoryProvider()
    provider.initialize(session_id="test-session", hermes_home=str(tmp_path), platform="cli")
    return provider


def _assert_cloud_client_lazy_installed_before_import(tmp_path, monkeypatch, mode: str):
    """Cloud/local-external clients must ensure lazy deps before importing."""
    import builtins

    provider = _provider_for_mode(tmp_path, monkeypatch, mode)
    ensure_calls = []

    def fake_ensure(feature, prompt=True):
        ensure_calls.append((feature, prompt))

    class FakeHindsight:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    real_import = builtins.__import__

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "hindsight_client":
            if ensure_calls != [("memory.hindsight", False)]:
                raise ModuleNotFoundError("No module named 'hindsight_client'")
            return SimpleNamespace(Hindsight=FakeHindsight)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("tools.lazy_deps.ensure", fake_ensure)
    monkeypatch.setattr(builtins, "__import__", guarded_import)

    client = provider._get_client()

    assert ensure_calls == [("memory.hindsight", False)]
    assert isinstance(client, FakeHindsight)
    assert client.kwargs == {
        "base_url": "http://localhost:9999",
        "timeout": 120.0,
        "api_key": "test-key",
    }


class _FakeSessionDB:
    def __init__(self, messages=None):
        self._messages = list(messages or [])

    def get_messages_as_conversation(self, session_id):
        return list(self._messages)


@pytest.fixture()
def provider(tmp_path, monkeypatch):
    """Create an initialized HindsightMemoryProvider with a mock client."""
    config = {
        "mode": "cloud",
        "apiKey": "test-key",
        "api_url": "http://localhost:9999",
        "bank_id": "test-bank",
        "budget": "mid",
        "memory_mode": "hybrid",
    }
    config_path = tmp_path / "hindsight" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config))

    monkeypatch.setattr(
        "plugins.memory.hindsight.get_hermes_home", lambda: tmp_path
    )

    p = HindsightMemoryProvider()
    p.initialize(session_id="test-session", hermes_home=str(tmp_path), platform="cli")
    p._client = _make_mock_client()
    return p


@pytest.fixture()
def provider_with_config(tmp_path, monkeypatch):
    """Create a provider factory that accepts custom config overrides."""
    def _make(**overrides):
        config = {
            "mode": "cloud",
            "apiKey": "test-key",
            "api_url": "http://localhost:9999",
            "bank_id": "test-bank",
            "budget": "mid",
            "memory_mode": "hybrid",
        }
        config.update(overrides)
        config_path = tmp_path / "hindsight" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config))

        monkeypatch.setattr(
            "plugins.memory.hindsight.get_hermes_home", lambda: tmp_path
        )

        p = HindsightMemoryProvider()
        p.initialize(session_id="test-session", hermes_home=str(tmp_path), platform="cli")
        p._client = _make_mock_client()
        return p
    return _make


def test_normalize_retain_tags_accepts_csv_and_dedupes():
    assert _normalize_retain_tags("agent:fakeassistantname, source_system:hermes-agent, agent:fakeassistantname") == [
        "agent:fakeassistantname",
        "source_system:hermes-agent",
    ]


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestSchemas:
    def test_retain_schema_has_content(self):
        assert RETAIN_SCHEMA["name"] == "hindsight_retain"
        assert "content" in RETAIN_SCHEMA["parameters"]["properties"]
        assert "tags" in RETAIN_SCHEMA["parameters"]["properties"]
        assert "content" in RETAIN_SCHEMA["parameters"]["required"]


    def test_get_tool_schemas_returns_three(self, provider):
        schemas = provider.get_tool_schemas()
        assert len(schemas) == 3
        names = {s["name"] for s in schemas}
        assert names == {"hindsight_retain", "hindsight_recall", "hindsight_reflect"}

    def test_context_mode_returns_no_tools(self, provider_with_config):
        p = provider_with_config(memory_mode="context")
        assert p.get_tool_schemas() == []

    def test_get_tool_schemas_before_initialize(self):
        """MemoryManager.add_provider() calls get_tool_schemas() BEFORE initialize().

        Reading a field that only initialize() sets raises AttributeError there, and
        agent_init catches it by dropping the whole manager — so one missing default
        silently disables memory (retain AND recall) for every session.
        """
        p = HindsightMemoryProvider()
        names = {s["name"] for s in p.get_tool_schemas()}
        assert names == {"hindsight_retain", "hindsight_recall", "hindsight_reflect"}


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestConfig:
    def test_cloud_client_lazy_installs_dependency_before_import(self, tmp_path, monkeypatch):
        _assert_cloud_client_lazy_installed_before_import(tmp_path, monkeypatch, "cloud")


    def test_default_values(self, provider):
        assert provider._auto_retain is True
        assert provider._auto_recall is True
        assert provider._retain_every_n_turns == 1
        assert provider._recall_max_tokens == 4096
        assert provider._recall_max_input_chars == 800
        assert provider._tags is None
        assert provider._observation_scopes is None
        assert provider._recall_tags is None
        # Default recall narrowed to observation-only; world/experience are
        # aggregate facts that often crowd out concrete-event signal during
        # auto-recall. Users opt back in via the recall_types config key.
        assert provider._recall_types == ["observation"]
        assert provider._bank_mission == ""
        assert provider._bank_retain_mission is None
        assert provider._retain_context == "conversation between Hermes Agent and the User"

    def test_recall_types_default_is_observation_only(self, provider):
        """Auto-recall must filter to observation by default."""
        assert provider._recall_types == ["observation"]

    def test_explicit_empty_recall_types_disables_the_filter(self, provider_with_config):
        """Only an unset key gets the observation default; ``[]`` matches the empty string."""
        assert provider_with_config(recall_types=[])._recall_types == []
        assert provider_with_config(recall_types="")._recall_types == []


    def test_observation_scopes_keyword_config(self, provider_with_config):
        p = provider_with_config(observation_scopes="per_tag")
        assert p._observation_scopes == "per_tag"


    def test_custom_config_values(self, provider_with_config):
        p = provider_with_config(
            retain_tags=["tag1", "tag2"],
            retain_source="hermes",
            retain_user_prefix="User (fakeusername)",
            retain_assistant_prefix="Assistant (fakeassistantname)",
            recall_tags=["recall-tag"],
            recall_tags_match="all",
            auto_retain=False,
            auto_recall=False,
            retain_every_n_turns=3,
            retain_context="custom-ctx",
            bank_retain_mission="Extract key facts",
            recall_max_tokens=2048,
            recall_types=["world", "experience"],
            recall_prompt_preamble="Custom preamble:",
            recall_max_input_chars=500,
            bank_mission="Test agent mission",
        )
        assert p._tags == ["tag1", "tag2"]
        assert p._retain_tags == ["tag1", "tag2"]
        assert p._retain_source == "hermes"
        assert p._retain_user_prefix == "User (fakeusername)"
        assert p._retain_assistant_prefix == "Assistant (fakeassistantname)"
        assert p._recall_tags == ["recall-tag"]
        assert p._recall_tags_match == "all"
        assert p._auto_retain is False
        assert p._auto_recall is False
        assert p._retain_every_n_turns == 3
        assert p._retain_context == "custom-ctx"
        assert p._bank_retain_mission == "Extract key facts"
        assert p._recall_max_tokens == 2048
        assert p._recall_types == ["world", "experience"]
        assert p._recall_prompt_preamble == "Custom preamble:"
        assert p._recall_max_input_chars == 500
        assert p._bank_mission == "Test agent mission"

    def test_retain_source_defaults_empty(self, provider):
        # Opt-in per AGENTS.md: no attribution tag ships by default.
        assert provider._retain_source == ""

    def test_retain_source_absent_from_metadata_by_default(self, provider):
        # metadata.source is stamped only when the user sets retain_source.
        meta = provider._build_metadata(message_count=2, turn_index=1)
        assert "source" not in meta

    def test_retain_source_user_override_wins(self, provider_with_config):
        # Users can still opt in explicitly (config key / env var).
        p = provider_with_config(retain_source="cogoport")
        assert p._retain_source == "cogoport"
        assert p._build_metadata(message_count=2, turn_index=1)["source"] == "cogoport"

    def test_embedded_profile_env_includes_idle_timeout_from_config(self):
        env = _build_embedded_profile_env({
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "idle_timeout": 0,
        })

        assert env["HINDSIGHT_EMBED_DAEMON_IDLE_TIMEOUT"] == "0"


    def test_get_client_passes_idle_timeout_to_hindsight_embedded(self, monkeypatch):
        captured = {}

        class FakeHindsightEmbedded:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setitem(sys.modules, "hindsight", SimpleNamespace(HindsightEmbedded=FakeHindsightEmbedded))
        monkeypatch.setattr("plugins.memory.hindsight._check_local_runtime", lambda: (True, ""))

        p = HindsightMemoryProvider()
        p._mode = "local_embedded"
        p._config = {
            "profile": "hermes",
            "llm_provider": "openai_compatible",
            "llm_api_key": "test-key",
            "llm_model": "test-model",
            "idle_timeout": 0,
        }
        p._llm_base_url = "http://localhost:8060/v1"

        p._get_client()

        assert captured["idle_timeout"] == 0
        assert captured["llm_provider"] == "openai"


class TestPostSetup:
    def test_setup_cancel_at_mode_picker_writes_nothing(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes-home"
        user_home = tmp_path / "user-home"
        user_home.mkdir()
        monkeypatch.setenv("HOME", str(user_home))
        monkeypatch.setattr("plugins.memory.hindsight.get_hermes_home", lambda: hermes_home)

        save_config = MagicMock()
        which = MagicMock(return_value="/usr/bin/uv")
        run = MagicMock()
        monkeypatch.setattr("hermes_cli.memory_setup._curses_select", lambda *args, **kwargs: _CANCELLED)
        monkeypatch.setattr("shutil.which", which)
        monkeypatch.setattr("subprocess.run", run)
        monkeypatch.setattr("builtins.input", MagicMock(side_effect=AssertionError("prompt should not run")))
        monkeypatch.setattr("getpass.getpass", MagicMock(side_effect=AssertionError("prompt should not run")))
        monkeypatch.setattr("hermes_cli.config.save_config", save_config)

        provider = HindsightMemoryProvider()
        provider.post_setup(str(hermes_home), {"memory": {"provider": "builtin"}})

        save_config.assert_not_called()
        which.assert_not_called()
        run.assert_not_called()
        assert not (hermes_home / ".env").exists()
        assert not (hermes_home / "hindsight" / "config.json").exists()
        assert not (user_home / ".hindsight" / "profiles" / "hermes.env").exists()


    def test_local_embedded_setup_materializes_profile_env(self, tmp_path, monkeypatch):
        hermes_home = tmp_path / "hermes-home"
        user_home = tmp_path / "user-home"
        user_home.mkdir()
        monkeypatch.setenv("HOME", str(user_home))

        selections = iter([1, 0])  # local_embedded, openai
        monkeypatch.setattr("hermes_cli.memory_setup._curses_select", lambda *args, **kwargs: next(selections))
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "sk-local-test")
        saved_configs = []
        monkeypatch.setattr("hermes_cli.config.save_config", lambda cfg: saved_configs.append(cfg.copy()))

        provider = HindsightMemoryProvider()
        provider.post_setup(str(hermes_home), {"memory": {}})

        assert saved_configs[-1]["memory"]["provider"] == "hindsight"
        env_text = (hermes_home / ".env").read_text()
        assert "HINDSIGHT_LLM_API_KEY=sk-local-test\n" in env_text
        assert "HINDSIGHT_TIMEOUT=120\n" in env_text
        assert "HINDSIGHT_IDLE_TIMEOUT=300\n" in env_text

        profile_env = user_home / ".hindsight" / "profiles" / "hermes.env"
        assert profile_env.exists()
        assert profile_env.read_text() == (
            "HINDSIGHT_API_LLM_PROVIDER=openai\n"
            "HINDSIGHT_API_LLM_API_KEY=sk-local-test\n"
            "HINDSIGHT_API_LLM_MODEL=gpt-4o-mini\n"
            "HINDSIGHT_API_LOG_LEVEL=info\n"
            "HINDSIGHT_EMBED_DAEMON_IDLE_TIMEOUT=300\n"
        )


# ---------------------------------------------------------------------------
# Tool handler tests
# ---------------------------------------------------------------------------


class TestToolHandlers:
    def test_retain_success(self, provider):
        result = json.loads(provider.handle_tool_call(
            "hindsight_retain", {"content": "user likes dark mode"}
        ))
        assert result["result"] == "Memory stored successfully."
        provider._client.aretain_batch.assert_called_once()
        call_kwargs = provider._client.aretain_batch.call_args.kwargs
        assert call_kwargs["bank_id"] == "test-bank"
        item = call_kwargs["items"][0]
        assert item["content"] == "user likes dark mode"
        # bank_id/retain_async are call-level args, never item keys.
        assert "bank_id" not in item
        assert "retain_async" not in item

    def test_retain_defaults_item_timestamp_when_no_occurred_at(self, provider, monkeypatch):
        event_time = datetime(2026, 8, 24, 9, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
        monkeypatch.setattr("plugins.memory.hindsight._hermes_now", lambda: event_time)
        result = json.loads(provider.handle_tool_call(
            "hindsight_retain", {"content": "user likes dark mode"}
        ))
        assert result["result"] == "Memory stored successfully."
        item = provider._client.aretain_batch.call_args.kwargs["items"][0]
        # Non-temporal retains still carry a defaulted event timestamp so the
        # server can resolve any relative time phrases (#93568).
        assert item["timestamp"] == event_time.isoformat(timespec="seconds")

    def test_retain_threads_explicit_occurred_at_into_item_timestamp(self, provider):
        result = json.loads(provider.handle_tool_call(
            "hindsight_retain",
            {"content": "user visited Paris", "occurred_at": "2026-03-03"},
        ))
        assert result["result"] == "Memory stored successfully."
        item = provider._client.aretain_batch.call_args.kwargs["items"][0]
        assert item["timestamp"] == "2026-03-03"

    def test_retain_ignores_blank_occurred_at(self, provider, monkeypatch):
        event_time = datetime(2026, 8, 24, 9, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
        monkeypatch.setattr("plugins.memory.hindsight._hermes_now", lambda: event_time)
        json.loads(provider.handle_tool_call(
            "hindsight_retain", {"content": "hello", "occurred_at": "   "}
        ))
        item = provider._client.aretain_batch.call_args.kwargs["items"][0]
        assert item["timestamp"] == event_time.isoformat(timespec="seconds")

    def test_build_retain_kwargs_accepts_explicit_occurred_at(self, provider):
        item = provider._build_retain_kwargs("dinner with Sam", occurred_at="2026-08-20T19:00:00+02:00")
        assert item["timestamp"] == "2026-08-20T19:00:00+02:00"

    def test_build_retain_kwargs_omits_strategy_when_unset(self, provider):
        """Default is no strategy key at all, so the bank keeps deciding."""
        assert "strategy" not in provider._build_retain_kwargs("hello")

    def test_build_retain_kwargs_sets_configured_strategy(self, provider):
        """A configured strategy rides on every item, steering extraction for this content type."""
        provider._retain_strategy = "agent-session"
        assert provider._build_retain_kwargs("hello")["strategy"] == "agent-session"

    def test_retain_strategy_is_exposed_as_a_setting(self, provider):
        """Operators must be able to set it without editing code."""
        keys = {opt["key"] for opt in provider.get_config_schema()}
        assert "retain_strategy" in keys

    def test_retain_schema_exposes_occurred_at(self):
        from plugins.memory.hindsight import RETAIN_SCHEMA

        props = RETAIN_SCHEMA["parameters"]["properties"]
        assert "occurred_at" in props
        assert props["occurred_at"]["type"] == "string"
        # The description must steer the model to pass event times.
        assert "event" in props["occurred_at"]["description"].lower()
        assert "occurred_at" not in RETAIN_SCHEMA["parameters"]["required"]


    def test_recall_success(self, provider):
        result = json.loads(provider.handle_tool_call(
            "hindsight_recall", {"query": "dark mode"}
        ))
        assert "Memory 1" in result["result"]
        assert "Memory 2" in result["result"]


    def test_reflect_success(self, provider):
        result = json.loads(provider.handle_tool_call(
            "hindsight_reflect", {"query": "summarize"}
        ))
        assert result["result"] == "Synthesized answer"


    def test_unknown_tool(self, provider):
        result = json.loads(provider.handle_tool_call(
            "hindsight_unknown", {}
        ))
        assert "error" in result


    def test_local_embedded_recall_reconnects_after_idle_shutdown(self, provider, monkeypatch):
        first_client = _make_mock_client()
        first_client.arecall.side_effect = RuntimeError("Cannot connect to host 127.0.0.1:8888")
        second_client = _make_mock_client()
        second_client.arecall.return_value = SimpleNamespace(
            results=[SimpleNamespace(text="Recovered memory")]
        )
        clients = iter([first_client, second_client])

        provider._mode = "local_embedded"
        provider._client = first_client
        monkeypatch.setattr(provider, "_get_client", lambda: next(clients))

        result = json.loads(provider.handle_tool_call(
            "hindsight_recall", {"query": "test"}
        ))

        assert result["result"] == "1. Recovered memory"
        assert provider._client is second_client
        first_client.arecall.assert_called_once()
        second_client.arecall.assert_called_once()


# ---------------------------------------------------------------------------
# Prefetch tests
# ---------------------------------------------------------------------------


class TestPrefetch:
    def test_prefetch_returns_empty_when_no_result(self, provider):
        assert provider.prefetch("test") == ""


    def test_recall_sync_defaults_off(self, provider):
        assert provider._recall_sync is False

    def test_recall_sync_recalls_current_query_synchronously(self, provider_with_config):
        # recall_sync=True: prefetch() must do a live recall against the
        # *current* query (not read a previously queued buffer). #5820
        p = provider_with_config(recall_sync=True)
        captured = {}

        def _capture_recall(**kwargs):
            captured["query"] = kwargs.get("query", "")
            return SimpleNamespace(results=[SimpleNamespace(text="fresh memory")])

        p._client.arecall = AsyncMock(side_effect=_capture_recall)

        # Nothing pre-buffered — proves the result comes from a live recall.
        assert p._prefetch_result == ""
        result = p.prefetch("fix tests")

        assert captured["query"] == "fix tests"       # current query, not ignored
        assert "fresh memory" in result
        p._client.arecall.assert_called_once()

    def test_recall_sync_skips_background_queue(self, provider_with_config):
        # With sync recall there's nothing to prime in the background.
        p = provider_with_config(recall_sync=True)
        p.queue_prefetch("anything")
        assert p._prefetch_thread is None

    def test_async_default_ignores_current_query_and_reads_buffer(self, provider):
        # Default (recall_sync off): prefetch returns the buffered result and
        # does NOT issue a live recall for the current query.
        provider._prefetch_result = "- buffered from previous turn"
        result = provider.prefetch("a totally different current query")
        assert "buffered from previous turn" in result
        provider._client.arecall.assert_not_called()

    def test_queue_prefetch_skipped_in_tools_mode(self, provider_with_config):
        p = provider_with_config(memory_mode="tools")
        p.queue_prefetch("test")
        # Should not start a thread
        assert p._prefetch_thread is None

    def test_prefetch_waits_for_pending_retain_before_recall(self, provider):
        """The background prefetch must wait for queued retains to drain so the
        next turn's recall observes the just-completed turn (no retain race)."""
        import threading

        order = []
        release = threading.Event()

        async def _slow_retain(*args, **kwargs):
            release.wait(timeout=5.0)
            order.append("retain")

        async def _recall(**kwargs):
            order.append("recall")
            return SimpleNamespace(results=[SimpleNamespace(text="m")])

        provider._client.aretain_batch = AsyncMock(side_effect=_slow_retain)
        provider._client.arecall = AsyncMock(side_effect=_recall)

        # Enqueue a slow retain, then immediately queue the next-turn prefetch.
        provider.sync_turn("hello", "world")
        provider.queue_prefetch("next turn query")

        # Let the prefetch thread start and reach the drain barrier.
        time.sleep(0.2)
        assert order == [], "recall ran before the pending retain drained"

        # Release the retain; the prefetch should now proceed AFTER it.
        release.set()
        if provider._prefetch_thread:
            provider._prefetch_thread.join(timeout=5.0)
        provider._retain_queue.join()
        assert order and order[0] == "retain"
        assert "recall" in order

    def test_prefetch_wait_for_retain_can_be_disabled(self, provider_with_config):
        p = provider_with_config(prefetch_waits_for_retain=False)
        p._client = _make_mock_client()
        assert p._prefetch_waits_for_retain is False


class TestPrefetchServerRetainVisibility:
    """PR #62871 review follow-up: draining the local writer queue is not a
    read-after-write signal for async retains. With ``retain_async=True`` the
    server accepts the write and returns an ``operation_id`` that stays
    ``pending`` until the write is durable/recall-visible. The background
    prefetch must gate on server-side operation completion, not just the local
    queue, before recalling.
    """

    def _client_with_ops(self, statuses):
        """Mock client whose aretain_batch returns an async operation_id and
        whose operations.get_operation_status yields *statuses* in order
        (last value repeats)."""
        client = _make_mock_client()
        client.aretain_batch = AsyncMock(
            return_value=SimpleNamespace(operation_id="op-1", operation_ids=None)
        )
        seq = list(statuses)

        async def _status(**kwargs):
            value = seq.pop(0) if len(seq) > 1 else seq[0]
            return SimpleNamespace(status=value)

        client.operations = MagicMock()
        client.operations.get_operation_status = AsyncMock(side_effect=_status)
        return client

    def test_tracks_async_operation_id_from_retain(self, provider):
        provider._client.aretain_batch = AsyncMock(
            return_value=SimpleNamespace(operation_id="op-async-1", operation_ids=None)
        )
        provider.sync_turn("hello", "world")
        provider._retain_queue.join()
        assert "op-async-1" in provider._pending_retain_ops

    def test_tracks_multiple_operation_ids(self, provider):
        provider._client.aretain_batch = AsyncMock(
            return_value=SimpleNamespace(
                operation_id=None, operation_ids=["op-a", "op-b"]
            )
        )
        provider.sync_turn("hello", "world")
        provider._retain_queue.join()
        assert {"op-a", "op-b"} <= provider._pending_retain_ops

    def test_sync_retain_tracks_no_ops(self, provider_with_config):
        p = provider_with_config(retain_async=False)
        p._client = _make_mock_client()
        p._client.aretain_batch = AsyncMock(
            return_value=SimpleNamespace(operation_id="op-x", operation_ids=None)
        )
        p.sync_turn("hello", "world")
        p._retain_queue.join()
        # retain_async=False → no server-side op to wait on.
        assert p._pending_retain_ops == set()

    def test_prefetch_waits_for_server_completion_before_recall(self, provider):
        """Recall must not run until the tracked async op reports completed."""
        order = []

        async def _recall(**kwargs):
            order.append("recall")
            return SimpleNamespace(results=[SimpleNamespace(text="m")])

        provider._client = self._client_with_ops(["pending", "pending", "completed"])
        provider._client.arecall = AsyncMock(side_effect=_recall)

        provider.sync_turn("hello", "world")
        provider._retain_queue.join()
        assert "op-1" in provider._pending_retain_ops

        provider.queue_prefetch("next turn query")
        if provider._prefetch_thread:
            provider._prefetch_thread.join(timeout=5.0)

        # Recall ran, the op was polled to completion, and the pending set
        # was cleared (so a later prefetch won't re-poll it).
        assert order == ["recall"]
        assert provider._client.operations.get_operation_status.await_count >= 3
        assert provider._pending_retain_ops == set()

    def test_prefetch_proceeds_after_server_wait_timeout(self, provider_with_config):
        """A wedged/never-completing async op must not hang prefetch forever;
        it recalls anyway once the drain budget is exhausted."""
        p = provider_with_config(prefetch_retain_drain_timeout=0.3)
        order = []

        async def _recall(**kwargs):
            order.append("recall")
            return SimpleNamespace(results=[SimpleNamespace(text="m")])

        p._client = self._client_with_ops(["pending"])  # never completes
        p._client.arecall = AsyncMock(side_effect=_recall)

        p.sync_turn("hello", "world")
        p._retain_queue.join()

        start = time.monotonic()
        p.queue_prefetch("next turn query")
        if p._prefetch_thread:
            p._prefetch_thread.join(timeout=5.0)
        elapsed = time.monotonic() - start

        assert order == ["recall"], "prefetch should recall after the timeout"
        assert elapsed < 3.0, "prefetch must not block well past the drain budget"

    def test_pending_ops_are_polled_against_their_own_bank(self, provider):
        """A session-scoped bank template can leave an old-session op pending when the next
        session retains to another bank; each op's status must be queried in the bank it was
        retained to, or the other bank's 404 reads as completion and the barrier lies."""
        polled = []

        async def _status(*, bank_id, operation_id):
            polled.append((operation_id, bank_id))
            return SimpleNamespace(status="completed")

        provider._client.operations = MagicMock()
        provider._client.operations.get_operation_status = AsyncMock(side_effect=_status)
        provider._track_retain_ops(SimpleNamespace(operation_id="old-op", operation_ids=None), "old-bank")
        provider._track_retain_ops(SimpleNamespace(operation_id="new-op", operation_ids=None), "new-bank")

        assert provider._wait_for_server_retain_ops(lambda: False, 5.0) is True
        assert sorted(polled) == [("new-op", "new-bank"), ("old-op", "old-bank")]
        assert provider._pending_retain_ops == set()

    def test_timed_out_ops_are_dropped_not_repolled(self, provider_with_config):
        """Ops unresolved at deadline must be EVICTED so a permanently failing
        status endpoint can't make every later prefetch re-burn the full
        timeout on a growing pending set (unbounded session-wide degradation
        + reply-path join penalty)."""
        p = provider_with_config(prefetch_retain_drain_timeout=0.3)
        p._client = self._client_with_ops(["pending"])  # never completes
        p._client.arecall = AsyncMock(
            return_value=SimpleNamespace(results=[SimpleNamespace(text="m")])
        )

        p.sync_turn("hello", "world")
        p._retain_queue.join()
        assert p._pending_retain_ops, "op should be tracked before the wait"

        # First prefetch burns the budget and must DROP the wedged op.
        p.queue_prefetch("q1")
        if p._prefetch_thread:
            p._prefetch_thread.join(timeout=5.0)
        assert p._pending_retain_ops == set(), (
            "unresolved ops must be evicted at deadline, not retained"
        )

        # A later prefetch must not poll the dropped op again (counted, not timed).
        polls = p._client.operations.get_operation_status.await_count
        p.queue_prefetch("q2")
        if p._prefetch_thread:
            p._prefetch_thread.join(timeout=5.0)
            assert not p._prefetch_thread.is_alive()
        assert p._client.operations.get_operation_status.await_count == polls, (
            "second prefetch re-polled dropped ops — eviction regressed"
        )

    def test_drain_budget_bounds_the_status_request_itself(self, provider_with_config):
        """The drain deadline must bound each status request, not only the loop checks: a hung
        status endpoint otherwise holds the prefetch for the full request timeout (120s default)."""
        p = provider_with_config(timeout=30)

        async def _hung(*, bank_id, operation_id):
            await asyncio.sleep(10)
            return SimpleNamespace(status="completed")

        p._client.operations = MagicMock()
        p._client.operations.get_operation_status = AsyncMock(side_effect=_hung)
        p._track_retain_ops(SimpleNamespace(operation_id="op-1", operation_ids=None), "test-bank")

        start = time.monotonic()
        assert p._wait_for_retains_drained(0.3) is False
        assert time.monotonic() - start < 2.5
        assert p._pending_retain_ops == set()

    def test_embedded_reconnect_retry_gets_only_the_remaining_budget(self, provider):
        """The local_embedded reconnect retry must spend what the first attempt left of the
        caller's budget, not a fresh copy of it."""
        provider._mode = "local_embedded"
        budgets, calls = [], []
        real_run_sync = provider._run_sync

        def _spy(coro, *, keep_pending=None, timeout=None):
            budgets.append(timeout)
            return real_run_sync(coro, keep_pending=keep_pending, timeout=timeout)

        async def _status(*, bank_id, operation_id):
            calls.append(1)
            if len(calls) == 1:
                await asyncio.sleep(0.2)
                raise RuntimeError("Cannot connect to host 127.0.0.1:8888")
            return SimpleNamespace(status="completed")

        client = _make_mock_client()
        client.operations = MagicMock()
        client.operations.get_operation_status = AsyncMock(side_effect=_status)
        provider._client = client
        provider._get_client = lambda: client
        provider._run_sync = _spy

        assert provider._is_retain_op_complete("bank", "op-1", timeout=1.0) is True
        assert budgets[0] == 1.0
        assert len(budgets) == 2 and budgets[1] <= 0.85, budgets

    def test_embedded_reconnect_is_skipped_when_the_budget_is_spent(self, provider):
        """A slow client recreation that consumes the rest of the budget means no retry."""
        provider._mode = "local_embedded"
        calls = []

        async def _status(*, bank_id, operation_id):
            calls.append(1)
            raise RuntimeError("Cannot connect to host 127.0.0.1:8888")

        client = _make_mock_client()
        client.operations = MagicMock()
        client.operations.get_operation_status = AsyncMock(side_effect=_status)
        provider._client = client

        def _slow_recreate():
            if provider._client is None:
                time.sleep(0.3)
            return client

        provider._get_client = _slow_recreate
        assert provider._is_retain_op_complete("bank", "op-1", timeout=0.2) is False
        assert len(calls) == 1

    def test_operation_notfound_treated_as_complete(self, provider):
        """A NotFound (completed+evicted) op is treated as done, not pending."""
        exceptions = pytest.importorskip(
            "hindsight_client_api.exceptions",
            reason="Hindsight SDK is not installed",
        )
        NotFoundException = exceptions.NotFoundException

        client = _make_mock_client()
        client.operations = MagicMock()
        client.operations.get_operation_status = AsyncMock(
            side_effect=NotFoundException(status=404, reason="gone")
        )
        provider._client = client

        assert provider._is_retain_op_complete("bank", "op-gone") is True

    def test_transient_status_error_keeps_waiting(self, provider):
        """A transient status-check error means 'unknown', so keep waiting."""
        client = _make_mock_client()
        client.operations = MagicMock()
        client.operations.get_operation_status = AsyncMock(
            side_effect=RuntimeError("temporary blip")
        )
        provider._client = client

        assert provider._is_retain_op_complete("bank", "op-1") is False



# ---------------------------------------------------------------------------
# recall_status (deterministic recall indicator) tests
# ---------------------------------------------------------------------------


class TestPrefetchSupersession:
    def test_superseded_slow_worker_cannot_overwrite_newer_result(self, provider):
        """A worker that outlives prefetch()'s capped join must not publish over a newer
        request's completed recall in the same session."""
        release_old = threading.Event()
        old_started = threading.Event()

        async def _recall(**kwargs):
            if kwargs.get("query") == "old":
                old_started.set()
                while not release_old.is_set():
                    await asyncio.sleep(0.01)
                return SimpleNamespace(results=[SimpleNamespace(text="old memory")])
            return SimpleNamespace(results=[SimpleNamespace(text="new memory")])

        provider._client.arecall = AsyncMock(side_effect=_recall)
        provider._prefetch_waits_for_retain = False
        provider.queue_prefetch("old")
        old_worker = provider._prefetch_thread
        assert old_started.wait(5.0)
        provider.queue_prefetch("new")
        provider._prefetch_thread.join(timeout=5.0)
        assert "new memory" in provider._prefetch_result

        release_old.set()
        old_worker.join(timeout=5.0)
        assert not old_worker.is_alive()
        assert "new memory" in provider._prefetch_result
        assert "old memory" not in provider._prefetch_result

    def test_empty_newer_recall_does_not_inject_older_query_memories(self, provider):
        """An older worker's result buffered after prefetch()'s capped join must not survive a
        newer query that found nothing: the next turn would get the older query's memories."""
        async def _recall(**kwargs):
            if kwargs.get("query") == "old":
                return SimpleNamespace(results=[SimpleNamespace(text="old memory")])
            return SimpleNamespace(results=[])

        provider._client.arecall = AsyncMock(side_effect=_recall)
        provider._prefetch_waits_for_retain = False
        provider.queue_prefetch("old")
        provider._prefetch_thread.join(timeout=5.0)
        assert "old memory" in provider._prefetch_result  # landed, never consumed
        provider.queue_prefetch("new")
        provider._prefetch_thread.join(timeout=5.0)
        assert "old memory" not in provider.prefetch("new")

class TestMissionConfig:
    def test_configured_missions_are_applied_once_per_bank_before_retain(self, provider_with_config):
        """``bank_mission``/``bank_retain_mission`` are documented as applied via the Banks API;
        they must reach the bank (each resolved bank once), not only sit on the provider."""
        p = provider_with_config(bank_mission="Reflect framing", bank_retain_mission="Extract decisions")
        p._client.acreate_bank = AsyncMock(return_value=SimpleNamespace())
        p._client.aretain_batch = AsyncMock(return_value=SimpleNamespace(operation_id=None, operation_ids=None))

        p._retain_batch({"content": "a"}, bank_id="bank-a")
        p._retain_batch({"content": "b"}, bank_id="bank-a")
        p._retain_batch({"content": "c"}, bank_id="bank-b")

        calls = [c.kwargs for c in p._client.acreate_bank.await_args_list]
        assert calls == [
            {"bank_id": "bank-a", "reflect_mission": "Reflect framing", "retain_mission": "Extract decisions"},
            {"bank_id": "bank-b", "reflect_mission": "Reflect framing", "retain_mission": "Extract decisions"},
        ]
        assert p._client.aretain_batch.await_count == 3

    def test_concurrent_caller_waits_for_the_bank_mission_in_flight(self, provider_with_config):
        """A second retain/reflect for the same bank must not reach the server while the first
        caller is still applying that bank's missions."""
        p = provider_with_config(bank_retain_mission="Extract decisions")
        order: list = []
        started, release = threading.Event(), threading.Event()

        async def _create(**_kw):
            order.append("mission-start")
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            order.append("mission-applied")
            return SimpleNamespace()

        async def _retain(**_kw):
            order.append("retain")
            return SimpleNamespace(operation_id=None, operation_ids=None)

        p._client.acreate_bank = AsyncMock(side_effect=_create)
        p._client.aretain_batch = AsyncMock(side_effect=_retain)
        first = threading.Thread(target=p._retain_batch, args=({"content": "a"},), kwargs={"bank_id": "bank-a"})
        first.start()
        assert started.wait(5.0)
        second = threading.Thread(target=p._retain_batch, args=({"content": "b"},), kwargs={"bank_id": "bank-a"})
        second.start()
        time.sleep(0.2)
        assert order == ["mission-start"], order
        release.set()
        first.join(5.0)
        second.join(5.0)
        assert order[:2] == ["mission-start", "mission-applied"]
        assert order.count("retain") == 2
        assert p._client.acreate_bank.await_count == 1

    def test_waiter_does_not_overtake_a_mission_applied_after_embedded_reconnect(self, provider_with_config):
        """With a short provider timeout, a reconnect retry that finishes the mission late must
        still hold concurrent callers for that bank until the mission is applied."""
        p = provider_with_config(bank_retain_mission="Extract decisions", timeout=1)
        p._timeout = 0.5
        p._mode = "local_embedded"
        order: list = []
        attempts: list = []
        started = threading.Event()

        async def _create(**_kw):
            attempts.append(1)
            started.set()
            if len(attempts) == 1:
                await asyncio.sleep(0.3)
                raise RuntimeError("Cannot connect to host 127.0.0.1:8888")
            # The retry outlives the 0.5s provider timeout (lands ~0.65s) but succeeds.
            await asyncio.sleep(0.35)
            order.append("mission-applied")
            return SimpleNamespace()

        async def _retain(**_kw):
            order.append("retain")
            return SimpleNamespace(operation_id=None, operation_ids=None)

        client = p._client
        client.acreate_bank = AsyncMock(side_effect=_create)
        client.aretain_batch = AsyncMock(side_effect=_retain)
        p._get_client = lambda: client
        first = threading.Thread(target=p._retain_batch, args=({"content": "a"},), kwargs={"bank_id": "bank-a"})
        first.start()
        assert started.wait(5.0)
        second = threading.Thread(target=p._retain_batch, args=({"content": "b"},), kwargs={"bank_id": "bank-a"})
        second.start()
        first.join(5.0)
        second.join(5.0)
        assert len(attempts) == 2
        assert order == ["mission-applied", "retain", "retain"], order

    def test_no_missions_configured_makes_no_bank_calls(self, provider):
        provider._client.acreate_bank = AsyncMock()
        provider._client.aretain_batch = AsyncMock(return_value=SimpleNamespace(operation_id=None, operation_ids=None))
        provider._retain_batch({"content": "a"}, bank_id="bank-a")
        provider._client.acreate_bank.assert_not_awaited()

    def test_mission_failure_does_not_block_retain(self, provider_with_config):
        p = provider_with_config(bank_retain_mission="Extract decisions")
        p._client.acreate_bank = AsyncMock(side_effect=RuntimeError("banks api down"))
        p._client.aretain_batch = AsyncMock(return_value=SimpleNamespace(operation_id=None, operation_ids=None))
        p._retain_batch({"content": "a"}, bank_id="bank-a")
        p._retain_batch({"content": "b"}, bank_id="bank-a")
        assert p._client.aretain_batch.await_count == 2
        assert p._client.acreate_bank.await_count == 1  # best effort, not retried every retain


class TestRecallStatus:
    def test_none_before_any_prefetch(self, provider):
        # Nothing recalled yet → no indicator.
        assert provider.recall_status() is None

    def test_reports_count_after_recall(self, provider):
        # Mock client returns 2 memories; prefetch consumes the block.
        provider.queue_prefetch("test")
        if provider._prefetch_thread:
            provider._prefetch_thread.join(timeout=5.0)
        provider.prefetch("test")

        status = provider.recall_status()
        assert status is not None
        assert status.provider_label == "Hindsight"
        assert status.count == 2

    def test_reports_count_in_recall_sync_mode(self, provider_with_config):
        # recall_sync path does a live recall inside prefetch() (no background
        # prime) — the indicator must still report the count for that turn.
        p = provider_with_config(recall_sync=True)
        assert p.prefetch("test")  # live recall returns the 2 mock memories
        status = p.recall_status()
        assert status is not None
        assert status.count == 2

    def test_none_when_recall_returned_nothing(self, provider):
        provider._client.arecall = AsyncMock(
            return_value=SimpleNamespace(results=[])
        )
        provider.queue_prefetch("test")
        if provider._prefetch_thread:
            provider._prefetch_thread.join(timeout=5.0)
        assert provider.prefetch("test") == ""
        assert provider.recall_status() is None

    def test_stale_count_cleared_on_empty_turn(self, provider):
        # First turn recalls 2 memories.
        provider.queue_prefetch("test")
        if provider._prefetch_thread:
            provider._prefetch_thread.join(timeout=5.0)
        provider.prefetch("test")
        assert provider.recall_status().count == 2

        # Next turn recalls nothing — the prior count must not linger.
        provider._client.arecall = AsyncMock(
            return_value=SimpleNamespace(results=[])
        )
        provider.queue_prefetch("test2")
        if provider._prefetch_thread:
            provider._prefetch_thread.join(timeout=5.0)
        provider.prefetch("test2")
        assert provider.recall_status() is None

    def test_suppressed_when_indicator_off(self, provider_with_config):
        p = provider_with_config(recall_indicator=False)
        p.queue_prefetch("test")
        if p._prefetch_thread:
            p._prefetch_thread.join(timeout=5.0)
        p.prefetch("test")
        # Memory was injected, but the indicator is turned off.
        assert p._last_recall_returned is True
        assert p.recall_status() is None

    def test_reflect_mode_reports_generic_count(self, provider_with_config):
        p = provider_with_config(recall_prefetch_method="reflect")
        p.queue_prefetch("test")
        if p._prefetch_thread:
            p._prefetch_thread.join(timeout=5.0)
        p.prefetch("test")
        status = p.recall_status()
        assert status is not None
        # Reflect synthesizes across memories → no discrete count (0).
        assert status.count == 0


# ---------------------------------------------------------------------------
# sync_turn tests
# ---------------------------------------------------------------------------


class TestSyncTurn:
    @pytest.mark.parametrize("notice", [
        "[ASYNC DELEGATION BATCH COMPLETE — batch-1]",
        "[ASYNC DELEGATION COMPLETE — child-1]",
        "[ASYNC DELEGATION TASK FAILED — batch-1, task 1/2]",
        "[NATIVE REVIEW COMPLETE — candidate-1]",
        "[SUBAGENT child-1] finished",
    ])
    def test_retain_filter_drops_injected_notice_but_keeps_real_user_message(self, notice):
        assert filter_retain_messages("Keep this decision", notice) == ("Keep this decision", None)

    def test_retain_filter_drops_recalled_context_and_status_only_assistant(self):
        user, assistant = filter_retain_messages(
            "<memory-context>old recalled fact</memory-context>Keep this request",
            "[SILENT]",
        )
        assert (user, assistant) == ("Keep this request", None)

    def test_retain_filter_keeps_substantive_one_line_status_report(self):
        assert filter_retain_messages("Question", "Status: deployment failed because the database is unavailable.") == (
            "Question",
            "Status: deployment failed because the database is unavailable.",
        )

    @pytest.mark.parametrize("event", [
        {"type": "async_delegation", "delegation_id": "child-1", "goal": "Review, check, fix and run changes",
         "status": "completed", "summary": "Review complete; run the check again."},
        {"type": "async_delegation", "delegation_id": "batch-1", "is_batch": True,
         "goals": ["Review and fix the changes"],
         "results": [{"task_index": 0, "status": "completed", "summary": "Run the check."}]},
        {"type": "async_delegation", "delegation_id": "batch-1", "task_failure_notice": True,
         "goals": ["Review and fix the changes"], "n_tasks": 1,
         "results": [{"task_index": 0, "status": "failed", "error": "Run the check."}]},
        {"type": "completion", "session_id": "proc-1", "command": "run review check",
         "exit_code": 0, "output": "Fix it and run the check."},
    ])
    def test_retain_filter_separates_formatter_notice_from_user_text(self, event):
        from tools.process_registry_notifications import format_process_notification

        notice = format_process_notification(event)
        assert filter_retain_messages(notice, "[SILENT]") == (None, None)
        request = "Remember the design decision about the database."
        assert filter_retain_messages(f"{notice}\n\n{request}", "[SILENT]") == (request, None)

    def test_retain_filter_preserves_plain_user_and_out_of_band_steer(self):
        from agent.prompt_builder import format_steer_marker
        from tools.process_registry_notifications import format_process_notification

        request = "What does a literal <memory-context> tag do?"
        assert filter_retain_messages(request, "answer") == (request, "answer")
        notice = format_process_notification({"type": "async_delegation", "delegation_id": "child-2",
                                              "goal": "Review the changes", "summary": "Check the result."})
        assert filter_retain_messages(notice + format_steer_marker(request), "[SILENT]") == (request, None)
        from gateway.run import build_resume_recovery_note
        assert filter_retain_messages(build_resume_recovery_note("shutdown_timeout"), "[SILENT]") == (None, None)
        assert filter_retain_messages(build_resume_recovery_note("shutdown_timeout", request), "[SILENT]") == (
            request, None)
        assert filter_retain_messages("<memory-context>recalled text without a closing tag", "[SILENT]") == (None, None)

    def test_cron_context_does_not_auto_retain(self, provider_with_config):
        p = provider_with_config()
        p.initialize(session_id="cron_job-1", agent_context="cron", platform="cron")
        p._client = _make_mock_client()

        p.sync_turn("scheduled input", "scheduled result")

        assert p._retain_queue.empty()
        p._client.aretain_batch.assert_not_called()

    def test_cron_context_keeps_recall_tools_but_hides_retain_tool(self, provider_with_config):
        p = provider_with_config()
        p.initialize(session_id="cron_job-1", agent_context="cron", platform="cron")
        p._client = _make_mock_client()

        tool_names = {schema["name"] for schema in p.get_tool_schemas()}
        assert tool_names == {"hindsight_recall", "hindsight_reflect"}

        result = p.handle_tool_call("hindsight_retain", {"content": "do not store this"})

        assert "disabled in cron context" in result
        p._client.aretain_batch.assert_not_called()

    def test_cron_context_does_not_flush_buffered_turns_on_session_switch(self, provider_with_config):
        p = provider_with_config()
        p.initialize(session_id="cron_job-1", agent_context="cron", platform="cron")
        p._client = _make_mock_client()
        p._session_turns = ["buffered cron turn"]

        p.on_session_switch("cron_job-2")

        assert p._retain_queue.empty()
        p._client.aretain_batch.assert_not_called()

    def test_sync_turn_retains_metadata_rich_turn(self, provider_with_config, monkeypatch):
        event_time = datetime(2026, 8, 10, 11, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
        monkeypatch.setattr("plugins.memory.hindsight._hermes_now", lambda: event_time)
        p = provider_with_config(
            retain_tags=["conv", "session1"],
            retain_source="hermes",
            retain_user_prefix="User (fakeusername)",
            retain_assistant_prefix="Assistant (fakeassistantname)",
        )
        p.initialize(
            session_id="session-1",
            platform="discord",
            user_id="fakeusername-123",
            user_name="fakeusername",
            chat_id="1485316232612941897",
            chat_name="fakeassistantname-forums",
            chat_type="thread",
            thread_id="1491249007475949698",
            agent_identity="fakeassistantname",
        )
        p._client = _make_mock_client()

        p.sync_turn("hello", "hi there")
        p._retain_queue.join()

        p._client.aretain_batch.assert_called_once()
        call_kwargs = p._client.aretain_batch.call_args.kwargs
        assert call_kwargs["bank_id"] == "test-bank"
        assert call_kwargs["document_id"].startswith("session-1-")
        assert call_kwargs["retain_async"] is True
        assert len(call_kwargs["items"]) == 1
        item = call_kwargs["items"][0]
        assert item["context"].startswith("conversation between Hermes Agent and the User\n\nChat-session extraction:")
        assert "confirmed decisions" in item["context"]
        assert "assistant proposal is not a user decision" in item["context"]
        assert item["tags"] == ["conv", "session1", "session:session-1"]
        content = json.loads(item["content"])
        assert len(content) == 1
        assert content[0][0]["role"] == "user"
        assert content[0][0]["content"] == "User (fakeusername): hello"
        assert content[0][1]["role"] == "assistant"
        assert content[0][1]["content"] == "Assistant (fakeassistantname): hi there"
        assert item["metadata"]["source"] == "hermes"
        assert item["metadata"]["session_id"] == "session-1"
        assert item["metadata"]["platform"] == "discord"
        assert item["metadata"]["user_id"] == "fakeusername-123"
        assert item["metadata"]["user_name"] == "fakeusername"
        assert item["metadata"]["chat_id"] == "1485316232612941897"
        assert item["metadata"]["chat_name"] == "fakeassistantname-forums"
        assert item["metadata"]["chat_type"] == "thread"
        assert item["metadata"]["thread_id"] == "1491249007475949698"
        assert item["metadata"]["agent_identity"] == "fakeassistantname"
        assert item["metadata"]["turn_index"] == "1"
        assert item["metadata"]["message_count"] == "2"
        assert content[0][0]["timestamp"] == event_time.isoformat(timespec="seconds")
        assert content[0][1]["timestamp"] == event_time.isoformat(timespec="seconds")
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", item["metadata"]["retained_at"])
        assert item["timestamp"] == event_time.isoformat(timespec="seconds")

    def test_retain_timestamp_normalizes_a_naive_clock(self, provider, monkeypatch):
        event_time = datetime(2026, 8, 10, 11, 9)
        monkeypatch.setattr("plugins.memory.hindsight._hermes_now", lambda: event_time)

        timestamp = provider._build_retain_kwargs("hello")["timestamp"]
        parsed = datetime.fromisoformat(timestamp)

        assert parsed.tzinfo is not None
        assert parsed.utcoffset() is not None

    @pytest.mark.asyncio
    async def test_retain_timestamp_is_serialized_by_pinned_client(self, provider):
        hindsight_client = pytest.importorskip(
            "hindsight_client", reason="pinned hindsight-client SDK not installed"
        )
        Hindsight = hindsight_client.Hindsight

        item = provider._build_retain_kwargs("hello")
        item.pop("bank_id", None)
        item.pop("retain_async", None)

        client = Hindsight(base_url="http://localhost:9999", api_key="test-key")
        client._memory_api.retain_memories = AsyncMock(return_value=SimpleNamespace(ok=True))
        try:
            await client.aretain_batch(bank_id="test-bank", items=[item])
            call = client._memory_api.retain_memories.await_args
            assert call is not None
            request = call.args[1]
            assert request.to_dict()["items"][0]["timestamp"] == item["timestamp"]
        finally:
            await client.aclose()


    def test_resume_creates_new_document(self, tmp_path, monkeypatch):
        """Resuming a session (re-initializing) gets a new document_id
        so previously stored content is not overwritten."""
        config = {"mode": "cloud", "apiKey": "k", "api_url": "http://x", "bank_id": "b"}
        config_path = tmp_path / "hindsight" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config))
        monkeypatch.setattr("plugins.memory.hindsight.get_hermes_home", lambda: tmp_path)

        p1 = HindsightMemoryProvider()
        p1.initialize(session_id="resumed-session", hermes_home=str(tmp_path), platform="cli")

        # Sleep just enough that the microsecond timestamp differs
        import time
        time.sleep(0.001)

        p2 = HindsightMemoryProvider()
        p2.initialize(session_id="resumed-session", hermes_home=str(tmp_path), platform="cli")

        # Same session, but each process gets its own document_id
        assert p1._document_id != p2._document_id
        assert p1._document_id.startswith("resumed-session-")
        assert p2._document_id.startswith("resumed-session-")


# ---------------------------------------------------------------------------
# retain indicator ("saving to memory") tests
# ---------------------------------------------------------------------------


class TestRetainIndicator:
    _SAVING = "👁️ Hindsight — saving to memory…"

    def test_emits_saving_on_dispatch(self, provider_with_config):
        calls = []
        p = provider_with_config(retain_async=False)
        p._status_callback = calls.append
        p.sync_turn("hello", "hi")
        p._retain_queue.join()
        assert self._SAVING in calls

    def test_suppressed_when_indicator_off(self, provider_with_config):
        calls = []
        p = provider_with_config(retain_indicator=False, retain_async=False)
        p._status_callback = calls.append
        p.sync_turn("hello", "hi")
        p._retain_queue.join()
        assert calls == []

    def test_no_emit_when_auto_retain_off(self, provider_with_config):
        calls = []
        p = provider_with_config(auto_retain=False)
        p._status_callback = calls.append
        p.sync_turn("hello", "hi")  # returns early — nothing dispatched
        assert calls == []

    def test_no_emit_on_buffered_turn(self, provider_with_config):
        # retain_every_n_turns=2: turn 1 buffers (no write, no line),
        # turn 2 flushes (one line) — "saving" only fires on a real write.
        calls = []
        p = provider_with_config(retain_every_n_turns=2, retain_async=False)
        p._status_callback = calls.append
        p.sync_turn("t1-u", "t1-a")
        assert calls == []
        p.sync_turn("t2-u", "t2-a")
        p._retain_queue.join()
        assert calls == [self._SAVING]

    def test_no_crash_without_callback(self, provider_with_config):
        p = provider_with_config(retain_async=False)
        assert p._status_callback is None
        p.sync_turn("hello", "hi")  # must not raise
        p._retain_queue.join()

    def test_status_callback_wired_from_initialize(self, tmp_path, monkeypatch):
        cb = lambda _m: None
        p = _provider_for_mode(tmp_path, monkeypatch, "cloud")
        p.initialize(session_id="s", hermes_home=str(tmp_path), status_callback=cb)
        assert p._status_callback is cb


# ---------------------------------------------------------------------------
# Shutdown / writer tests
# ---------------------------------------------------------------------------


class TestShutdownRace:
    def test_sync_turn_uses_single_writer_thread(self, provider):
        """All retains run through one long-lived writer thread."""
        provider.sync_turn("a", "b")
        provider._retain_queue.join()
        first_writer = provider._writer_thread
        assert first_writer is not None
        assert first_writer.is_alive()

        provider.sync_turn("c", "d")
        provider._retain_queue.join()
        # Same thread reused — no ad-hoc thread per call.
        assert provider._writer_thread is first_writer
        assert provider._client.aretain_batch.call_count == 2


    def test_shutdown_flushes_buffered_tail(self, provider_with_config):
        p = provider_with_config(retain_every_n_turns=3, retain_async=False)
        client = p._client
        old_doc = p._document_id
        p.sync_turn("last user turn", "last assistant turn")
        client.aretain_batch.assert_not_called()

        p.shutdown()

        client.aretain_batch.assert_called_once()
        kw = client.aretain_batch.call_args.kwargs
        assert kw["bank_id"] == "test-bank"
        assert kw["document_id"] == old_doc
        assert "last user turn" in kw["items"][0]["content"]
        assert "session:test-session" in kw["items"][0]["tags"]
        assert p._retain_queue.empty()

    def test_shutdown_drains_pending_retains(self, provider):
        """Shutdown must wait for queued retains to complete, not abandon them.

        Otherwise the LAST in-flight turn — typically the most important —
        is silently lost.
        """
        client = provider._client
        provider.sync_turn("a", "b")
        provider.sync_turn("c", "d")
        provider.shutdown()
        # Both retains drained before shutdown returned.
        assert client.aretain_batch.call_count == 2
        assert provider._retain_queue.empty()


# ---------------------------------------------------------------------------
# on_session_switch — flush + prefetch reset behavior
# ---------------------------------------------------------------------------


class TestSessionSwitchBufferFlush:
    def test_session_template_rotates_bank_without_redirecting_queued_writes(self, provider_with_config):
        p = provider_with_config(bank_id_template="hermes-{session}",
                                 retain_every_n_turns=2, retain_async=False)
        client = p._client
        entered, release = threading.Event(), threading.Event()

        async def retain(**kwargs):
            if kwargs["items"][0]["metadata"]["turn_index"] == "2":
                entered.set()
                assert release.wait(timeout=5.0)

        client.aretain_batch = AsyncMock(side_effect=retain)
        assert p._bank_id == "hermes-test-session"
        try:
            p.sync_turn("old first", "reply")
            p.sync_turn("old second", "reply")
            assert entered.wait(timeout=5.0)
            p.sync_turn("old tail", "reply")
            p.on_session_switch("new-sid")
            assert p._bank_id == "hermes-new-sid"
        finally:
            release.set()
        p._retain_queue.join()
        assert [call.kwargs["bank_id"] for call in client.aretain_batch.call_args_list] == [
            "hermes-test-session", "hermes-test-session",
        ]
        p.sync_turn("new first", "reply")
        p.sync_turn("new second", "reply")
        p._retain_queue.join()
        assert client.aretain_batch.call_args.kwargs["bank_id"] == "hermes-new-sid"
        p.handle_tool_call("hindsight_recall", {"query": "new query"})
        assert client.arecall.call_args.kwargs["bank_id"] == "hermes-new-sid"

    def test_slow_old_prefetch_cannot_repopulate_new_session(self, provider):
        entered, release = threading.Event(), threading.Event()

        def slow_recall(query):
            entered.set()
            assert release.wait(timeout=10.0)
            return "- old session context", 1

        provider._do_recall = slow_recall
        provider.queue_prefetch("old question")
        assert entered.wait(timeout=5.0)
        try:
            provider.on_session_switch("new-sid")
        finally:
            release.set()
        provider._prefetch_thread.join(timeout=5.0)
        assert not provider._prefetch_thread.is_alive()
        assert provider.prefetch("new question") == ""
        assert provider.recall_status() is None

    def test_switch_to_unrelated_session_clears_the_old_parent(self, provider_with_config):
        """An explicit empty parent on a switch to a different session must drop the previous
        branch's parent, or later retains tag the unrelated conversation with the old lineage."""
        p = provider_with_config()
        p.on_session_switch("branch-sid", parent_session_id="root-sid")
        assert p._parent_session_id == "root-sid"
        p.on_session_switch("unrelated-sid", parent_session_id="")
        assert p._session_id == "unrelated-sid"
        assert p._parent_session_id == ""

    def test_rewind_of_the_same_session_keeps_its_parent(self, provider_with_config):
        """/undo re-fires the hook for the SAME session with no parent; lineage must survive."""
        p = provider_with_config()
        p.on_session_switch("branch-sid", parent_session_id="root-sid")
        p.on_session_switch("branch-sid", parent_session_id="", reset=False, rewound=True)
        assert p._parent_session_id == "root-sid"

    def test_buffered_turns_flushed_before_clear(self, provider_with_config):
        """retain_every_n_turns > 1 must not silently drop partial buffers
        on session switch. Whatever's in _session_turns at switch time
        should land in the OLD document under the OLD session id."""
        p = provider_with_config(retain_every_n_turns=3, retain_async=False)
        old_doc = p._document_id

        # Two turns buffered, no retain yet (boundary is at turn 3). The
        # writer hasn't been started either — sync_turn's early return
        # skips _ensure_writer when no retain is due.
        p.sync_turn("turn1-user", "turn1-asst")
        p.sync_turn("turn2-user", "turn2-asst")
        assert p._sync_thread is None
        p._client.aretain_batch.assert_not_called()

        # Switch — flush should fire under OLD document_id via the writer queue.
        p.on_session_switch("new-sid", parent_session_id="test-session", reset=True)
        p._retain_queue.join()

        p._client.aretain_batch.assert_called_once()
        kw = p._client.aretain_batch.call_args.kwargs
        assert kw["document_id"] == old_doc
        item = kw["items"][0]
        # Both buffered turns must be present in the flushed payload.
        content = json.loads(item["content"])
        flat = json.dumps(content)
        assert "turn1-user" in flat
        assert "turn2-user" in flat
        # Old session id must appear in lineage tags / metadata.
        assert "session:test-session" in item["tags"]
        assert item["metadata"]["session_id"] == "test-session"

        # And the new session must start with a clean slate.
        assert p._session_id == "new-sid"
        assert p._session_turns == []
        assert p._turn_counter == 0
        assert p._document_id != old_doc
        assert p._document_id.startswith("new-sid-")


    def test_in_flight_prefetch_thread_drained_on_switch(self, provider, monkeypatch):
        """on_session_switch must wait for an in-flight prefetch from the
        old session to settle before clearing _prefetch_result, otherwise
        the thread can race and re-populate the field after the clear."""
        import threading

        gate = threading.Event()
        finished = threading.Event()

        def _slow_prefetch():
            gate.wait(timeout=5.0)
            with provider._prefetch_lock:
                provider._prefetch_result = "old-session recall"
            finished.set()

        provider._prefetch_thread = threading.Thread(target=_slow_prefetch, daemon=True)
        provider._prefetch_thread.start()

        # Release the prefetch worker so it writes _prefetch_result, then
        # call on_session_switch — it must join the thread before clearing.
        gate.set()
        provider.on_session_switch("new-sid")

        assert finished.is_set(), "switch returned before prefetch thread settled"
        assert provider._prefetch_result == ""

    def test_flush_serializes_behind_pending_retains_via_writer_queue(
        self, provider_with_config
    ):
        """The flush closure must ride the same _retain_queue sync_turn
        uses, so it lands FIFO behind any still-queued old-session
        retains rather than racing them on a separate thread.

        Regression guard: an earlier draft spawned a raw threading.Thread
        for flush, overwriting _sync_thread and racing the writer against
        the same document_id.
        """
        import threading as _threading

        p = provider_with_config(retain_every_n_turns=2, retain_async=False)

        # Block the first writer job until we've enqueued the flush
        # behind it. This proves ordering — the flush MUST wait.
        gate = _threading.Event()
        call_order: list[str] = []

        def _aretain_batch_tracking(**kw):
            idx = kw["items"][0]["metadata"].get("turn_index", "")
            call_order.append(str(idx))
            if idx == "2":
                # First retain blocks until we've enqueued the flush.
                gate.wait(timeout=5.0)

        p._client.aretain_batch = AsyncMock(side_effect=_aretain_batch_tracking)

        # Turn 1+2 → boundary hit → retain enqueued (will block).
        p.sync_turn("turn1-user", "turn1-asst")
        p.sync_turn("turn2-user", "turn2-asst")

        # One more buffered turn so flush has something to land.
        p.sync_turn("turn3-user", "turn3-asst")

        # Switch while the first retain is still blocked on `gate`.
        p.on_session_switch("new-sid", parent_session_id="test-session")

        # Release the first retain. Flush must have been enqueued
        # BEHIND it, and run second.
        gate.set()
        p._retain_queue.join()

        # The flush carries all buffered turns; sync_turn's retain #2
        # carried the batch at boundary time. Two distinct calls.
        assert p._client.aretain_batch.call_count == 2
        # First call landed while buffer was [t1, t2]; flush landed
        # after we added t3. So the second call must be strictly after.
        assert call_order[0] == "2"
        # Flush retain has turn_index matching the buffered count at
        # switch time (3 turns accumulated, _turn_index was set to 3
        # by the last sync_turn).
        assert call_order[1] == "3"


# ---------------------------------------------------------------------------
# update_mode='append' capability probe + retain dispatch
# ---------------------------------------------------------------------------


def test_capability_cache_does_not_leak_between_tests_first():
    """Pairs with the next test: a cached modern answer here must not reach it."""
    from plugins.memory import hindsight
    hindsight._append_capability_cache[("http://localhost:9999", None)] = True
    hindsight._append_capability_cache[("http://localhost:9999", "leak")] = True


def test_capability_cache_does_not_leak_between_tests_second():
    from plugins.memory import hindsight
    assert hindsight._append_capability_cache == {}


class TestUpdateModeAppendCapability:
    def _clear_capability_cache(self):
        from plugins.memory.hindsight import _append_capability_cache, _append_capability_lock
        with _append_capability_lock:
            _append_capability_cache.clear()

    def test_legacy_api_falls_back_to_per_process_doc_id(self, provider, monkeypatch):
        """API returns no /version (or pre-0.5.0) — sync_turn must use the
        per-process unique doc_id and NOT pass update_mode."""
        self._clear_capability_cache()
        monkeypatch.setattr(
            "plugins.memory.hindsight._fetch_hindsight_api_version",
            lambda *a, **kw: None,
        )
        old_doc = provider._document_id
        provider.sync_turn("hello", "hi")
        provider._retain_queue.join()

        kw = provider._client.aretain_batch.call_args.kwargs
        assert kw["document_id"] == old_doc
        assert kw["document_id"].startswith("test-session-")
        item = kw["items"][0]
        assert "update_mode" not in item

    def test_modern_api_uses_stable_doc_id_with_append(self, provider, monkeypatch):
        """API on >=0.5.0 — retain uses stable session_id and sets update_mode='append'."""
        self._clear_capability_cache()
        monkeypatch.setattr(
            "plugins.memory.hindsight._fetch_hindsight_api_version",
            lambda *a, **kw: "0.5.6",
        )
        provider.sync_turn("hello", "hi")
        provider._retain_queue.join()

        kw = provider._client.aretain_batch.call_args.kwargs
        # Stable: just the session id, no per-process timestamp suffix.
        assert kw["document_id"] == "test-session"
        item = kw["items"][0]
        assert item["update_mode"] == "append"


    def test_session_switch_flush_picks_capability_against_old_session(
        self, provider_with_config, monkeypatch
    ):
        """When the API supports append, the flush on /reset must land
        in the OLD session's stable document, not a per-process id."""
        self._clear_capability_cache()
        monkeypatch.setattr(
            "plugins.memory.hindsight._fetch_hindsight_api_version",
            lambda *a, **kw: "0.5.6",
        )
        p = provider_with_config(retain_every_n_turns=3, retain_async=False)
        p.sync_turn("turn1-user", "turn1-asst")
        p.sync_turn("turn2-user", "turn2-asst")
        p.on_session_switch("new-sid", parent_session_id="test-session", reset=True)
        p._retain_queue.join()

        kw = p._client.aretain_batch.call_args.kwargs
        # Flush goes to the OLD session's stable doc, not new-sid's.
        assert kw["document_id"] == "test-session"
        assert kw["items"][0]["update_mode"] == "append"


# ---------------------------------------------------------------------------
# System prompt tests
# ---------------------------------------------------------------------------


class TestSystemPrompt:
    def test_hybrid_mode_prompt(self, provider):
        block = provider.system_prompt_block()
        assert "Hindsight Memory" in block
        assert "hindsight_recall" in block
        assert "automatically injected" in block


# ---------------------------------------------------------------------------
# Config schema tests
# ---------------------------------------------------------------------------


class TestConfigSchema:
    def test_schema_has_all_new_fields(self, provider):
        schema = provider.get_config_schema()
        keys = {f["key"] for f in schema}
        expected_keys = {
            "mode", "api_url", "api_key", "llm_provider", "llm_api_key",
            "llm_model", "bank_id", "bank_id_template", "bank_mission", "bank_retain_mission",
            "recall_budget", "memory_mode", "recall_prefetch_method",
            "retain_tags", "retain_source",
            "retain_user_prefix", "retain_assistant_prefix",
            "recall_tags", "recall_tags_match",
            "auto_recall", "auto_retain",
            "retain_every_n_turns", "retain_async", "retain_context",
            "recall_max_tokens", "recall_max_input_chars",
            "recall_prompt_preamble",
        }
        assert expected_keys.issubset(keys), f"Missing: {expected_keys - keys}"


# ---------------------------------------------------------------------------
# bank_id_template tests
# ---------------------------------------------------------------------------


class TestBankIdTemplate:
    def test_sanitize_bank_segment_passthrough(self):
        assert _sanitize_bank_segment("hermes") == "hermes"
        assert _sanitize_bank_segment("my-agent_1") == "my-agent_1"


    def test_resolve_empty_template_uses_fallback(self):
        result = _resolve_bank_id_template(
            "", fallback="hermes", profile="coder"
        )
        assert result == "hermes"


    def test_resolve_sanitizes_placeholder_values(self):
        result = _resolve_bank_id_template(
            "user-{user}", fallback="hermes",
            profile="", workspace="", platform="",
            user="josh@example.com", session="",
        )
        assert result == "user-josh-example-com"


    def test_provider_uses_bank_id_template_from_config(self, tmp_path, monkeypatch):
        config = {
            "mode": "cloud",
            "apiKey": "k",
            "api_url": "http://x",
            "bank_id": "fallback-bank",
            "bank_id_template": "hermes-{profile}",
        }
        config_path = tmp_path / "hindsight" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config))
        monkeypatch.setattr("plugins.memory.hindsight.get_hermes_home", lambda: tmp_path)

        p = HindsightMemoryProvider()
        p.initialize(
            session_id="s1",
            hermes_home=str(tmp_path),
            platform="cli",
            agent_identity="coder",
            agent_workspace="hermes",
        )
        assert p._bank_id == "hermes-coder"
        assert p._bank_id_template == "hermes-{profile}"

    def test_disposable_profiles_derive_distinct_banks(self, tmp_path, monkeypatch):
        config = {
            "mode": "cloud",
            "apiKey": "fixture-key",
            "api_url": "http://fixture",
            "bank_id": "hermes",
            "bank_id_template": "hermes-{profile}",
        }
        home = tmp_path / "hindsight"
        home.mkdir()
        (home / "config.json").write_text(json.dumps(config))
        monkeypatch.setattr("plugins.memory.hindsight.get_hermes_home", lambda: tmp_path)

        banks = []
        for profile in ("personal-fixture", "lpg-fixture"):
            p = HindsightMemoryProvider()
            p.initialize(session_id="s1", hermes_home=str(tmp_path), platform="cli", agent_identity=profile)
            banks.append(p._bank_id)

        assert banks == ["hermes-personal-fixture", "hermes-lpg-fixture"]
        assert len(set(banks)) == 2


# ---------------------------------------------------------------------------
# Availability tests
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_available_with_api_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "plugins.memory.hindsight.get_hermes_home",
            lambda: tmp_path / "nonexistent",
        )
        monkeypatch.setenv("HINDSIGHT_API_KEY", "test-key")
        p = HindsightMemoryProvider()
        assert p.is_available()


    def test_local_mode_unavailable_when_runtime_import_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "plugins.memory.hindsight.get_hermes_home",
            lambda: tmp_path / "nonexistent",
        )
        monkeypatch.setenv("HINDSIGHT_MODE", "local")

        def _raise(_name):
            raise RuntimeError(
                "NumPy was built with baseline optimizations: (x86_64-v2)"
            )

        monkeypatch.setattr(
            "importlib.import_module",
            _raise,
        )
        p = HindsightMemoryProvider()
        assert not p.is_available()

    def test_initialize_disables_local_mode_when_runtime_import_fails(self, tmp_path, monkeypatch):
        config = {"mode": "local_embedded"}
        config_path = tmp_path / "hindsight" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(config))
        monkeypatch.setattr(
            "plugins.memory.hindsight.get_hermes_home", lambda: tmp_path
        )

        def _raise(_name):
            raise RuntimeError("x86_64-v2 unsupported")

        monkeypatch.setattr(
            "importlib.import_module",
            _raise,
        )

        p = HindsightMemoryProvider()
        p.initialize(session_id="test-session", hermes_home=str(tmp_path), platform="cli")
        assert p._mode == "disabled"


    def test_disabled_provider_makes_no_network_calls(self, tmp_path, monkeypatch):
        """A provider that disabled itself must not fall through to the cloud client: no recall,
        retain, tool call or advertised memory."""
        config_path = tmp_path / "hindsight" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({"mode": "local_embedded"}))
        monkeypatch.setattr("plugins.memory.hindsight.get_hermes_home", lambda: tmp_path)

        def _raise(_name):
            raise RuntimeError("x86_64-v2 unsupported")

        monkeypatch.setattr("importlib.import_module", _raise)
        p = HindsightMemoryProvider()
        p.initialize(session_id="test-session", hermes_home=str(tmp_path), platform="cli")
        assert p._mode == "disabled"
        cloud = MagicMock(side_effect=AssertionError("cloud client must not be built"))
        monkeypatch.setattr(p, "_new_cloud_client", cloud)

        assert p._recall_disabled() is True
        p.queue_prefetch("q")
        assert p.prefetch("q") == ""
        p.sync_turn("hello", "world")
        assert p._retain_queue.unfinished_tasks == 0
        assert "error" in json.loads(p.handle_tool_call("hindsight_recall", {"query": "q"}))
        assert p.get_tool_schemas() == []
        assert p.system_prompt_block() == ""
        with pytest.raises(RuntimeError, match="disabled"):
            p._get_client()
        cloud.assert_not_called()

    def test_shutdown_unregisters_the_atexit_callback(self, provider, monkeypatch):
        """The bound atexit callback strongly references the provider; shutdown must drop it so
        evicted gateway sessions can be collected. (The module's autouse fixture keeps its own
        list of providers, so assert the atexit registry directly rather than via GC.)"""
        registered: list = []
        monkeypatch.setattr("plugins.memory.hindsight.atexit.register", registered.append)

        def _unregister(fn):
            registered[:] = [f for f in registered if f != fn]

        monkeypatch.setattr("plugins.memory.hindsight.atexit.unregister", _unregister)
        provider._auto_retain = True
        provider._client.aretain_batch = AsyncMock(return_value=SimpleNamespace(operation_id=None, operation_ids=None))
        provider.sync_turn("hello", "world")
        assert registered == [provider._atexit_shutdown]
        provider.shutdown()
        assert registered == []
        assert provider._atexit_registered is False


class TestSharedEventLoopLifecycle:
    """Regression tests for #11923 — Hindsight leaking aiohttp ClientSession /
    TCPConnector objects in long-running gateway processes.

    Root cause: the module-global ``_loop`` / ``_loop_thread`` pair is shared
    across every HindsightMemoryProvider instance in the process (the plugin
    loader builds one provider per AIAgent, and the gateway builds one AIAgent
    per concurrent chat session). When a session ended, ``shutdown()`` stopped
    the shared loop, which orphaned every *other* live provider's aiohttp
    ClientSession on a dead loop. Those sessions were never closed and surfaced
    as ``Unclosed client session`` / ``Unclosed connector`` errors.
    """

    def test_shutdown_does_not_stop_shared_event_loop(self, provider_with_config):
        from plugins.memory import hindsight as hindsight_mod

        async def _noop():
            return 1

        # Prime the shared loop by scheduling a trivial coroutine — mirrors
        # the first time any real async call (arecall/aretain/areflect) runs.
        assert hindsight_mod._run_sync(_noop()) == 1

        loop_before = hindsight_mod._loop
        thread_before = hindsight_mod._loop_thread
        assert loop_before is not None and loop_before.is_running()
        assert thread_before is not None and thread_before.is_alive()

        # Build two independent providers (two concurrent chat sessions).
        provider_a = provider_with_config()
        provider_b = provider_with_config()

        # End session A.
        provider_a.shutdown()

        # Module-global loop/thread must still be the same live objects —
        # provider B (and any other sibling provider) is still relying on them.
        assert hindsight_mod._loop is loop_before, (
            "shutdown() swapped out the shared event loop — sibling providers "
            "would have their aiohttp ClientSession orphaned (#11923)"
        )
        assert hindsight_mod._loop.is_running(), (
            "shutdown() stopped the shared event loop — sibling providers' "
            "aiohttp sessions would leak (#11923)"
        )
        assert hindsight_mod._loop_thread is thread_before
        assert hindsight_mod._loop_thread.is_alive()

        # Provider B can still dispatch async work on the shared loop.
        async def _still_working():
            return 42

        assert hindsight_mod._run_sync(_still_working()) == 42

        provider_b.shutdown()

    def test_client_aclose_called_on_cloud_mode_shutdown(self, provider):
        """Per-provider session cleanup still runs even though the shared
        loop is preserved. Each provider's own aiohttp session is closed
        via ``self._client.aclose()``; only the (empty) shared loop survives.
        """
        assert provider._client is not None
        mock_client = provider._client

        provider.shutdown()

        mock_client.aclose.assert_called_once()
        assert provider._client is None


class TestShutdown:
    def test_local_embedded_shutdown_closes_inner_async_client_on_shared_loop(self, provider):
        inner_client = _make_mock_client()
        embedded = MagicMock()
        embedded._client = inner_client
        embedded.close = MagicMock()

        provider._mode = "local_embedded"
        provider._client = embedded

        provider.shutdown()

        inner_client.aclose.assert_awaited_once()
        embedded.close.assert_called_once()
        assert embedded._client is None
        assert provider._client is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits not enforced on Windows")
def test_save_config_sets_owner_only_permissions(tmp_path):
    """hindsight/config.json must be written with 0o600 so API key is not world-readable."""
    provider = HindsightMemoryProvider()
    provider.save_config({"api_key": "hd-test-key"}, str(tmp_path))
    config_file = tmp_path / "hindsight" / "config.json"
    assert config_file.exists()
    mode = stat.S_IMODE(config_file.stat().st_mode)
    assert mode == 0o600, f"Expected 0o600 (owner-only), got {oct(mode)}"


def test_load_config_corrupt_profile_file_falls_through_to_env(tmp_path, monkeypatch):
    """A corrupt $HERMES_HOME/hindsight/config.json is not the config: the loader falls through
    (legacy file, then env) instead of returning an empty, silently-unconfigured mapping."""
    home = tmp_path / "home"
    (home / "hindsight").mkdir(parents=True)
    (home / "hindsight" / "config.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
    monkeypatch.setenv("HINDSIGHT_MODE", "local")
    monkeypatch.setenv("HINDSIGHT_BANK_ID", "from-env")

    cfg = _load_config()

    assert cfg["mode"] == "local"
    assert cfg["banks"]["hermes"]["bankId"] == "from-env"


class TestLoadSimpleEnv:
    def test_bom_first_key_is_recognized(self, tmp_path):
        """A Notepad-edited .env carries a BOM; the first key must still parse
        instead of becoming '\ufeffHINDSIGHT_LLM_API_KEY'."""
        env_path = tmp_path / ".env"
        env_path.write_bytes("﻿HINDSIGHT_LLM_API_KEY=sk-test\n".encode("utf-8"))
        values = _load_simple_env(env_path)
        assert values.get("HINDSIGHT_LLM_API_KEY") == "sk-test"


class TestPostSetupEnvEncoding:
    def _run_cloud_post_setup(self, tmp_path, monkeypatch):
        """Drive post_setup through the cloud path with piped stdin."""
        import io

        monkeypatch.setattr("hermes_cli.memory_setup._curses_select",
                            lambda *a, **kw: 0)  # cloud mode
        monkeypatch.setattr("hermes_cli.config.save_config", lambda c: None)
        # Skip the dependency install (now routed through lazy_deps, NS-605).
        import tools.lazy_deps as lazy_deps_mod
        monkeypatch.setattr(
            lazy_deps_mod, "install_specs",
            lambda *a, **kw: lazy_deps_mod.InstallSpecsResult(ok=True),
        )
        # First line: API key prompt (readline). Second line: API URL (input).
        monkeypatch.setattr(sys, "stdin", io.StringIO("sk-new\n\n"))

        provider = HindsightMemoryProvider()
        provider.post_setup(str(tmp_path), {"memory": {}})

    def test_bom_first_key_updated_in_place(self, tmp_path, monkeypatch):
        """The setup writer reads the existing .env BOM-tolerantly, so a
        BOM'd first key is matched and rewritten, not duplicated."""
        env_path = tmp_path / ".env"
        env_path.write_bytes("﻿HINDSIGHT_API_KEY=old\n".encode("utf-8"))

        self._run_cloud_post_setup(tmp_path, monkeypatch)

        content = env_path.read_text(encoding="utf-8")
        assert content.count("HINDSIGHT_API_KEY=") == 1
        assert "HINDSIGHT_API_KEY=sk-new" in content
        assert "old" not in content
        assert "﻿" not in content


class TestClientAutoUpgradeRoutesThroughLazyDeps:
    """The initialize()-time hindsight-client auto-upgrade must go through
    lazy_deps.install_specs() (environment-aware, durable-target on sealed
    hosted venvs) — never a direct `uv pip install --python sys.executable`
    subprocess, which fails with EROFS/EACCES on immutable images (NS-605)."""

    def _init_with_outdated_client(self, tmp_path, monkeypatch, outcome):
        import importlib.metadata as md
        import subprocess as subprocess_mod
        import tools.lazy_deps as lazy_deps_mod

        config_path = tmp_path / "hindsight" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({"mode": "cloud"}))
        monkeypatch.setattr(
            "plugins.memory.hindsight.get_hermes_home", lambda: tmp_path
        )

        # Simulate an installed-but-outdated client.
        monkeypatch.setattr(md, "version", lambda name: "0.0.1")

        calls = []
        monkeypatch.setattr(
            lazy_deps_mod, "install_specs",
            lambda specs, **kw: calls.append(tuple(specs)) or outcome,
        )

        # Regression guard: no direct pip subprocess may run.
        def _no_subprocess(*a, **kw):  # pragma: no cover - fails loudly
            raise AssertionError(f"unexpected subprocess.run during auto-upgrade: {a}")
        monkeypatch.setattr(subprocess_mod, "run", _no_subprocess)

        provider = HindsightMemoryProvider()
        provider.initialize(session_id="s", hermes_home=str(tmp_path), platform="cli")
        return calls

    def test_default_fixture_never_installs_for_an_outdated_sdk(self, tmp_path, monkeypatch):
        """Mocked tests must not reach a real install even when the installed SDK looks outdated:
        the autouse fixture replaces install_specs, so initialize()'s auto-upgrade is absorbed."""
        import importlib.metadata as md
        import tools.lazy_deps as lazy_deps_mod

        assert lazy_deps_mod.install_specs is not _REAL_INSTALL_SPECS
        config_path = tmp_path / "hindsight" / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({"mode": "cloud"}))
        monkeypatch.setattr("plugins.memory.hindsight.get_hermes_home", lambda: tmp_path)
        monkeypatch.setattr(md, "version", lambda name: "0.0.1")
        HindsightMemoryProvider().initialize(session_id="s", hermes_home=str(tmp_path), platform="cli")

    def test_upgrade_uses_install_specs_not_subprocess(self, tmp_path, monkeypatch):
        from plugins.memory.hindsight import _MIN_CLIENT_VERSION
        from tools.lazy_deps import InstallSpecsResult

        calls = self._init_with_outdated_client(
            tmp_path, monkeypatch, InstallSpecsResult(ok=True)
        )
        assert calls == [(f"hindsight-client>={_MIN_CLIENT_VERSION}",)]

    def test_blocked_upgrade_is_nonfatal_and_surfaces_reason(
        self, tmp_path, monkeypatch, caplog
    ):
        import logging
        from tools.lazy_deps import InstallSpecsResult

        with caplog.at_level(logging.WARNING):
            calls = self._init_with_outdated_client(
                tmp_path, monkeypatch,
                InstallSpecsResult(ok=False, blocked=True,
                                   reason="runtime installs are disabled on this deployment"),
            )
        assert len(calls) == 1  # attempted exactly once, init still completed
        assert any("runtime installs are disabled" in r.getMessage()
                   for r in caplog.records)



class TestMultiplexBackgroundScope:
    """Under multiplex_profiles get_secret fails closed on an unscoped thread;
    the writer / daemon-start threads are spawned from a scoped context and
    must carry it along (#92608, #94933)."""

    @pytest.fixture()
    def scoped_embedded(self, tmp_path, monkeypatch):
        from agent.secret_scope import (
            build_profile_secret_scope, reset_secret_scope, set_multiplex_active, set_secret_scope,
        )
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        created = []

        class FakeHindsightEmbedded:
            def __init__(self, **kwargs):
                created.append(kwargs["llm_api_key"])
                self._manager = SimpleNamespace(is_running=lambda profile: False, stop=lambda profile: None)
                self._ensure_started = lambda: None

        dem = SimpleNamespace(console=None)
        monkeypatch.setitem(sys.modules, "hindsight", SimpleNamespace(HindsightEmbedded=FakeHindsightEmbedded))
        monkeypatch.setitem(sys.modules, "hindsight_embed", SimpleNamespace(daemon_embed_manager=dem))
        monkeypatch.setitem(sys.modules, "hindsight_embed.daemon_embed_manager", dem)
        monkeypatch.setattr("plugins.memory.hindsight._check_local_runtime", lambda: (True, ""))

        home = tmp_path / "profiles" / "p1"
        (home / "hindsight").mkdir(parents=True)
        (home / ".env").write_text("HINDSIGHT_LLM_API_KEY=p1-secret\n")
        (home / "hindsight" / "config.json").write_text(json.dumps(
            {"mode": "local_embedded", "llm_provider": "openai", "llm_model": "m", "memory_mode": "hybrid"}
        ))
        # Enter the profile scope the way gateway _profile_runtime_scope does.
        set_multiplex_active(True)
        monkeypatch.setattr("plugins.memory.hindsight.get_hermes_home", lambda: home)
        home_tok = set_hermes_home_override(str(home))
        scope_tok = set_secret_scope(build_profile_secret_scope(home))
        yield created, home
        set_multiplex_active(False)
        reset_secret_scope(scope_tok)
        reset_hermes_home_override(home_tok)

    def test_writer_thread_resolves_profile_secret(self, scoped_embedded):
        created, home = scoped_embedded
        p = HindsightMemoryProvider()
        p._mode = "local_embedded"
        p._config = {"profile": "hermes", "llm_provider": "openai", "llm_model": "m"}
        p._ensure_writer()
        p._retain_queue.put(p._get_client)   # real body: get_secret(HINDSIGHT_LLM_API_KEY)
        p._retain_queue.put(_WRITER_SENTINEL)
        p._writer_thread.join(timeout=5)
        assert created == ["p1-secret"]

    def test_daemon_start_thread_resolves_profile_secret(self, scoped_embedded):
        created, home = scoped_embedded
        p = HindsightMemoryProvider()
        p.initialize(session_id="s1", hermes_home=str(home), platform="cli")
        for t in threading.enumerate():
            if t.name == "hindsight-daemon-start":
                t.join(timeout=5)
        assert created == ["p1-secret"]
        assert "Daemon started successfully" in (home / "logs" / "hindsight-embed.log").read_text()


def test_append_mode_trims_retained_turns_without_dropping_any(provider, monkeypatch):
    """Append retains ship only the delta, so retained turns leave `_session_turns` (a never-ending
    session no longer pins every turn) while every turn is still shipped exactly once."""
    provider._auto_retain = True
    provider._retain_every_n_turns = 3
    monkeypatch.setattr(provider, "_ensure_writer", lambda: None)
    monkeypatch.setattr(provider, "_register_atexit", lambda: None)
    monkeypatch.setattr(provider, "_resolve_retain_target", lambda doc: ("doc", "append"))
    shipped: list[str] = []
    monkeypatch.setattr(provider, "_make_turn_retain_job",
                        lambda turns, **kw: (lambda: shipped.extend(turns)))
    provider._retain_queue = MagicMock(put=lambda job: job())

    for i in range(7):
        provider.sync_turn(f"user {i}", f"assistant {i}")

    assert len(provider._session_turns) == 1  # only the un-retained tail (turn 7)
    assert provider._last_retained_turn_count == 0
    assert len(shipped) == 6 and len(set(shipped)) == 6


# ---------------------------------------------------------------------------
# Config normalization and retain durability (v0.21.5 sync review)
# ---------------------------------------------------------------------------


def test_malformed_bank_id_template_falls_back():
    """Unmatched braces raise ValueError from str.format; the documented fallback applies."""
    assert _resolve_bank_id_template("hermes-{profile", "hermes", profile="default") == "hermes"
    assert _resolve_bank_id_template("hermes-{profile:!}", "hermes", profile="default") == "hermes"


def test_csv_recall_tags_reach_the_sdk_as_a_list(provider_with_config):
    """The schema documents comma-separated recall_tags; RecallRequest rejects a string."""
    p = provider_with_config(recall_tags="project-a, project-b,project-a")
    assert p._recall_tags == ["project-a", "project-b"]
    p._recall("what changed?")
    kwargs = p._client.arecall.call_args.kwargs
    assert kwargs["tags"] == ["project-a", "project-b"]
    recall_request = pytest.importorskip("hindsight_client_api.models").RecallRequest
    recall_request(query="what changed?", tags=kwargs["tags"])  # real SDK validation boundary


def test_list_recall_tags_are_preserved(provider_with_config):
    assert provider_with_config(recall_tags=["a", "b"])._recall_tags == ["a", "b"]
    assert provider_with_config(recall_tags="")._recall_tags is None


class TestRetainRetry:
    """A queued append delta is the only copy of those turns: a transient failure must not lose it."""

    def _append_provider(self, provider, monkeypatch):
        monkeypatch.setattr("plugins.memory.hindsight._fetch_hindsight_api_version", lambda *a, **kw: "0.5.6")
        monkeypatch.setattr("plugins.memory.hindsight._RETAIN_RETRY_BASE_S", 0.0)
        return provider

    def _wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def test_failed_append_turn_is_retried_in_order(self, provider, monkeypatch):
        p = self._append_provider(provider, monkeypatch)
        shipped = []

        async def _flaky(**kwargs):
            if not shipped and not getattr(_flaky, "failed", False):
                _flaky.failed = True
                raise ConnectionError("hindsight unreachable")
            shipped.append(kwargs["items"][0]["content"])
            return SimpleNamespace(ok=True)

        p._client.aretain_batch = AsyncMock(side_effect=_flaky)
        p.sync_turn("first question", "first answer")
        p._retain_queue.join()
        assert shipped == []  # the first attempt failed and was kept, not discarded
        p.sync_turn("second question", "second answer")
        assert self._wait_for(lambda: len(shipped) == 2)
        assert "first question" in shipped[0] and "second question" in shipped[1]
        assert not p._retain_backlog

    def test_backlog_retries_on_idle_without_new_turns(self, provider, monkeypatch):
        p = self._append_provider(provider, monkeypatch)
        calls = []

        async def _fail_once(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise ConnectionError("hindsight unreachable")
            return SimpleNamespace(ok=True)

        p._client.aretain_batch = AsyncMock(side_effect=_fail_once)
        p.sync_turn("only question", "only answer")
        assert self._wait_for(lambda: len(calls) == 2)
        assert self._wait_for(lambda: not p._retain_backlog)

    def test_timed_out_write_that_lands_is_not_sent_again(self, provider, monkeypatch):
        """A timeout stops the wait, not the write: a retry must not append the same turns twice."""
        p = self._append_provider(provider, monkeypatch)
        p._timeout = 0.2
        writes = []

        async def _slow_success(**kwargs):
            await asyncio.sleep(0.5)
            writes.append(kwargs["items"][0]["content"])
            return SimpleNamespace(ok=True)

        p._client.aretain_batch = AsyncMock(side_effect=_slow_success)
        p.sync_turn("slow question", "slow answer")
        assert self._wait_for(lambda: p._client.aretain_batch.await_count >= 1 and not p._retain_backlog, timeout=10.0)
        time.sleep(0.8)
        assert len(writes) == 1
        assert p._client.aretain_batch.await_count == 1

    def test_wait_timeout_racing_completion_still_hands_over_the_future(self, monkeypatch):
        """A send that completes right as the wait times out must still be kept, not resent blind."""
        import concurrent.futures
        import plugins.memory.hindsight as hindsight_mod

        class _CompletesAsTheWaitTimesOut(concurrent.futures.Future):
            def result(self, timeout=None):
                if not self.done():
                    self.set_result("landed")
                    raise TimeoutError()
                return super().result(timeout)

        racy = _CompletesAsTheWaitTimesOut()

        def _schedule(coro, loop, **kwargs):
            coro.close()
            return racy

        monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", _schedule)
        kept = []

        async def _noop():
            return None

        with pytest.raises(TimeoutError):
            hindsight_mod._run_sync(_noop(), timeout=0.01, keep_pending=kept.append)
        assert kept == [racy]

    def test_retry_judges_the_earlier_send_by_its_outcome_not_the_wait(self, provider, monkeypatch):
        """The earlier send finishing as the retry's wait times out is a success: no second send."""
        import concurrent.futures

        p = self._append_provider(provider, monkeypatch)
        p._timeout = 0.01
        earlier = concurrent.futures.Future()
        sends = []

        def _retain_batch(item, *, keep_pending=None, **kwargs):
            sends.append(item)
            keep_pending(earlier)
            raise TimeoutError()

        monkeypatch.setattr(p, "_retain_batch", _retain_batch)
        job = p._make_turn_retain_job(["turn"], document_id="doc", update_mode="append", label="retain")
        with pytest.raises(TimeoutError):
            job()

        real_wait = concurrent.futures.wait

        def _wait_that_races(fs, timeout=None, **kwargs):
            earlier.set_result(SimpleNamespace(ok=True))  # lands exactly as the wait gives up
            return real_wait(fs, timeout=0)

        monkeypatch.setattr(concurrent.futures, "wait", _wait_that_races)
        job()
        assert len(sends) == 1

    def test_timed_out_write_that_failed_is_sent_again(self, provider, monkeypatch):
        p = self._append_provider(provider, monkeypatch)
        p._timeout = 0.2
        writes = []

        async def _slow_then_fast(**kwargs):
            if not getattr(_slow_then_fast, "slow_done", False):
                _slow_then_fast.slow_done = True
                await asyncio.sleep(0.5)
                raise ConnectionError("hindsight dropped the request")
            writes.append(kwargs["items"][0]["content"])
            return SimpleNamespace(ok=True)

        p._client.aretain_batch = AsyncMock(side_effect=_slow_then_fast)
        p.sync_turn("retry question", "retry answer")
        assert self._wait_for(lambda: len(writes) == 1 and not p._retain_backlog, timeout=10.0)
        assert "retry question" in writes[0]

    def test_persistent_failure_is_bounded(self, provider, monkeypatch):
        p = self._append_provider(provider, monkeypatch)
        monkeypatch.setattr("plugins.memory.hindsight._RETAIN_MAX_ATTEMPTS", 3)
        p._client.aretain_batch = AsyncMock(side_effect=ConnectionError("down"))
        p.sync_turn("q", "a")
        assert self._wait_for(lambda: p._client.aretain_batch.await_count >= 3 and not p._retain_backlog)
        time.sleep(0.3)
        assert p._client.aretain_batch.await_count == 3
        started = time.monotonic()
        p.shutdown()
        assert time.monotonic() - started < 12.0

    def test_queued_jobs_do_not_bypass_the_retry_delay(self, provider, monkeypatch):
        """Newer jobs queue behind a failed head inside its backoff instead of retrying it."""
        p = self._append_provider(provider, monkeypatch)
        monkeypatch.setattr("plugins.memory.hindsight._RETAIN_RETRY_BASE_S", 30.0)
        p._client.aretain_batch = AsyncMock(side_effect=ConnectionError("down"))
        p.sync_turn("q1", "a1")
        p._retain_queue.join()
        for i in range(2, 6):
            p.sync_turn(f"q{i}", f"a{i}")
        p._retain_queue.join()
        assert p._client.aretain_batch.await_count == 1  # no retry burst inside the 30s backoff
        assert len(p._retain_backlog) == 5
        assert p._retain_backlog[0][1] == 1

    def test_prefetch_barrier_waits_for_a_pending_retry(self, provider, monkeypatch):
        """A failed retain awaiting retry is not recall-visible, so the drain barrier holds."""
        p = self._append_provider(provider, monkeypatch)
        monkeypatch.setattr("plugins.memory.hindsight._RETAIN_RETRY_BASE_S", 30.0)
        p._client.aretain_batch = AsyncMock(side_effect=ConnectionError("down"))
        p.sync_turn("q", "a")
        p._retain_queue.join()
        assert p._retain_queue.unfinished_tasks == 0 and len(p._retain_backlog) == 1
        assert p._wait_for_retains_drained(0.2) is False
        # Once the retry succeeds the barrier releases.
        p._client.aretain_batch = AsyncMock(return_value=SimpleNamespace(ok=True))
        p._retain_backlog_next_at = 0.0
        assert self._wait_for(lambda: not p._retain_backlog)
        assert p._wait_for_retains_drained(2.0) is True


def test_shared_loop_is_not_replaced_during_startup(monkeypatch):
    """A second caller arriving while the first loop thread is still starting must get the same
    loop, not a replacement: one cached async client cannot span two event loops."""
    import asyncio
    import threading

    import plugins.memory.hindsight as hs

    monkeypatch.setattr(hs, "_loop", None)
    monkeypatch.setattr(hs, "_loop_thread", None)
    real_set = asyncio.set_event_loop
    gate = threading.Event()

    def slow_set_event_loop(loop):
        gate.wait(timeout=0.5)  # widen the startup window before run_forever()
        real_set(loop)

    monkeypatch.setattr(hs.asyncio, "set_event_loop", slow_set_event_loop)
    results: list = []
    callers = [threading.Thread(target=lambda: results.append(hs._get_loop())) for _ in range(2)]
    for c in callers:
        c.start()
    for c in callers:
        c.join(timeout=10.0)
    loops = {id(loop) for loop in results}
    try:
        assert len(results) == 2
        assert len(loops) == 1, "startup window let a second loop replace the first"
    finally:
        for loop in {id(l): l for l in results}.values():
            loop.call_soon_threadsafe(loop.stop)
