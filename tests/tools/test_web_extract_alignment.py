"""Provider identities survive canonical URLs without guessing ambiguous redirects."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.tools.test_web_extract_accounting import A, B, C, FINAL, page, run
from tests.tools.test_web_extract_accounting import offline as offline
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


_AMBIGUOUS = "provider returned results that could not be matched to this URL; retry it alone"
_MISSING = "Extract backend returned no result for this URL"


def _uncached(*urls):
    assert all(cache.extract_cache_get(url, provider="exa") is None for url in urls)


def test_unique_www_apex_batch_is_recovered_and_cached(offline):
    """example.com/a and /b redirecting to www.example.com/a and /b stay paid-once."""
    requested = ["https://example.com/a", "https://example.com/b"]
    returned = ["https://www.example.com/a", "https://www.example.com/b"]
    rows = [page(url, content=f"Paid page {url}. " * 40) for url in reversed(returned)]
    provider = SimpleNamespace(name="exa", extract=AsyncMock(return_value=rows))
    first = run(provider, requested)
    assert [result["url"] for result in first] == returned
    assert [result["requested_url"] for result in first] == requested
    assert [result["content"] for result in first] == [f"Paid page {url}. " * 40 for url in returned]
    assert not any(result.get("error") for result in first)
    second = run(provider, requested)
    assert all(result["cached"] for result in second)
    assert [result["url"] for result in second] == returned
    assert provider.extract.await_count == 1
    for requested_url, final in zip(requested, returned):
        cached = cache.extract_cache_get(requested_url, provider="exa")
        assert cached is not None and cached["url"] == final
        assert cache.extract_cache_get(final, provider="exa") is None


def test_scheme_and_www_batch_pairs_only_unique_paths(offline):
    requested = ["http://example.com/a", "http://example.com/b?q=1"]
    returned = ["https://www.example.com/a", "https://www.example.com/b?q=1"]
    provider = SimpleNamespace(name="exa", extract=AsyncMock(return_value=[page(url) for url in returned]))
    results = run(provider, requested)
    assert [result["url"] for result in results] == returned
    assert [result["requested_url"] for result in results] == requested
    assert not any(result.get("error") for result in results)


def test_ambiguous_batch_errors_without_caching(offline):
    """Same paths on non-alias hosts are not guessed, and the paid pages are not stored."""
    requested = ["https://example.com/a", "https://other.test/a"]
    returned = ["https://cdn.example.net/a", "https://static.other.test/a"]
    provider = SimpleNamespace(name="exa", extract=AsyncMock(return_value=[page(url) for url in returned]))
    results = run(provider, requested)
    assert [result["url"] for result in results] == requested
    assert [result["error"] for result in results] == [_AMBIGUOUS, _AMBIGUOUS]
    assert not any(result.get("content") for result in results)
    _uncached(*requested, *returned)


def test_duplicate_www_alias_is_not_attributed(offline):
    requested = ["https://example.com/a", "https://example.com/b"]
    returned = ["https://www.example.com/a", "https://WWW.example.com/a"]
    provider = SimpleNamespace(name="exa", extract=AsyncMock(return_value=[page(url) for url in returned]))
    results = run(provider, requested)
    assert [result["error"] for result in results] == [_AMBIGUOUS, _AMBIGUOUS]
    assert not any(result.get("content") for result in results)
    _uncached(*requested, *returned)


def test_partial_www_pair_does_not_claim_the_unrelated_page(offline):
    requested = ["https://example.com/a", "https://example.com/b"]
    paired = "https://www.example.com/a"
    unrelated = "https://evil.test/stolen"
    provider = SimpleNamespace(name="exa", extract=AsyncMock(return_value=[page(unrelated), page(paired)]))
    results = run(provider, requested)
    assert results[0]["url"] == paired and results[0]["requested_url"] == requested[0]
    assert results[0]["content"] == page(paired)["content"] and not results[0].get("error")
    assert results[1]["url"] == requested[1] and results[1]["error"] == _AMBIGUOUS
    assert not results[1].get("content")
    cached = cache.extract_cache_get(requested[0], provider="exa")
    assert cached is not None and cached["url"] == paired
    _uncached(requested[1], paired, unrelated)


def test_conflicting_source_blocks_www_pair(offline):
    requested = ["https://example.com/a", "https://example.com/b"]
    blocked = "https://www.example.com/a"
    paired = "https://www.example.com/b"
    provider = SimpleNamespace(name="exa", extract=AsyncMock(return_value=[
        page(blocked, metadata={"sourceURL": "https://evil.test/a"}),
        page(paired),
    ]))
    results = run(provider, requested)
    assert results[0]["url"] == requested[0] and results[0]["error"] == _AMBIGUOUS
    assert not results[0].get("content")
    assert results[1]["url"] == paired and results[1]["requested_url"] == requested[1]
    _uncached(requested[0], blocked)


def test_omitted_page_keeps_no_result_error(offline):
    provider = SimpleNamespace(name="exa", extract=AsyncMock(return_value=[page(B)]))
    results = run(provider, [A, B])
    assert results[0]["url"] == A and results[0]["error"] == _MISSING
    assert not results[0].get("content")
    assert results[1]["url"] == B and not results[1].get("error")
