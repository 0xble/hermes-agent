"""Adoption drains yield to discard callbacks without losing retained writes."""

import asyncio
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from gateway.run import GatewayRunner
from tests.gateway import test_session_hygiene_turnhold_adoption as adoption


DRAINS = [
    ("_drain_background", "_background_tasks"),
    ("_drain_deferred", "_deferred_agent_cleanup_tasks"),
]


@pytest.mark.parametrize(("helper", "attribute"), DRAINS)
def test_completed_adoption_task_drain_runs_queued_discard_callbacks(helper, attribute):
    # A subprocess bounds starvation even when the loop cannot run wait_for's
    # timeout. No random scheduling: add_done_callback queues a completed task.
    probe = textwrap.dedent("""\
        import asyncio
        import faulthandler
        import sys
        from types import SimpleNamespace
        from gateway.run import GatewayRunner
        from tests.gateway import test_session_hygiene_turnhold_adoption as adoption

        async def main():
            task = asyncio.create_task(asyncio.sleep(0))
            await task
            retained = set()
            GatewayRunner._track_task_in(retained, task)
            assert task.done() and task in retained
            runner = SimpleNamespace(**{sys.argv[2]: retained})
            await getattr(adoption, sys.argv[1])(runner, timeout=0.1)
            assert not retained, "drain returned before the discard callback ran"

        faulthandler.dump_traceback_later(2)
        asyncio.run(main())
    """)
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe, helper, attribute],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"adoption drain starved its loop and timeout callback:\n{exc.stderr}"
        ) from exc
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize(("helper", "attribute"), DRAINS)
@pytest.mark.parametrize("outcome", ["complete", "cancel_task", "timeout", "cancel_drain"])
async def test_adoption_drain_joins_writes_and_preserves_cancellation(helper, attribute, outcome):
    retained = set()
    runner = SimpleNamespace(**{attribute: retained})
    started, release, written = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def write():
        started.set()
        await release.wait()
        written.set()
        # A write's completion can retain another write before the drain finishes.
        if outcome == "complete":
            GatewayRunner._track_task_in(retained, asyncio.create_task(asyncio.sleep(0)))

    task = GatewayRunner._track_task_in(retained, asyncio.create_task(write(), name="adoption-write"))
    await started.wait()
    drain = asyncio.create_task(getattr(adoption, helper)(runner, timeout=2))
    try:
        await asyncio.sleep(0)
        assert not drain.done() and not written.is_set()
        if outcome == "complete":
            release.set()
            await drain
            assert written.is_set() and not retained
        elif outcome == "cancel_task":
            task.cancel()
            await drain
            assert task.cancelled() and not written.is_set() and not retained
        elif outcome == "timeout":
            with pytest.raises(TimeoutError, match="adoption-write"):
                await drain
            assert task.cancelled() and not written.is_set()
        else:
            drain.cancel()
            with pytest.raises(asyncio.CancelledError):
                await drain
            assert task.cancelled() and not written.is_set()
    finally:
        release.set()
        drain.cancel()
        for pending in list(retained):
            pending.cancel()
        await asyncio.gather(drain, *list(retained), return_exceptions=True)
