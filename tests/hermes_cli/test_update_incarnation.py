"""A7 identities at the durable marker/pause ownership boundary, independent of clock phase."""
from __future__ import annotations

import os
import time

import pytest

from hermes_cli import update_lock, update_pause_record as pause_record


@pytest.mark.parametrize("actual, recorded, live", [
    (1000.5, "ct:1000.500", True),
    (1000.5, "ct:1000.000", False),  # a high-resolution prior incarnation, not a coarse producer
    (1000.5, "ct:1000", False),
    (1000.5, "ct:999.500", False),
    (1000.0, "ct:1000.000", True),  # .000 can also be an exact, legitimate identity
    (1000.5, None, False),
    (None, None, True),
    (None, "ct:1000.000", False),
])
def test_only_this_incarnation_holds_its_pause_and_marker(tmp_path, monkeypatch, actual, recorded, live):
    pid, foreign = os.getpid(), 4242
    world = update_lock._World(pid, actual, time.time(), lambda p: p == foreign, lambda p: 900.0)
    monkeypatch.setattr(update_lock, "_real_world", lambda: world)
    monkeypatch.setattr(update_lock, "process_create_time", lambda p=None: actual)
    monkeypatch.setattr(update_lock, "_is_ancestor_pid", lambda p: False)
    monkeypatch.delenv(update_lock.HANDOFF_PID_ENV, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ident = {"pid": pid, "ct": recorded}

    # A previous updater's durable restart debt must be claimable, never mistaken for ours.
    pause_record.write({"pause_id": "a" * 32, "resume_needed": True, "profiles": {"p": 99}}, owner=ident)
    claim = pause_record.claim(pause_record.record_path())
    assert (claim is None) is live, "a stale own-PID pause was held instead of recoverable"
    if claim:
        assert claim[1]["claimer"] == pause_record.identity()
        assert claim[1]["token"]["profiles"] == {"p": 99}
        pause_record.release_claims([claim[0]])
        assert not claim[0].exists()

    # An unrelated custodian authorizes only the current delegate, including no-ct degradation.
    marker = tmp_path / "marker"
    delegate = "" if recorded is None else f"delegate:{pid} {recorded}\n"
    if actual is None and recorded is None:
        body = f"{pid}\n{int(world.now)}\n"  # a degraded owner writes no-ct, not a no-ct delegate
    else:
        body = f"{foreign}\n{int(world.now)}\nct:900.000\n{delegate}"
    marker.write_text(body)
    lock = update_lock.UpdateLock(path=marker)
    try:
        assert lock.acquire() is live, "a stale own-PID delegate authorized an unrelated update"
    finally:
        lock.release()
    if live and actual is None:
        assert not marker.exists()
    elif live:
        after = marker.read_text().splitlines()
        assert after[0] == str(foreign) and "ct:900.000" in after, "the custodian keeps its claim"
        assert not any(line.startswith("delegate:") for line in after), "release retires only us"
    else:
        assert marker.read_text() == body, "a refused contender mutated another update's marker"

    # The documented cross-process rule remains deliberately broader and conservative.
    assert update_lock.incarnation_live(foreign, "ct:899.500") is True
    assert update_lock.incarnation_live(foreign, "ct:890.000") is False
    assert update_lock.incarnation_live(foreign, None) is None
    assert update_lock.incarnation_live(9999, "ct:900.000") is False
