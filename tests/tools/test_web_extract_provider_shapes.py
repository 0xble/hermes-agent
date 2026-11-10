"""Real adapter outputs obey one success-first extraction identity contract, offline."""

import json
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.web import keyless_mcp as keyless
from plugins.web._common import document, page_error
from tests.tools.test_web_extract_accounting import A, B, FINAL, offline, run
from tools import web_result_cache as cache
from tools import web_tools as wt

__all__ = ["offline"]  # Re-export the shared offline fixture for pytest discovery.


def page(url):
    return {"url": url, "content": f"Content fetched for {url}. " * 60, "error": None}


CASES = [
    ("exa", "ids", [A, B], [FINAL, "https://other.test/b"]),
    ("exa", "upgrade", [A.replace("https:", "http:"), B.replace("https:", "http:")], [A, B]),
    ("exa", "opaque", [A, B], [None, None]),
    ("exa", "requested-final", [A, B], [B, B]),
    ("exa", "omitted", [A, B], [None, B]),
    ("parallel", "redirect-error", [A, B], [FINAL, B]),
    ("parallel", "omitted", [A, B], [None, B]),
    ("parallel-keyless", "www-placeholder", [A], [A.replace("site.test", "www.site.test")]),
    ("parallel-keyless", "redirect-error", [A, B], [FINAL, B]),
    ("parallel-keyless", "upgrade", [A.replace("https:", "http:"), B.replace("https:", "http:")], [A, B]),
    ("parallel-keyless", "ambiguous", [A, B], [None, None]),
    ("parallel-keyless", "omitted", [A, B], [None, B]),
    ("exa-keyless", "per-url", [A, B], [A, B]),
    ("firecrawl-keyless", "per-url", [A, B], [A, B]),
    ("keenable-keyless", "source", [A, B], [FINAL, FINAL]),
    ("keenable", "source", [A, B], [FINAL, FINAL]),
    ("firecrawl", "final-source", [A, B], [FINAL, B]),
    ("firecrawl", "error", [A, B], [None, B]),
    ("tavily", "failed-results", [A, B], [FINAL, B]),
    ("tavily", "failed-urls", [A, B], [FINAL, B]),
    ("tavily", "omitted", [A, B], [None, B]),
    ("perplexity", "per-url", [A, B], [A, B]),
    ("perplexity", "omitted", [A, B], [None, B]),
    ("rescue", "placeholder", [A, B], [FINAL, B]),
    *[(name, "failure", [A, B], [None, None]) for name in (
        "exa", "parallel", "tavily", "firecrawl", "keenable", "perplexity",
        "parallel-keyless", "exa-keyless", "firecrawl-keyless", "keenable-keyless",
    )],
]


def keyless_adapter(monkeypatch, vendor, shape):
    """Exercise a real keyless adapter and ring with fake transport responses."""
    if shape == "failure":
        from plugins.web.firecrawl import provider as fc
        failure = Mock(side_effect=keyless.KeylessMCPError("Endpoint unavailable"))
        monkeypatch.setattr(keyless, "mcp_call", failure)
        monkeypatch.setattr(keyless, "_keenable_request", failure)
        monkeypatch.setattr(fc, "_KeylessFirecrawlClient", lambda: NS(scrape=failure))
    elif vendor == "parallel":
        final = A.replace("site.test", "www.site.test") if shape == "www-placeholder" else FINAL
        rows = [{"url": B, "excerpts": [page(B)["content"]]}]
        if shape != "omitted":
            rows.append({"url": final, "full_content": page(A)["content"]})
        if shape == "www-placeholder":
            rows = rows[1:]
        if shape == "ambiguous":
            rows = [{"url": FINAL, "full_content": page(A)["content"]}]
        if shape == "upgrade":
            rows = [{"url": u, "content": page(u)["content"]} for u in (B, A)]
        errors = [{"url": A, "error_type": "fetch_failed"}] if shape == "redirect-error" else []
        monkeypatch.setattr(keyless, "mcp_call", Mock(return_value=json.dumps({"results": rows, "errors": errors})))
    elif vendor == "exa":
        monkeypatch.setattr(keyless, "mcp_call", lambda endpoint, tool, arguments: "# Page\n" + page(arguments["urls"][0])["content"])
    elif vendor == "firecrawl":
        from plugins.web.firecrawl import provider as fc
        monkeypatch.setattr(fc, "_KeylessFirecrawlClient", lambda: NS(scrape=lambda *, url, **kw: {
            "data": {"markdown": page(url)["content"], "metadata": {"sourceURL": FINAL}}
        }))
    else:
        monkeypatch.setattr(keyless, "_keenable_request", lambda *args, params: {"url": FINAL, "content": page(params["url"])["content"]})
    # The actual ring/failover passes the selected adapter's rows through unchanged.
    monkeypatch.setattr(keyless, "_ring_order", lambda name: [vendor])
    return NS(name=vendor + "-keyless", extract=lambda urls, **kw: keyless.extract_with_failover(vendor, urls))


def failed_adapter(monkeypatch, module, name):
    seam, class_name = {
        "exa": ("_get_exa_client", "ExaWebSearchProvider"),
        "parallel": ("_get_async_client", "ParallelWebSearchProvider"),
        "firecrawl": ("_get_firecrawl_client", "FirecrawlWebSearchProvider"),
        "tavily": ("_tavily_request", "TavilyWebSearchProvider"),
        "perplexity": ("_perplexity_request", "PerplexityWebSearchProvider"),
        "keenable": (None, "KeenableWebSearchProvider"),
    }[name]
    failure = Mock(side_effect=RuntimeError("Endpoint unavailable"))
    if seam:
        monkeypatch.setattr(module, seam, failure)
    else:
        monkeypatch.setattr("requests.get", failure)
    if name == "firecrawl":
        monkeypatch.setattr(module, "_use_keyless_ring", lambda *args: False)
    if name == "tavily":
        monkeypatch.setattr(module, "_auth", lambda action: ("", None, "fake "))
    return getattr(module, class_name)()


def adapter(monkeypatch, name, shape):
    """Fake only SDK/HTTP seams; exercise each repository adapter and the dispatcher."""
    if name.endswith("-keyless"):
        return keyless_adapter(monkeypatch, name.removesuffix("-keyless"), shape)

    if name == "rescue":
        from tools import web_tools_extract as extract
        monkeypatch.setattr(extract, "_rescue_eligible", lambda provider: True)
        monkeypatch.setattr(keyless, "extract_with_failover", Mock(return_value=[document(B, "B", page(B)["content"]),
            document(FINAL, "A", page(A)["content"]), page_error(A, "no content returned")]))
        return NS(name="exa", extract=Mock(side_effect=RuntimeError("Provider unavailable")))

    import importlib
    module = importlib.import_module(f"plugins.web.{name}.provider")
    if hasattr(module, "use_keyless"):
        monkeypatch.setattr(module, "use_keyless", lambda *args: False)
        monkeypatch.setattr(module, "provider_env", lambda key: None)
    if shape == "failure":
        return failed_adapter(monkeypatch, module, name)
    if name == "exa":
        rows = [NS(url=B, id=B, title="B", text=page(B)["content"])]
        if shape != "omitted":
            rows.append(NS(url=A, id="opaque-id", title="A", text=page(A)["content"]))
        if shape in ("ids", "opaque"):
            rows = [NS(url=final, id=requested if shape == "ids" else "opaque-id", title=requested,
                       text=page(requested)["content"]) for requested, final in ((B, "https://other.test/b"), (A, FINAL))]
        if shape == "requested-final":
            rows = [NS(url=B, id=u, title=u, text=page(u)["content"]) for u in (B, A)]
        monkeypatch.setattr(module, "_get_exa_client", lambda: NS(get_contents=Mock(return_value=NS(results=rows))))
        return module.ExaWebSearchProvider()
    if name == "parallel":
        rows = [NS(url=B, title="B", full_content=None, excerpts=[page(B)["content"]])]
        errors = []
        if shape == "redirect-error":
            rows.append(NS(url=FINAL, title="A", full_content=page(A)["content"], excerpts=[]))
            errors = [NS(url=A, content=None, error_type="fetch_failed")]
        monkeypatch.setattr(module, "_get_async_client", lambda: NS(beta=NS(extract=AsyncMock(return_value=NS(results=rows, errors=errors)))))
        return module.ParallelWebSearchProvider()
    if name == "keenable":
        monkeypatch.setattr("requests.get", lambda endpoint, *, params, **kw: NS(status_code=200, json=lambda: {"url": FINAL, "content": page(params["url"])["content"]}))
        return module.KeenableWebSearchProvider()
    if name == "firecrawl":
        monkeypatch.setattr(module, "_use_keyless_ring", lambda *args: False)
        monkeypatch.setattr(module, "check_website_access", lambda url: None)
        monkeypatch.setattr(module, "is_safe_url", lambda url: True)

        def scrape(*, url, **kw):
            if url == A and shape == "error":
                raise RuntimeError("Scrape unavailable")
            final = FINAL if url == A else B
            # Exercise typed metadata as well as the final-URL sourceURL shape.
            return {"data": {"markdown": page(url)["content"], "metadata": NS(model_dump=lambda: {"sourceURL": final})}}

        monkeypatch.setattr(module, "_get_firecrawl_client", lambda *args: NS(scrape=scrape))
        return module.FirecrawlWebSearchProvider()
    if name == "tavily":
        raw: dict[str, Any] = {"results": [{"url": B, "content": page(B)["content"]}]}
        if shape != "omitted":
            raw["results"].append({"url": FINAL, "raw_content": page(A)["content"]})
            raw["failed_results" if shape == "failed-results" else "failed_urls"] = (
                [{"url": A, "error": "fetch_failed"}] if shape == "failed-results" else [A])
        monkeypatch.setattr(module, "_auth", lambda action: ("", None, "fake "))
        monkeypatch.setattr(module, "_tavily_request", Mock(return_value=raw))
        return module.TavilyWebSearchProvider()
    rows = [{"url": B, "text": page(B)["content"]}]
    if shape != "omitted":
        rows.append({"url": A, "text": page(A)["content"]})
    monkeypatch.setattr(module, "_perplexity_request", Mock(return_value={"results": rows}))
    return module.PerplexityWebSearchProvider()


@pytest.mark.parametrize("name, shape, requested, expected", CASES, ids=[f"{name}-{shape}" for name, shape, *_ in CASES])
def test_adapter_shapes_preserve_content_identity_and_requested_cache_keys(offline, name, shape, requested, expected):
    offline.setattr(wt, "_load_web_config", lambda: {"extract_direct": False, "cache_ttl_minutes": 360})
    provider = adapter(offline, name, shape)
    results = run(provider, requested)
    assert len(results) == len(requested)
    for url, result, final in zip(requested, results, expected):
        if final is None:
            assert result["url"] == url and result["error"] and not result.get("content")
            assert cache.extract_cache_get(url, provider=provider.name) is None
        else:
            assert result["url"] == final and result["content"] and not result.get("error")
            assert url.replace("http:", "https:") in result["content"]
            if final != url:
                assert result["requested_url"] == url
            if final not in requested:
                assert cache.extract_cache_get(final, provider=provider.name) is None
            cached = cache.extract_cache_get(url, provider=provider.name)
            if name == "rescue":
                assert result["metadata"]["rescued_from"] == "exa" and cached is None
            else:
                assert cached and cached["url"] == final and cached["content"] == result["content"]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("requested, rows, expected", [
    ([A], [page(FINAL), page_error(A, "no content returned")], [FINAL]),
    ([A, B], [page(FINAL), page_error(A, "fetch failed"), page(B)], [FINAL, B]),
    ([A, B], [page(FINAL), page_error(A, "fetch failed"), page_error(B, "fetch failed")], [None, None]),
    ([A, B], [page(FINAL), page("https://other.test/final"), page_error(A, "failed"), page(B)], [None, B]),
    ([A, B], [page(B), page(B), page_error(A, "failed")], [None, B]),
    ([A, A.replace("https:", "http:")], [page(A + "#part")], [None, None]),
    ([A, A.replace("site.test", "www.site.test")], [page(FINAL)], [None, None]),
    ([A, B], [page(A.replace("site.test", "www.site.test")), page(B.replace("site.test", "www.site.test"))],
     [A.replace("site.test", "www.site.test"), B.replace("site.test", "www.site.test")]),
    (["https://site.test:8080/a", B], [page("https://site.test:8081/a"), page(B + "?different")], [None, None]),
    (["http://SITE.test:80/a#old", "http://site.test:80/b"],
     [page("https://site.test:443/a/#new"), page("https://site.test:443/b")],
     ["https://site.test:443/a/#new", "https://site.test:443/b"]),
    ([A], [page(FINAL), page_error(A, "placeholder"), page_error("https://unknown.test", "failed")], [FINAL]),
    ([A, B], [{**page(FINAL), "metadata": {"sourceURL": A}},
              {**page("https://other.test/b"), "metadata": {"sourceURL": B}}], [FINAL, "https://other.test/b"]),
])
def test_only_successes_fill_slots_without_guessing_ambiguous_batches(offline, reverse, requested, rows, expected):
    provider = NS(name="exa", extract=AsyncMock(return_value=list(reversed(rows)) if reverse else rows))
    results = run(provider, requested)
    assert len(results) == len(requested)
    for url, result, final in zip(requested, results, expected):
        if final is None:
            assert result["url"] == url and result["error"] and not result.get("content")
        else:
            assert result["url"] == final and result["content"] and not result.get("error")
