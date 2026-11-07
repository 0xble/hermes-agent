"""Provider identities survive canonical URLs without guessing ambiguous redirects."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.tools.test_web_extract_accounting import A, B, C, FINAL, offline, page, run
from tools import web_result_cache as cache


@pytest.mark.parametrize("requested, returned", [
    ("https://example.com", "https://example.com/"),
    ("https://EXAMPLE.com/Article?topic=One", "https://example.com/Article?topic=One"),
    ("HTTPS://example.com/Article", "https://example.com/Article"),
    ("https://example.com:443/Article", "https://example.com/Article"),
    ("http://example.com:80/Article", "http://example.com/Article"),
    ("https://example.com/Article#section", "https://example.com/Article"),
    ("https://example.com/Article/", "https://example.com/Article"),
])
def test_canonical_batch_identities_keep_requested_cache_keys(offline, requested, returned):
    provider = SimpleNamespace(name="exa", extract=AsyncMock(return_value=[page(B + "/"), page(returned)]))
    first = run(provider, [requested, B])
    assert [result["url"] for result in first] == [returned, B + "/"]
    assert first[0]["requested_url"] == requested
    assert not any(result.get("error") for result in first)
    second = run(provider, [requested, B])
    assert second[0]["cached"] and second[0]["url"] == returned
    assert second[0]["requested_url"] == requested
    assert provider.extract.await_count == 1
    assert cache.extract_cache_get(requested, provider="exa")
    assert cache.extract_cache_get(returned, provider="exa") is None


@pytest.mark.parametrize("requested, returned, expected", [
    ([A, B], [page(B), page(FINAL)], [FINAL, B]),
    ([A, B, C], [page(C), page(FINAL)], [None, None, C]),
    ([A, B], [page(B), page(FINAL), page("https://other.test/final")], [None, B]),
    ([A, B], [page(B), page(FINAL, metadata={"sourceURL": C})], [None, B]),
    # Neither path case nor query values are interchangeable identities.
    ([A, B], [page(A.upper()), page(B + "?q=one")], [None, None]),
    ([A, B], [page(A + "?q=one"), page(B + "?q=two")], [None, None]),
    ([A, B], [page(B, metadata={"sourceURL": A + "/"})], [B, None]),
    ([A, A + "/", B], [page(FINAL, metadata={"sourceURL": A + "#part"}), page(B)], [None, None, B]),
    ([A, A + "/", B], [page(A), page(A + "#part"), page(B)], [A, None, B]),
])
def test_leftover_pairing_requires_one_unambiguous_identity(offline, requested, returned, expected):
    provider = SimpleNamespace(name="exa", extract=AsyncMock(return_value=returned))
    results = run(provider, requested)
    assert len(results) == len(requested)
    for url, result, final in zip(requested, results, expected):
        if final is None:
            assert result["url"] == url and result["error"]
            assert not result["content"]
        else:
            assert result["url"] == final and not result.get("error")
            if final != url:
                assert result["requested_url"] == url


@pytest.mark.parametrize("success_first", [False, True])
@pytest.mark.parametrize("error_url", [A, A + "/"])
def test_duplicate_identity_prefers_success_over_error(offline, success_first, error_url):
    returned = [page(error_url, error="Provider failed"), page(A)]
    if success_first:
        returned.reverse()
    provider = SimpleNamespace(name="exa", extract=AsyncMock(return_value=[*returned, page(B)]))
    first = run(provider, [A, B])
    assert first[0]["url"] == A and not first[0].get("error")
    assert first[0]["content"] == page(A)["content"]
    assert run(provider, [A, B])[0]["cached"]
    assert provider.extract.await_count == 1
