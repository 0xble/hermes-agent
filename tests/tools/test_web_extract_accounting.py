"""Offline extraction accounting invariants: identity, completeness and TTL reuse."""

import asyncio
import copy
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tools import web_extract_direct as direct
from tools import web_result_cache as cache
from tools import web_tools as wt
from tools import web_tools_extract as extract


@pytest.fixture
def offline(monkeypatch, tmp_path):
    def denied(*args, **kwargs):
        raise AssertionError("External network is forbidden in extraction tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(wt, "_load_web_config", lambda: {"cache_ttl_minutes": 360})
    monkeypatch.setattr(wt, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(wt, "_get_extract_backend", lambda: "exa")
    monkeypatch.setattr(wt, "async_is_safe_url", AsyncMock(return_value=True))
    monkeypatch.setattr(extract, "_direct_html_enabled", lambda provider: True)
    monkeypatch.setattr(direct, "extract_direct", AsyncMock(return_value=None))
    monkeypatch.setattr("tools.website_policy.check_website_access", lambda url: None)
    monkeypatch.setattr(extract, "_rescue_eligible", lambda provider: False)
    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(cache, "_cache_dir", lambda: tmp_path / "cache")
    return monkeypatch


def run(provider, urls):
    return asyncio.run(extract._extract_safe_urls(provider, urls, None))


A, B, C = ("https://site.test/" + name for name in ("a", "b", "c"))
FINAL = "https://canonical.test/final"


def page(url, **kwargs):
    return {"url": url, "content": "Provider text. " * 60, "error": None, **kwargs}


@pytest.mark.parametrize("repeat", [False, True])
@pytest.mark.parametrize("fixed", ["none", "direct", "cache", "no_direct"])
@pytest.mark.parametrize("returned, expected", [
    ([page(B)], [None, B]),
    ([page(B), page(A)], [A, B]),
    ([page(B), page(B)], [None, B]),
    ([], [None, None]),
    ([page(FINAL, metadata={"sourceURL": B})], [None, FINAL]),
    ([page("", metadata={"sourceURL": A})], [A, None]),
    ([page(B, metadata={"sourceURL": A})], [B, None]),
    ([page(B, metadata={"sourceURL": A}), page(B)], [B, B]),
    ([page(FINAL)], [None, None]),
    ([page("https://foreign.test/x"), page(B)], [None, B]),
])
def test_batch_accounts_for_each_requested_identity(offline, fixed, returned, expected, repeat):
    provider = SimpleNamespace(name="exa", supports_extract=lambda: True, extract=AsyncMock(return_value=copy.deepcopy(returned)))
    if fixed == "direct":
        offline.setattr(direct, "extract_direct", AsyncMock(side_effect=lambda url, **kw: page(C) if url == C else None))
    if fixed == "cache":
        cache.extract_cache_put(C, page(C)["content"], provider="exa")
    if fixed == "no_direct":
        offline.setattr(wt, "_load_web_config", lambda: {"extract_direct": False})
    has_fixed = fixed in ("direct", "cache")
    requested = [A, A, B] if repeat else [A, B]
    expected = [expected[0], *expected] if repeat else expected
    urls = [C, *requested] if has_fixed else requested
    results = run(provider, urls)
    assert len(results) == len(urls)
    if has_fixed:
        fixed_result = results.pop(0)
        assert fixed_result["url"] == C and not fixed_result.get("error")
        assert fixed_result["content"] == page(C)["content"]
        if fixed == "cache":
            assert fixed_result["cached"]
    for requested_url, result, final in zip(requested, results, expected):
        if final is None:
            assert result["url"] == requested_url and result["error"]
            assert not result["content"]
        else:
            assert result["url"] == final and not result.get("error")
            assert result["content"] == page(final)["content"]
            if final != requested_url:
                assert result["requested_url"] == requested_url
        if fixed != "no_direct":
            assert result["fallback_from"] == "direct_http"
            assert result["extract_backend"] == "exa" and result["direct_fetch_error"]


def test_public_batch_keeps_invalid_blocked_and_repeated_slots(offline):
    provider = SimpleNamespace(name="exa", supports_extract=lambda: True, extract=AsyncMock(return_value=[page(B)]))
    offline.setattr("agent.web_search_registry.get_provider", lambda name: provider)
    blocked = "http://127.0.0.1/private"
    offline.setattr(wt, "async_is_safe_url", AsyncMock(side_effect=lambda url: url != blocked))
    urls = [None, A, B, A, blocked]
    results = json.loads(asyncio.run(wt.web_extract_tool(urls, char_limit=10000)))["results"]
    assert len(results) == len(urls)
    assert results[0]["error"]
    assert [result["url"] for result in results[1:]] == urls[1:]
    assert not results[2]["error"] and results[2]["content"]
    assert all(results[i]["error"] for i in (1, 3, 4))
    assert results[1]["fallback_from"] == results[3]["fallback_from"] == "direct_http"


def test_provider_exception_is_a_per_url_error_not_a_lost_batch(offline):
    provider = SimpleNamespace(name="exa", supports_extract=lambda: True,
                               extract=AsyncMock(side_effect=RuntimeError("Provider unavailable")))
    offline.setattr("agent.web_search_registry.get_provider", lambda name: provider)
    results = json.loads(asyncio.run(wt.web_extract_tool([A, B], char_limit=10000)))["results"]
    assert [result["url"] for result in results] == [A, B]
    assert all(result["error"] == "Provider unavailable" for result in results)
    assert all(result["fallback_from"] == "direct_http" for result in results)


@pytest.mark.parametrize("mode", ["success", "redirect", "thin", "error", "rescue"])
def test_direct_miss_reuses_only_successful_provider_pages_within_ttl(offline, mode):
    now = [100000.0]
    offline.setattr(cache.time, "time", lambda: now[0])
    result = page(FINAL if mode == "redirect" else A)
    if mode == "thin":
        result["content"] = "Thin teaser"
    if mode == "error":
        result["error"] = "Provider failed"
    provider = SimpleNamespace(name="exa", supports_extract=lambda: True, extract=AsyncMock(return_value=[result]))
    if mode == "rescue":
        provider.extract.side_effect = RuntimeError("Provider unavailable")
        offline.setattr(extract, "_rescue_eligible", lambda provider: True)
        offline.setattr(extract, "_rescue_extract", Mock(return_value=[page(A, metadata={"rescued_from": "exa"})]))
    offline.setattr("agent.web_search_registry.get_provider", lambda name: provider)

    def public_run():
        return json.loads(asyncio.run(wt.web_extract_tool([A], char_limit=10000)))["results"][0]

    first = public_run()
    now[0] += 58
    second = public_run()
    cacheable = mode in ("success", "redirect")
    assert provider.extract.await_count == (1 if cacheable else 2)
    assert direct.extract_direct.await_count == 2  # Direct still precedes cache lookup.
    assert first["content"] == second["content"]
    assert second["fallback_from"] == "direct_http" and second["direct_fetch_error"]
    if cacheable:
        assert second["cached"] is True and second["extract_backend"] == "exa_cache"
        assert second["url"] == first["url"]
        if mode == "redirect":
            assert second["requested_url"] == A
            assert cache.extract_cache_get(FINAL, provider="exa") is None
        # Provenance also survives a later configuration that disables direct fetch.
        offline.setattr(wt, "_load_web_config", lambda: {"cache_ttl_minutes": 360, "extract_direct": False})
        third = public_run()
        assert third["cached"] and third["fallback_from"] == "direct_http"
        assert third["extract_backend"] == "exa_cache" and third["url"] == first["url"]
        assert provider.extract.await_count == 1
        now[0] += 360 * 60
        public_run()
        assert provider.extract.await_count == 2
    else:
        assert not second.get("cached")
