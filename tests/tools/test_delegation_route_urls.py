"""Durable route URLs are parseable, secret-free and stable under sanitization."""
from urllib.parse import urlsplit

import pytest

from tools.custom_subagents import nonsecret_route_url


@pytest.mark.parametrize("url,expected", [
    ("http://[::1]:8000/v1", "http://[::1]:8000/v1"),
    ("http://[::1]/v1", "http://[::1]/v1"),
    ("http://[::1]:0/v1", "http://[::1]:0/v1"),
    ("https://user:secret@[2001:db8::1]:8443/v1?token=secret#secret", "https://[2001:db8::1]:8443/v1"),
    ("http://127.0.0.1:8000/v1", "http://127.0.0.1:8000/v1"),
    ("https://user:secret@fixture.invalid/v1?token=secret#secret", "https://fixture.invalid/v1"),
])
def test_durable_url_preserves_endpoint_without_secrets(url, expected):
    stored = nonsecret_route_url(url)
    assert stored == expected
    assert nonsecret_route_url(stored) == stored
    assert urlsplit(stored).port == urlsplit(url).port
    assert "secret" not in stored


def test_historical_unbracketed_ipv6_is_not_guessed():
    assert nonsecret_route_url("http://::1:8000/v1") == ""
