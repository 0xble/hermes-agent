"""Transient outages may reuse secrets only within the configured age limit."""

import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent.secret_sources import onepassword as op
from agent.secret_sources._cache import CachedFetch


@pytest.mark.parametrize("limit,age,expected", [(86400, 3600, True), (86400, 31536000, False),
                                               (0, 3600, False), (float('inf'), 31536000, False)])
def test_source_bounds_transient_fallback_without_refreshing_cache(monkeypatch, tmp_path, limit, age, expected):
    monkeypatch.setattr(op, 'get_source_environment', lambda: {})
    monkeypatch.setattr(op, 'find_op', lambda _: Path('/synthetic/op'))
    monkeypatch.setattr(op, '_rate_limit_cooldown_active', lambda *a: True)
    store = Mock()
    store.lookup.return_value = None
    entry = CachedFetch(secrets={'DUMMY': 'synthetic'}, fetched_at=time.time() - age)
    store.disk.read.side_effect = lambda key, ttl, home: entry if age < ttl else None
    monkeypatch.setattr(op, '_STORE', store)
    result = op.OnePasswordSource().fetch({'env': {'DUMMY': 'op://test/item/field'},
                                          'cache_max_stale_seconds': limit}, tmp_path)
    assert bool(result.secrets) is expected
    store.store.assert_not_called()

