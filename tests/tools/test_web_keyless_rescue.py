"""One-shot keyless rescue: keyed/configured backend fails → THIS call rides
the keyless ring; the NEXT call attempts the chosen backend again.

Covers:
- eligibility: keyed ring vendors and non-ring backends are eligible;
  keyless-mode ring calls are not (they already walked the ring); config
  gates (keyless_rescue / keyless_fallback) turn it off
- search dispatcher: failure-result and raised-exception paths both rescue,
  result annotated with rescued_from + backend_error
- statelessness: the very next dispatch calls the chosen backend again
- extract dispatcher: whole-batch failure rescues; partial failure passes
  through untouched
- rescue failure: original backend error survives, rescue note appended
"""

import json
from unittest.mock import patch

import pytest

from tools import web_tools
from tools import web_tools_rescue
from plugins.web import keyless_mcp
from plugins.web.keenable.provider import KeenableWebSearchProvider


class _KeyedBoomProvider:
    """Minimal keyed provider double that always fails."""

    name = "keenable"
    display_name = "Keenable"

    def supports_search(self):
        return True

    def supports_extract(self):
        return True

    def is_available(self):
        return True

    def search(self, query, limit=5):
        return {"success": False, "error": "HTTP 500 upstream exploded"}

    def extract(self, urls, **kwargs):
        return [
            {"url": u, "title": "", "content": "", "error": "HTTP 500 upstream exploded"}
            for u in urls
        ]


class _RaisingProvider(_KeyedBoomProvider):
    def search(self, query, limit=5):
        raise RuntimeError("connection reset by peer")


class _GatewayFirecrawlBoomProvider(_KeyedBoomProvider):
    """Managed-gateway Firecrawl double with no direct provider key."""

    name = "firecrawl"
    display_name = "Firecrawl"


class _GoodProvider(_KeyedBoomProvider):
    name = "exa"
    display_name = "Exa"

    def search(self, query, limit=5):
        return _ring_ok("exa")


@pytest.fixture(autouse=True)
def _keyed_keenable_env(monkeypatch):
    """Simulate a keyed Keenable setup with rescue enabled."""
    monkeypatch.setattr(
        "agent.web_search_provider.get_provider_env",
        lambda name: "kn-real" if name == "KEENABLE_API_KEY" else "",
    )
    monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "keenable"})
    monkeypatch.setattr(
        "agent.web_search_registry._keyless_tier_enabled", lambda: True
    )
    yield


def _ring_ok(vendor="exa"):
    return {"success": True, "data": {"web": [{"url": f"https://{vendor}.example"}]}}


class TestEligibility:
    def test_keyed_ring_vendor_is_eligible(self):
        assert web_tools_rescue._rescue_eligible(_KeyedBoomProvider()) is True

    def test_keyless_mode_ring_vendor_not_eligible(self, monkeypatch):
        # No key: the keenable call already rode the ring; no double-walk.
        monkeypatch.setattr(
            "agent.web_search_provider.get_provider_env", lambda name: ""
        )
        assert web_tools_rescue._rescue_eligible(KeenableWebSearchProvider()) is False

    def test_gateway_selected_ring_vendor_is_eligible_without_direct_key(self, monkeypatch):
        # The persisted Nous route uses its subscriber token, not the keyless ring — eligible.
        # The same keyless Firecrawl selected directly DID walk the ring — not eligible.
        monkeypatch.setattr(
            "agent.web_search_provider.get_provider_env", lambda name: ""
        )
        monkeypatch.setattr("plugins.web.firecrawl.provider._env", lambda name: "")
        monkeypatch.setattr("plugins.web.firecrawl.provider._is_tool_gateway_ready", lambda: True)
        monkeypatch.setattr(
            "tools.tool_backend_helpers.read_selection", lambda kind: "nous"
        )
        assert web_tools_rescue._rescue_eligible(_GatewayFirecrawlBoomProvider()) is True
        monkeypatch.setattr(
            "tools.tool_backend_helpers.read_selection", lambda kind: "firecrawl"
        )
        monkeypatch.setattr("plugins.web.keyless_mcp._web_config_selects", lambda name: name == "firecrawl")
        assert web_tools_rescue._rescue_eligible(_GatewayFirecrawlBoomProvider()) is False

    def test_non_ring_backend_is_eligible(self):
        class _SearxProvider(_KeyedBoomProvider):
            name = "searxng"

        assert web_tools_rescue._rescue_eligible(_SearxProvider()) is True

    def test_config_gate_disables(self, monkeypatch):
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"backend": "keenable", "keyless_rescue": False},
        )
        assert web_tools_rescue._rescue_eligible(_KeyedBoomProvider()) is False

    def test_keyless_fallback_off_disables(self, monkeypatch):
        monkeypatch.setattr(
            "agent.web_search_registry._keyless_tier_enabled", lambda: False
        )
        assert web_tools_rescue._rescue_eligible(_KeyedBoomProvider()) is False


class TestSearchRescue:
    def _dispatch(self, monkeypatch, provider):
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(
            "agent.web_search_registry.get_provider", lambda name: provider
        )
        return json.loads(web_tools.web_search_tool("q", limit=2))

    def test_failure_result_rescued_and_annotated(self, monkeypatch):
        with patch.object(
            keyless_mcp, "search_with_failover", return_value=_ring_ok()
        ) as ring:
            out = self._dispatch(monkeypatch, _KeyedBoomProvider())
        assert out["success"] is True
        assert out["data"]["rescued_from"] == "keenable"
        assert "HTTP 500" in out["data"]["backend_error"]
        assert "next call" in out["data"]["backend_error"].lower()
        ring.assert_called_once()

    def test_session_budget_warning_is_visible_and_warn_only(self, monkeypatch):
        from tools.web_result_cache import search_budget

        search_budget._counts["session-1"] = 49
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(
            "agent.web_search_registry.get_provider", lambda name: _GoodProvider()
        )
        out = json.loads(web_tools.web_search_tool("budget query", limit=2, session_id="session-1"))
        assert out["success"] is True
        assert any("warn-only" in warning for warning in out["warnings"])

    def test_successful_search_reuses_disk_cache_after_process_memory_reset(self, monkeypatch):
        from tools.web_result_cache import search_memo
        calls = {"backend": 0}

        class _CountingGood(_GoodProvider):
            def search(self, query, limit=5):
                calls["backend"] += 1
                return _ring_ok("exa")

        provider = _CountingGood()
        first = self._dispatch(monkeypatch, provider)
        assert first["success"] is True
        with search_memo._store_lock:
            search_memo._store.clear()
        second = self._dispatch(monkeypatch, provider)
        assert second["success"] is True
        assert calls["backend"] == 1

    def test_cache_disabled_does_not_persist_paid_success(self, monkeypatch):
        from tools import web_result_cache

        calls = {"backend": 0}

        class _CountingGood(_GoodProvider):
            def search(self, query, limit=5):
                calls["backend"] += 1
                return _ring_ok("exa")

        monkeypatch.setattr(web_result_cache, "_web_config", lambda: {"cache_enabled": False})
        provider = _CountingGood()
        first = self._dispatch(monkeypatch, provider)
        second = self._dispatch(monkeypatch, provider)
        assert first["success"] is True
        assert second["success"] is True
        assert calls["backend"] == 2

    def test_raised_exception_rescued(self, monkeypatch):
        with patch.object(
            keyless_mcp, "search_with_failover", return_value=_ring_ok()
        ):
            out = self._dispatch(monkeypatch, _RaisingProvider())
        assert out["success"] is True
        assert "connection reset" in out["data"]["backend_error"]

    def test_unrescued_exception_keeps_budget_warning_visible(self, monkeypatch):
        from tools.web_result_cache import search_budget

        class _UnrescuedRaising(_RaisingProvider):
            name = "searxng"

        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "searxng"})
        monkeypatch.setattr(web_tools, "_rescue_eligible", lambda provider: False)
        search_budget._counts["session-1"] = 49
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(
            "agent.web_search_registry.get_provider", lambda name: _UnrescuedRaising()
        )
        out = json.loads(web_tools.web_search_tool("unrescued", limit=2, session_id="session-1"))
        assert out["success"] is False
        assert "connection reset" in out["error"]
        assert any("warn-only" in warning for warning in out["warnings"])

    def test_managed_firecrawl_fallback_counts_toward_session_budget(self, monkeypatch):
        from tools.web_result_cache import search_budget

        class _PerplexityBoom:
            name = "perplexity"

            def search(self, query, limit=5):
                raise RuntimeError("managed primary unavailable")

        class _ManagedFirecrawl:
            name = "firecrawl"

            def search(self, query, limit=5):
                return _ring_ok("firecrawl")

        search_budget._counts["session-2"] = 98
        search_budget._warned["session-2"] = {50}
        monkeypatch.setattr(web_tools, "_managed_web_search", lambda: True)
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: True)
        monkeypatch.setattr(
            "agent.web_search_registry.get_provider", lambda name: _ManagedFirecrawl()
        )
        response, was_rescued, warning = web_tools._paid_search(
            _PerplexityBoom(), "budgeted fallback", 2, "session-2"
        )
        assert response["success"] is True
        assert was_rescued is True
        assert warning is not None
        assert "100 remote firecrawl" in warning
        assert search_budget._counts["session-2"] == 100

    def test_failed_managed_fallback_keeps_budget_warning_visible(self, monkeypatch):
        from tools.web_result_cache import search_budget

        class _PerplexityFail:
            name = "perplexity"

            def search(self, query, limit=5):
                return {"success": False, "error": "managed primary unavailable"}

        class _ManagedFirecrawlFail:
            name = "firecrawl"

            def search(self, query, limit=5):
                return {"success": False, "error": "fallback unavailable"}

        search_budget._counts["session-3"] = 48
        monkeypatch.setattr(web_tools, "_managed_web_search", lambda: True)
        monkeypatch.setattr(web_tools, "_is_tool_gateway_ready", lambda: True)
        monkeypatch.setattr(web_tools, "_rescue_eligible", lambda provider: False)
        monkeypatch.setattr(
            "agent.web_search_registry.get_provider", lambda name: _ManagedFirecrawlFail()
        )
        response, was_rescued, warning = web_tools._paid_search(
            _PerplexityFail(), "failed fallback", 2, "session-3"
        )
        assert response["success"] is False
        assert was_rescued is False
        assert warning is not None and "50 remote firecrawl" in warning
        assert search_budget._counts["session-3"] == 50

    def test_gateway_selected_firecrawl_failure_is_rescued(self, monkeypatch):
        monkeypatch.setattr(web_tools, "_load_web_config", lambda: {"backend": "firecrawl"})
        monkeypatch.setattr(
            "agent.web_search_provider.get_provider_env", lambda name: ""
        )
        monkeypatch.setattr(
            "tools.tool_backend_helpers.read_selection", lambda kind: "nous"
        )
        with patch.object(
            keyless_mcp, "search_with_failover", return_value=_ring_ok()
        ) as ring:
            out = self._dispatch(monkeypatch, _GatewayFirecrawlBoomProvider())
        assert out["success"] is True
        assert out["data"]["rescued_from"] == "firecrawl"
        ring.assert_called_once()

    def test_stateless_next_call_uses_chosen_backend(self, monkeypatch):
        calls = {"backend": 0}

        class _Counting(_KeyedBoomProvider):
            def search(self, query, limit=5):
                calls["backend"] += 1
                return {"success": False, "error": "HTTP 500 upstream exploded"}

        provider = _Counting()
        with patch.object(
            keyless_mcp, "search_with_failover", return_value=_ring_ok()
        ) as ring:
            self._dispatch(monkeypatch, provider)
            self._dispatch(monkeypatch, provider)
        # The chosen backend was attempted on BOTH calls (no sticky failover),
        # and each failure triggered its own one-shot rescue.
        assert calls["backend"] == 2
        assert ring.call_count == 2

    def test_rescue_failure_keeps_original_error(self, monkeypatch):
        with patch.object(
            keyless_mcp, "search_with_failover",
            return_value={"success": False, "error": "all throttled"},
        ):
            out = self._dispatch(monkeypatch, _KeyedBoomProvider())
        assert out["success"] is False
        assert "HTTP 500 upstream exploded" in out["error"]
        assert "keyless rescue also failed" in out["error"]

    def test_no_rescue_when_disabled(self, monkeypatch):
        monkeypatch.setattr(
            web_tools, "_load_web_config",
            lambda: {"backend": "keenable", "keyless_rescue": False},
        )
        with patch.object(keyless_mcp, "search_with_failover") as ring:
            out = self._dispatch(monkeypatch, _KeyedBoomProvider())
        assert out["success"] is False
        ring.assert_not_called()


class TestExtractRescue:
    async def _dispatch(self, monkeypatch, provider, urls):
        monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
        monkeypatch.setattr(
            "agent.web_search_registry.get_provider", lambda name: provider
        )

        async def _allow_all(url, **kwargs):
            return True

        monkeypatch.setattr(web_tools, "async_is_safe_url", _allow_all)
        raw = await web_tools.web_extract_tool(list(urls))
        data = json.loads(raw)
        return data["results"] if isinstance(data, dict) and "results" in data else data

    @pytest.mark.asyncio
    async def test_whole_batch_failure_rescued(self, monkeypatch):
        good = [
            {"url": "https://a", "title": "A", "content": "x" * 50,
             "raw_content": "x" * 50, "metadata": {"sourceURL": "https://a"}},
            {"url": "https://b", "title": "B", "content": "y" * 50,
             "raw_content": "y" * 50, "metadata": {"sourceURL": "https://b"}},
        ]
        with patch.object(
            keyless_mcp, "extract_with_failover", return_value=good
        ) as ring:
            results = await self._dispatch(
                monkeypatch, _KeyedBoomProvider(), ["https://a", "https://b"]
            )
        assert all(not r.get("error") for r in results)
        assert results[0]["content"].startswith("x")
        ring.assert_called_once()

    def test_rescue_extract_annotates_results(self, monkeypatch):
        good = [
            {"url": "https://a", "title": "A", "content": "x",
             "raw_content": "x", "metadata": {"sourceURL": "https://a"}},
        ]
        failed = [{"url": "https://a", "title": "", "content": "", "error": "HTTP 500"}]
        with patch.object(
            keyless_mcp, "extract_with_failover", return_value=good
        ):
            out = web_tools_rescue._rescue_extract("keenable", ["https://a"], failed)
        assert out[0]["metadata"]["rescued_from"] == "keenable"
        assert "HTTP 500" in out[0]["metadata"]["backend_error"]

    @pytest.mark.asyncio
    async def test_partial_failure_not_rescued(self, monkeypatch):
        class _Partial(_KeyedBoomProvider):
            def extract(self, urls, **kwargs):
                return [
                    {"url": urls[0], "title": "A", "content": "fine",
                     "raw_content": "fine", "metadata": {}},
                    {"url": urls[1], "title": "", "content": "", "error": "404"},
                ]

        with patch.object(keyless_mcp, "extract_with_failover") as ring:
            results = await self._dispatch(
                monkeypatch, _Partial(), ["https://a", "https://b"]
            )
        assert results[1].get("error")
        ring.assert_not_called()

    @pytest.mark.asyncio
    async def test_rescue_failure_keeps_original_errors(self, monkeypatch):
        still_bad = [
            {"url": "https://a", "title": "", "content": "", "error": "ring dead"},
            {"url": "https://b", "title": "", "content": "", "error": "ring dead"},
        ]
        with patch.object(
            keyless_mcp, "extract_with_failover", return_value=still_bad
        ):
            results = await self._dispatch(
                monkeypatch, _KeyedBoomProvider(), ["https://a", "https://b"]
            )
        assert all("HTTP 500" in r.get("error", "") for r in results)
