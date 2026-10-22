"""Due-time and worker-release wakes supplement the fixed-period cron ticker."""

import errno
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cron import jobs, scheduler, scheduler_admission as admission
from cron import scheduler_provider as sp

INTERVAL = 60
START = 100.0


def _drive(loop, tmp_path, monkeypatch, *, overrun=False, fail=False):
    clock = SimpleNamespace(now=START)
    ticks = []
    target = 3 if overrun or fail else 7
    due_instants = [START + offset for offset in (30, 90)]
    release_instants = [START + offset for offset in (45, 105)]
    monkeypatch.setattr(sp, "time", SimpleNamespace(monotonic=lambda: clock.now))

    class Stop:
        def is_set(self):
            return len(ticks) >= target

        def wait(self, timeout):
            if not self.is_set():
                assert timeout >= 0
                clock.now += timeout
            return self.is_set()

    def wait_for_tick(stop, timeout):
        future = [instant for instant in release_instants if instant > clock.now]
        until_release = min(future) - clock.now if future else timeout
        stop.wait(min(timeout, until_release))

    def due_wait(max_wait, profile_homes=None):
        future = [instant for instant in due_instants if instant > clock.now]
        return min(max_wait, min(future) - clock.now) if future else max_wait

    def tick(**kwargs):
        ticks.append(clock.now)
        clock.now += 1.7 * INTERVAL if overrun and len(ticks) == 2 else 0.25
        if fail and len(ticks) == 2:
            raise OSError(errno.EMFILE, "Too many open files")
        return 0

    # Advance complete logical waits, not the production wake poller's sub-waits.
    monkeypatch.setattr(admission, "wait_for_tick", wait_for_tick)
    monkeypatch.setattr(admission, "due_wait_for_homes", due_wait)
    monkeypatch.setattr(scheduler, "tick", tick)
    monkeypatch.setattr(scheduler, "_reclaim_fds_best_effort", lambda: None)
    monkeypatch.setattr(jobs, "record_ticker_heartbeat", lambda **kwargs: None)
    monkeypatch.setattr(jobs, "record_ticker_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(jobs, "clear_ticker_error", lambda: None)
    monkeypatch.setattr(sp.InProcessCronScheduler, "recover_interrupted", lambda self: 0)
    (tmp_path / "cron").mkdir(exist_ok=True)
    homes = None if loop == "single" else [("p1", tmp_path)]
    sp.InProcessCronScheduler().start(Stop(), interval=INTERVAL, profile_homes=homes)
    return ticks


@pytest.mark.parametrize("loop", ["single", "multiplex"])
def test_early_due_and_release_wakes_preserve_periodic_deadlines(loop, tmp_path, monkeypatch):
    ticks = _drive(loop, tmp_path, monkeypatch)
    assert ticks == pytest.approx([START + offset for offset in (0, 30, 45, 60, 90, 105, 120)])


@pytest.mark.parametrize("loop", ["single", "multiplex"])
def test_early_tick_overrun_rebaselines_without_a_burst(loop, tmp_path, monkeypatch):
    ticks = _drive(loop, tmp_path, monkeypatch, overrun=True)
    assert ticks == pytest.approx([START, START + 30, START + 30 + 1.7 * INTERVAL + INTERVAL])


@pytest.mark.parametrize("loop", ["single", "multiplex"])
def test_early_failure_retains_backoff_despite_worker_release(loop, tmp_path, monkeypatch):
    ticks = _drive(loop, tmp_path, monkeypatch, fail=True)
    assert ticks == pytest.approx([START, START + 30, START + 30 + INTERVAL])


def test_zero_wait_still_observes_stop_event():
    stop = Mock()
    stop.is_set.return_value = False
    admission.begin_tick()
    admission.wait_for_tick(stop, 0)
    stop.wait.assert_called_once_with(0)
