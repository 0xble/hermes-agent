"""Direct web extraction exercises the real dispatcher with a mock HTTP transport, never the network."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from tools import web_extract_direct as direct
from tools import web_tools as wt


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provider = SimpleNamespace(name="fixture", extract=AsyncMock(side_effect=lambda urls, format=None: [
        {"url": url, "title": "provider", "content": f"paid:{url}", "error": None} for url in urls
    ]))
    monkeypatch.setattr(wt, "_get_extract_backend", lambda: "fixture")
    monkeypatch.setattr(wt, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(wt, "_resolve_extract_provider", lambda backend: (provider, None))
    monkeypatch.setattr(wt, "async_is_safe_url", AsyncMock(return_value=True))
    monkeypatch.setattr("tools.website_policy.check_website_access", lambda url: None)
    monkeypatch.setattr("tools.web_result_cache.extract_cache_get", lambda *a, **k: None)
    monkeypatch.setattr("tools.web_result_cache.extract_cache_put", lambda *a, **k: None)
    monkeypatch.setattr(wt, "_load_web_config", lambda: {})
    return provider


def _transport(monkeypatch, handler, *, safe=None):
    client = lambda **kwargs: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(direct, "create_ssrf_safe_async_client", client)
    monkeypatch.setattr(direct, "async_is_safe_url", AsyncMock(side_effect=safe or (lambda url: True)))


def _run(urls):
    return json.loads(asyncio.run(wt.web_extract_tool(urls, char_limit=10000)))["results"]


def test_plain_files_download_without_provider_and_preserve_mixed_order(harness, monkeypatch):
    seen = []

    def respond(request):
        seen.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "text/plain; charset=utf-8"}, text="fresh file")

    _transport(monkeypatch, respond)
    urls = ["https://site.test/page", "https://raw.githubusercontent.com/o/r/main/README.md",
            "https://site.test/list.json", "https://site.test/last"]
    results = _run(urls)
    assert [r["url"] for r in results] == urls
    assert [r["content"] for r in results] == [f"paid:{urls[0]}", "fresh file", "fresh file", f"paid:{urls[3]}"]
    assert seen == urls[1:3]
    harness.extract.assert_awaited_once()
    assert harness.extract.await_args.args[0] == [urls[0], urls[3]]


@pytest.mark.parametrize("status,mime,body", [
    (200, "text/html", "not a plain file"),
    (404, "text/plain", "missing"),
    (200, "application/octet-stream", "binary"),
    (200, "text/plain", "x" * (direct._MAX_BYTES + 1)),
])
def test_direct_rejection_falls_back_to_provider(harness, monkeypatch, status, mime, body):
    _transport(monkeypatch, lambda request: httpx.Response(status, headers={"content-type": mime}, text=body))
    assert _run(["https://site.test/readme.md"])[0]["title"] == "provider"
    harness.extract.assert_awaited_once()


def test_redirect_to_private_address_never_requested(harness, monkeypatch):
    seen = []

    def respond(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    _transport(monkeypatch, respond, safe=lambda url: "127.0.0.1" not in url)
    assert _run(["https://site.test/readme.md"])[0]["title"] == "provider"
    assert seen == ["https://site.test/readme.md"]


def test_docs_hit_index_miss_and_traversal(harness, monkeypatch):
    _transport(monkeypatch, lambda request: pytest.fail("docs must not use HTTP"))
    root = "https://hermes-agent.nousresearch.com/docs/"
    hit = _run([root + "user-guide/configuration", root])
    assert "local Hermes docs checkout" in hit[0]["title"]
    assert "Hermes Agent Configuration" in hit[0]["content"]
    # The docs root has an index in the checkout.
    assert "local Hermes docs checkout" in hit[1]["title"]
    missed = _run([root + "does-not-exist", root + "%2e%2e/%2e%2e/AGENTS.md", root + "llms.txt"])
    assert [r["title"] for r in missed] == ["provider"] * 3


def test_config_off_uses_provider_even_for_docs_and_plain_file(harness, monkeypatch):
    monkeypatch.setattr(wt, "_load_web_config", lambda: {"extract_direct": False})
    _transport(monkeypatch, lambda request: pytest.fail("disabled route must not use HTTP"))
    results = _run(["https://site.test/file.txt", "https://hermes-agent.nousresearch.com/docs/user-guide/configuration"])
    assert [r["title"] for r in results] == ["provider", "provider"]


def test_policy_gate_blocks_direct_route(harness, monkeypatch):
    monkeypatch.setattr("tools.website_policy.check_website_access", lambda url: "blocked")
    _transport(monkeypatch, lambda request: pytest.fail("policy-blocked direct HTTP"))
    assert _run(["https://site.test/file.txt"])[0]["title"] == "provider"


def test_secret_gate_refuses_before_direct_route(harness, monkeypatch):
    _transport(monkeypatch, lambda request: pytest.fail("secret-bearing URL requested"))
    outcome = json.loads(asyncio.run(wt.web_extract_tool(["https://site.test/file.md?api_key=sk-test-abc123"])))
    assert outcome["success"] is False
    harness.extract.assert_not_awaited()
