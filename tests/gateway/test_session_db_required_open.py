"""Required opens join exactly one attempt without weakening fallback or teardown."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from gateway.session_db_recovery import RecoverableHandleCache


@pytest.mark.parametrize("outcome", ["success", "failure", "closed"])
def test_required_open_joins_one_attempt(monkeypatch, outcome):
    cache = RecoverableHandleCache(clock=lambda: 0)
    path = Path("state.db")
    entered, release, joining = threading.Event(), threading.Event(), threading.Event()
    handle, replacement = object(), object()
    calls = []
    original_join = cache._join_open

    def join(*args):
        joining.set()
        return original_join(*args)

    monkeypatch.setattr(cache, "_join_open", join)

    def opener():
        calls.append(1)
        entered.set()
        assert release.wait(10)
        if outcome == "failure":
            raise OSError("unusable")
        return handle

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cache.get, path, opener)
        try:
            assert entered.wait(10)
            assert cache.get(path, opener) is None  # optional reads never block
            required = pool.submit(cache.get, path, opener, wait_for_open=True)
            assert joining.wait(10)
            assert not required.done()
            if outcome == "closed":
                cache.close_all(lambda _: None)
                assert cache.get(path, lambda: replacement) is replacement
        finally:
            release.set()
        expected = handle if outcome == "success" else None
        assert first.result(timeout=10) is expected
        assert required.result(timeout=10) is expected
    assert calls == [1]
    if outcome == "failure":
        assert cache.get(path, opener, wait_for_open=True) is None  # no backoff bypass
        assert calls == [1]
        assert "OSError: unusable" == cache.last_error(path)
    elif outcome == "closed":
        assert cache.get(path, opener, wait_for_open=True) is replacement
