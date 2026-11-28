"""A long housekeeping chore (the nightly state.db prune) runs off the housekeeping tick."""

import threading

import gateway.runtime_ownership as runtime_ownership
from gateway.run import _StateDbMaintenancePass
from gateway.runtime_ownership import ProfileOwnership


def test_long_chore_never_holds_the_tick_and_runs_one_at_a_time(monkeypatch):
    monkeypatch.setattr(runtime_ownership, "process_ownership", ProfileOwnership())
    release, started = threading.Event(), []

    def long_pass():
        started.append(threading.current_thread())
        release.wait(5)

    chore = _StateDbMaintenancePass("state.db maintenance tick", long_pass, threading.Event())

    chore()  # returns while the pass is still running
    assert chore.thread.is_alive()
    first = chore.thread

    chore()  # the next tick finds the previous pass running and skips
    assert chore.thread is first

    release.set()
    first.join(5)
    assert not first.is_alive()

    chore()  # a later tick starts a fresh pass
    chore.thread.join(5)
    assert len(started) == 2 and started[0] is not started[1]


def test_running_chore_is_a_registered_writer(monkeypatch):
    ownership = ProfileOwnership()
    monkeypatch.setattr(runtime_ownership, "process_ownership", ownership)
    release = threading.Event()
    chore = _StateDbMaintenancePass("state.db maintenance tick", lambda: release.wait(5), threading.Event())

    chore()

    assert chore.thread in ownership._writers  # close() keeps the profile lock while it may write
    release.set()
    chore.thread.join(5)


def test_start_writer_forgets_finished_writers_but_keeps_live_ones():
    ownership = ProfileOwnership()
    finished = threading.Thread(target=lambda: None)
    ownership.start_writer(finished)
    finished.join(5)
    release = threading.Event()
    live = threading.Thread(target=lambda: release.wait(5))

    ownership.start_writer(live)

    assert ownership._writers == [live]
    release.set()
    live.join(5)


def test_stop_cancels_a_running_prune_pass(monkeypatch):
    """Shutdown sets the pass's cancel signal, which a prune budget treats as its deadline."""
    from hermes_state_prune_budget import PruneBudget, PruneBudgetExpired

    monkeypatch.setattr(runtime_ownership, "process_ownership", ProfileOwnership())
    entered, outcome = threading.Event(), []

    def pass_with_budget():
        budget = PruneBudget()
        entered.set()
        try:
            while True:
                budget.pause(0.05)
        except PruneBudgetExpired:
            outcome.append("stopped")

    housekeeping_stop = threading.Event()
    chore = _StateDbMaintenancePass("state.db maintenance tick", pass_with_budget, housekeeping_stop)
    chore()
    assert entered.wait(5)

    housekeeping_stop.set()  # shutdown begins; the housekeeping loop has not reached its exit yet
    chore.thread.join(5)

    assert outcome == ["stopped"] and not chore.thread.is_alive()
