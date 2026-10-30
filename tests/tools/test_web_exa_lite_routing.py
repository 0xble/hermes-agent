"""Exa tier routing through registered handlers, using a fake SDK only."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.web.exa import provider as exa
from tools import web_result_cache as cache
from tools import web_tools as wt


@pytest.fixture
def backend_harness(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provider = exa.ExaWebSearchProvider()
    sdk = Mock()
    sdk.search.return_value = SimpleNamespace(results=[
        SimpleNamespace(url="https://site.test/result", title="Result", highlights=["A result"]),
    ])
    monkeypatch.setattr(exa, "_get_exa_client", lambda: sdk)
    monkeypatch.setattr(exa, "provider_env", lambda key: "test-key")
    monkeypatch.setattr(exa, "use_keyless", lambda *a: False)
    monkeypatch.setattr("tools.web_tools_rescue._ring_vendor_keyless", lambda name: False)
    monkeypatch.setattr(wt, "_load_web_config", dict)
    monkeypatch.setattr(wt, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(wt, "_get_search_backend", lambda: "exa")
    monkeypatch.setattr("agent.web_search_registry.get_provider", lambda name: provider)
    monkeypatch.setattr(cache, "search_memo", cache.SearchMemo())
    return provider, sdk


def _search(query, **kwargs):
    entry = wt.registry.get_entry("web_search")
    assert entry is not None
    return json.loads(entry.handler({"query": query, **kwargs}))


@pytest.mark.parametrize("query,stakes,expected,expected_reason", [
    ("python list comprehensions tutorial", None, "auto", "default"),
    ("python list comprehensions tutorial", "standard", "lite", "hint_standard"),
    ("capital gains calculation", "standard", "auto", "protected_guard"),
    ("how to reduce taxable income", "standard", "auto", "protected_guard"),
    ("migraine remedies", "standard", "auto", "protected_guard"),
    ("aspirin adverse reactions", "standard", "auto", "protected_guard"),
    ("AAPL earnings report", "standard", "auto", "protected_guard"),
    ("tratamiento para la gripe", "standard", "auto", "protected_guard"),
    ("wajib pajak penghasilan", "standard", "auto", "protected_guard"),
    ("café recipes", "standard", "auto", "protected_guard"),
    ("ceramic glazing techniques", "standard", "lite", "hint_standard"),
    ("employment contract interpretation", "standard", "auto", "protected_guard"),
    ("financial reporting for small businesses", "standard", "auto", "protected_guard"),
    ("tax deductions for home office", "standard", "auto", "protected_guard"),
    ("migraine medical treatment", "standard", "auto", "protected_guard"),
    ("health and nutrition research", "standard", "auto", "protected_guard"),
    ("GDPR compliance checklist", "standard", "auto", "protected_guard"),
    ("regulatory requirements for export", "standard", "auto", "protected_guard"),
    ("official Python documentation", "standard", "auto", "protected_guard"),
    ("site:oregon.gov property records", "standard", "auto", "protected_guard"),
    ("primary source for historical speech", "standard", "auto", "protected_guard"),
    ("current events in Europe", "standard", "auto", "protected_guard"),
    ("technology news", "standard", "auto", "protected_guard"),
    ("rainfall today", "standard", "auto", "protected_guard"),
    ("latest Python release", "standard", "auto", "protected_guard"),
    ("breaking announcement", "standard", "auto", "protected_guard"),
    ("changes in the last 24 hours", "standard", "auto", "protected_guard"),
    ("announcements in the last 24-48h", "standard", "auto", "protected_guard"),
    ("announcements 48 hours ago", "standard", "auto", "protected_guard"),
    ("announcements this week", "standard", "auto", "protected_guard"),
    ("incidents past 48h", "standard", "auto", "protected_guard"),
    ("electricity prices now", "standard", "auto", "protected_guard"),
    ("exchange rates now", "standard", "auto", "protected_guard"),
    ("is this safe to use", "standard", "auto", "protected_guard"),
    ("ibuprofen side effects", "standard", "auto", "protected_guard"),
    ("401(k) withdrawal penalties", "standard", "auto", "protected_guard"),
    ("noncompete enforceability", "standard", "auto", "protected_guard"),
    ("can I combine these ingredients", "standard", "auto", "protected_guard"),
    ("ADA policy", "standard", "auto", "protected_guard"),
    ("最新ニュース", "standard", "auto", "protected_guard"),
    ("history of the roman empire", "standard", "lite", "hint_standard"),
    ("best hiking trails near seattle", "standard", "lite", "hint_standard"),
    # Known guard gaps (e.g. "ozempic reviews") rely on the agent's judgment by design.
    ("ozempic reviews", "standard", "lite", "hint_standard"),
    ("ceramic glazing techniques", "high", "auto", "hint_high"),
    ("ceramic glazing techniques", "unknown", "auto", "default"),
    ("FastAPI docs", None, "auto", "default"),
    ("FastAPI docs", "standard", "lite", "hint_standard"),
    ("FastAPI docs", "high", "auto", "hint_high"),
    ("how to install ripgrep on mac", None, "auto", "default"),
    ("how to install ripgrep on mac", "standard", "lite", "hint_standard"),
    ("how to install ripgrep on mac", "high", "auto", "hint_high"),
    ("best espresso grinder reviews", None, "auto", "default"),
    ("best espresso grinder reviews", "standard", "lite", "hint_standard"),
    ("best espresso grinder reviews", "high", "auto", "hint_high"),
    ("site:github.com tokio select example", None, "auto", "default"),
    ("site:github.com tokio select example", "standard", "lite", "hint_standard"),
    ("site:github.com tokio select example", "high", "auto", "hint_high"),
    ("amoxicillin side-effects", "standard", "auto", "protected_guard"),
    ("managing asthma", "standard", "auto", "protected_guard"),
    ("being sued by a former employee", "standard", "auto", "protected_guard"),
    ("TSLA quarterly results", "standard", "auto", "protected_guard"),
    ("USD EUR conversion", "standard", "auto", "protected_guard"),
    ("amoxicillin reviews", "standard", "auto", "protected_guard"),
    ("doxycycline tutorial", "standard", "auto", "protected_guard"),
    ("managing asthma tutorial", "standard", "auto", "protected_guard"),
    ("sued by a former employee docs", "standard", "auto", "protected_guard"),
    ("TSLA quarterly results tutorial", "standard", "auto", "protected_guard"),
    ("USD EUR conversion tutorial", "standard", "auto", "protected_guard"),
    ("site:github.com tokio select example site:unknown.test", "standard", "lite", "hint_standard"),
    ("como hacer pan", "standard", "lite", "hint_standard"),
    ("resep nasi goreng", "standard", "lite", "hint_standard"),
    ("obscure opaque phrase", "standard", "lite", "hint_standard"),
    ("site:unknown.test python tutorial", "standard", "lite", "hint_standard"),
    ("site:github.com.evil.test tokio select example", "standard", "lite", "hint_standard"),
    ("site:github.com:443 tokio select example", "standard", "lite", "hint_standard"),
    ("site:github.com tokio select", "standard", "lite", "hint_standard"),
    ("site:github.com/example/project tokio select example", "standard", "lite", "hint_standard"),
    ("site:docs.python.org asyncio api", "standard", "lite", "hint_standard"),
    ("site:amazon.com espresso grinder reviews", "standard", "lite", "hint_standard"),
    ("site:github.com asthma tutorial", "standard", "auto", "protected_guard"),
    ("React state docs", "standard", "lite", "hint_standard"),
    ("API rate limiting tutorial", "standard", "lite", "hint_standard"),
    ("JavaScript array convert examples", "standard", "lite", "hint_standard"),
    *[(query, "standard", "auto", "protected_guard") for query in (
        "tax software reviews", "lawyer reviews", "mortgage calculator api",
        "state law tutorial", "state tax docs", "interest rate examples",
        "exchange rate api", "mortgage rate reviews", "tax rate api",
        "currency conversion tutorial", "convert USD/EUR examples",
        "current mortgage rates", "state income tax brackets", "convert 401k to roth",
    )],
])
def test_search_quality_reaches_sdk_and_metadata(backend_harness, query, stakes, expected, expected_reason):
    provider, sdk = backend_harness
    result = _search(query, **({"stakes": stakes} if stakes is not None else {}))
    assert result["success"]
    assert result["data"]["search_type"] == expected
    assert sdk.search.call_args.kwargs["type"] == expected
    assert result["data"]["search_type_reason"] == expected_reason
    assert provider.SEARCH_TYPE == "auto"  # never mutate the shared plugin instance


def test_lite_and_auto_have_separate_shared_cache_entries(backend_harness):
    _, sdk = backend_harness
    query = "python list comprehensions tutorial"
    assert _search(query, stakes="standard")["data"]["search_type"] == "lite"
    assert _search(query, stakes="high")["data"]["search_type"] == "auto"
    # A fresh in-memory memo proves the disk identity also separates both lanes.
    cache.search_memo = cache.SearchMemo()
    assert _search(query, stakes="standard")["data"]["search_type"] == "lite"
    assert _search(query, stakes="high")["data"]["search_type"] == "auto"
    assert sdk.search.call_count == 2
    # Default and high share the Auto lane, but the reason belongs to this request.
    assert _search(query)["data"]["search_type_reason"] == "default"
    assert _search(query, stakes="high")["data"]["search_type_reason"] == "hint_high"
    assert _search(query, stakes="standard")["data"]["search_type_reason"] == "hint_standard"
    assert sdk.search.call_count == 2
    # Protected searches share Auto but preserve the reason on memory and disk hits.
    query = "tax software reviews"
    assert _search(query)["data"]["search_type_reason"] == "default"
    assert _search(query, stakes="standard")["data"]["search_type_reason"] == "protected_guard"
    cache.search_memo = cache.SearchMemo()
    assert _search(query, stakes="high")["data"]["search_type_reason"] == "hint_high"
    assert _search(query, stakes="standard")["data"]["search_type_reason"] == "protected_guard"
    assert sdk.search.call_count == 3


def test_concurrent_search_hints_are_request_local(backend_harness):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    provider, sdk = backend_harness
    barrier = Barrier(2)

    def response(query, **kwargs):
        barrier.wait(timeout=5)
        return SimpleNamespace(results=[SimpleNamespace(url="https://site.test/" + kwargs["type"],
                                                       title=kwargs["type"], highlights=[])])

    sdk.search.side_effect = response
    with ThreadPoolExecutor(max_workers=2) as pool:
        requests = [pool.submit(_search, "FastAPI docs", stakes=hint)
                    for hint in ("standard", "high")]
        results = [request.result(timeout=10)["data"] for request in requests]
    assert [(r["search_type"], r["search_type_reason"], r["web"][0]["title"]) for r in results] == [
        ("lite", "hint_standard", "lite"), ("auto", "hint_high", "auto"),
    ]
    assert provider.SEARCH_TYPE == "auto"


def test_pre_policy_auto_cache_does_not_hide_search_type(backend_harness):
    _, sdk = backend_harness
    query = "official ceramic material standards"
    name, identity = wt._search_cache_identity(SimpleNamespace(name="exa"))
    cache.search_memo.store(name, query, 10, {"success": True, "data": {"web": []}}, **identity)
    assert _search(query)["data"]["search_type"] == "auto"
    sdk.search.assert_called_once()


def test_protected_direct_provider_call_and_other_provider_compatibility(backend_harness, monkeypatch):
    provider, sdk = backend_harness
    assert provider.search("clinical trial findings")["data"]["search_type"] == "auto"
    assert sdk.search.call_args.kwargs["type"] == "auto"
    legacy = SimpleNamespace(name="fixture", supports_search=lambda: True, search=Mock(return_value={"success": True, "data": {"web": []}}))
    monkeypatch.setattr("agent.web_search_registry.get_provider", lambda name: legacy)
    assert _search("clinical trial findings", stakes="high")["success"]
    legacy.search.assert_called_once_with("clinical trial findings", 10)


def test_search_hits_carry_exa_preview_images(backend_harness):
    """The agent gets a page's preview image straight from the search hit, and rows without one stay unchanged."""
    _, sdk = backend_harness
    sdk.search.return_value = SimpleNamespace(results=[
        SimpleNamespace(url="https://shop.test/drill", title="Drill", highlights=["A drill"],
                        image="https://cdn.shop.test/drill.jpg"),
        SimpleNamespace(url="https://site.test/page", title="Page", highlights=[], image=None),
    ])
    first, second = _search("compact brushless drill kit")["data"]["web"]
    assert first["image"] == "https://cdn.shop.test/drill.jpg"
    assert "image" not in second
