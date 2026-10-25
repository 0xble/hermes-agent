"""Abandoned claim refunds retain caller context and expose release failures."""

import asyncio
import concurrent.futures
import contextvars
import logging
import threading

import pytest

from gateway.run_notifications_ledger import claim_off_loop


@pytest.mark.parametrize("finished", [False, True])
@pytest.mark.parametrize("release_fails", [False, True])
def test_abandoned_claim_refund_context_and_failure_log(monkeypatch, caplog, finished, release_fails):
    scope = contextvars.ContextVar[str | None]("refund_profile", default=None)
    entered, proceed, released = threading.Event(), threading.Event(), threading.Event()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    submitted, observations, release_threads = [], [], []
    real_submit = pool.submit

    def submit(*args, **kwargs):
        future = real_submit(*args, **kwargs)
        submitted.append(future)
        return future

    def claim():
        scope.set("claim-local-mutation")
        entered.set()
        assert proceed.wait(5)
        return "claim-id"

    def release(result):
        observations.append((result, scope.get(), threading.current_thread().daemon))
        release_threads.append(threading.current_thread())
        released.set()
        if release_fails:
            raise RuntimeError("refund write failed")

    monkeypatch.setattr(pool, "submit", submit)
    caplog.set_level(logging.WARNING, logger="gateway.run")

    async def exercise():
        asyncio.get_running_loop().set_default_executor(pool)
        scope.set("owning-profile")
        task = asyncio.create_task(claim_off_loop(claim, release))
        try:
            while not entered.is_set():
                await asyncio.sleep(0)
            if finished:
                proceed.set()
                assert submitted[0].result(timeout=5) == "claim-id"
                pool.shutdown(wait=True)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            proceed.set()

    asyncio.run(exercise())
    assert released.wait(5)
    for thread in release_threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert observations == [("claim-id", "owning-profile", False)]
    failures = [record for record in caplog.records if "Could not refund abandoned durable claim" in record.message]
    assert len(failures) == int(release_fails)
    if release_fails:
        assert failures[0].exc_info[1].args == ("refund write failed",)
