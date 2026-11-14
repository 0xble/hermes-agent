"""Regression coverage for the async reaped-slot probe in the classic FIFO check."""

import gc
import inspect
import warnings

import pytest


@pytest.mark.asyncio
async def test_classic_turn_holds_awaits_reaped_eviction_without_warning():
    from gateway.session_authority import SessionAuthority

    calls = []

    class RunnerDouble:
        running = True

        def _is_session_running(self, route):
            return self.running

        def _hm_evict_idle_stale_agent(self, route):
            pass

        async def _hm_evict_reaped_agent(self, route):
            calls.append(route)
            self.running = False

    authority = object.__new__(SessionAuthority)
    authority.runner = RunnerDouble()
    authority._classic_evict_at = {}

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", RuntimeWarning)
        result = authority._classic_turn_holds("route")
        if inspect.isawaitable(result):
            result = await result
        gc.collect()

    assert result is False
    assert calls == ["route"]
    assert not [warning for warning in captured if issubclass(warning.category, RuntimeWarning)]
