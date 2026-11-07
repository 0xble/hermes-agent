"""Exa contents keep requested identities separate from canonical result URLs."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.web.exa import provider as exa
from tests.tools.test_web_extract_accounting import A, B, offline as offline, run
from tools import web_result_cache as cache


@pytest.mark.parametrize("route", ["sdk", "keyless"])
@pytest.mark.parametrize("identity", ["requested", "missing", "opaque"])
def test_canonical_batch_matches_only_reported_request_identities(offline, route, identity):
    urls = [A, B]
    finals = ["https://canonical.test/first", "https://canonical.test/second"]
    rows = []
    for requested, final in reversed(list(zip(urls, finals))):
        row = SimpleNamespace(url=final, title=requested, text=requested + " body" * 150)
        if identity != "missing":
            row.id = requested if identity == "requested" else "opaque-document-id"
        rows.append(row)
    client = SimpleNamespace(get_contents=Mock(return_value=SimpleNamespace(results=rows)))
    offline.setattr(exa, "provider_env", lambda key: None)
    offline.setattr(exa, "use_keyless", lambda *args: route == "keyless")
    get_client = Mock(return_value=client)
    offline.setattr(exa, "_get_exa_client", get_client)
    keyless = Mock(return_value=[{
        "url": row.url, "title": row.title, "content": row.text,
        "metadata": {"sourceURL": row.title} if identity == "requested" else {},
    } for row in rows])
    offline.setattr(exa, "keyless_extract", keyless)
    provider = exa.ExaWebSearchProvider()
    results = run(provider, urls)
    if identity == "requested":
        assert [result["url"] for result in results] == finals
        assert [result["requested_url"] for result in results] == urls
        assert [result["content"] for result in results] == [url + " body" * 150 for url in urls]
        assert all(not result.get("error") for result in results)
        cached = run(provider, urls)
        assert all(result["cached"] for result in cached)
        assert [result["url"] for result in cached] == finals
    else:
        assert [result["url"] for result in results] == urls
        assert all(result["error"] and not result["content"] for result in results)
        assert all(cache.extract_cache_get(url, provider="exa") is None for url in urls)
    if route == "sdk":
        client.get_contents.assert_called_once_with(urls, text=True)
        keyless.assert_not_called()
    else:
        keyless.assert_called_once()
        get_client.assert_not_called()
