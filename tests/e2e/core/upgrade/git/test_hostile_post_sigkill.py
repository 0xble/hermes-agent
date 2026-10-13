"""Hostile post: killed post-commit tail.

See ``_hostile_post`` for the shared harness and full cell contract.
The module-scoped real install is shared only within this bounded file.
"""
from __future__ import annotations

from tests.e2e.core.upgrade.git import _hostile_post as C
from tests.e2e.core.upgrade.git._hostile_post import (
    G,
    H,
    SLOW_WEB,
    WEB_STARTED,
    _clear_toggles,
    _facts,
    _kill_born_after,
    _namespace,
    _publish_web_release,
    _read,
    _toggle,
    _update_log,
    contextlib,
    json,
    re,
    w as w,
)
from tests.e2e.core.upgrade.git._hostile_diagnostics import (
    hostile_cell as hostile_cell,
    hostile_diagnostics as hostile_diagnostics,
)

pytestmark = C.pytestmark


def test_sigkill_during_post_commit_tail_leaves_a_running_receipt(w):
    w.reset_clean()
    _clear_toggles(w)
    prev = w.receipt()
    target = _publish_web_release(w, "c")
    log0 = len(_read(_update_log(w)))
    started = w.sb.home / WEB_STARTED
    _toggle(w, SLOW_WEB, True)
    killed: list[int] = []
    try:
        with _namespace(w) as host:
            before = {p["pid"] for p in host.procs()}
            spawn_log = w.sb.root / "update-c.log"  # sandbox-writable
            upd = host.spawn([w.sb.hermes, "update", "--yes", "--branch", "main", "--no-gateway-restart"],
                             log=spawn_log)

            def _slow_build_running():
                if started.is_file():
                    return True
                if not host.alive(upd):
                    raise AssertionError(f"premise: the update exited before the slow web build started:\n"
                                         f"{_read(spawn_log)[-6000:]}\n{w.diag()}")
                return False

            H.wait_for(_slow_build_running, timeout=1200, interval=1.0, what="the slow web build to start")
            with contextlib.suppress(AssertionError):
                H.wait_for(lambda: re.search(r"web UI|slow web build", _read(_update_log(w))[log0:]),
                           timeout=30, interval=0.5, what="update.log to show the web build")
            log_shows_build = bool(re.search(r"web UI|slow web build", _read(_update_log(w))[log0:]))
            killed = _kill_born_after(host, before)
            rec = w.receipt()
            facts = _facts(w, killed=killed, update_spawn_pid=upd, log_shows_web_build=log_shows_build,
                           previous_update_id=prev.get("update_id"))
            diag = facts + w.diag() + "\n--- spawned update output ---\n" + _read(spawn_log)[-6000:]
    finally:
        _toggle(w, SLOW_WEB, False)
        started.unlink(missing_ok=True)

    assert killed, f"premise: nothing was killed:\n{diag}"
    assert w.head() == target, f"premise: the kill landed before the release committed:\n{diag}"
    assert rec.get("outcome") == "running" and rec.get("update_id") and \
        rec.get("update_id") != prev.get("update_id"), \
        f"latest.json is not a `running` record of the killed update:\n{json.dumps(rec, default=str)[:3000]}\n{diag}"
    assert rec.get("pid") in killed, f"the running receipt names pid {rec.get('pid')}, not a killed process:\n{diag}"
    killed_id = rec["update_id"]

    cp = w.update("--no-gateway-restart")
    rec = w.receipt()
    diag = _facts(w, cp, killed_update_id=killed_id) + w.diag(cp)
    assert "interrupted" in G.output(cp), f"the next update did not report the interrupted run:\n{diag}"
    assert cp.returncode == 0, f"the update after the interrupted one failed:\n{diag}"
    assert rec.get("outcome") == "success" and rec.get("update_id") != killed_id, \
        f"latest.json is not this run's terminal success:\n{diag}"
