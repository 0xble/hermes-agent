"""Hostile post: interrupted post-commit tail.

See ``_hostile_post`` for the shared harness and full cell contract.
The module-scoped real install is shared only within this bounded file.
"""
from __future__ import annotations

from tests.e2e.core.upgrade.git import _hostile_post as C
from tests.e2e.core.upgrade.git._hostile_post import (
    H,
    INTERRUPTED_AFTER_COMMIT,
    SLOW_WEB,
    WEB_STARTED,
    _clear_toggles,
    _facts,
    _foreground_group,
    _namespace,
    _pending_markers,
    _publish_web_release,
    _read,
    _toggle,
    signal,
    w as w,
)
from tests.e2e.core.upgrade.git._hostile_diagnostics import (
    hostile_cell as hostile_cell,
    hostile_diagnostics as hostile_diagnostics,
)

pytestmark = C.pytestmark


def test_ctrl_c_after_commit_reports_the_new_code_not_a_failed_update(w):
    w.reset_clean()
    _clear_toggles(w)
    prev = w.receipt()
    target = _publish_web_release(w, "f")
    started = w.sb.home / WEB_STARTED
    _toggle(w, SLOW_WEB, True)
    try:
        with _namespace(w) as host:
            spawn_log = w.sb.root / "update-f.log"  # sandbox-writable
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
            interrupted = _foreground_group(host, upd)
            for pid in interrupted:
                host.kill(pid, signal.SIGINT)
            exited = True
            try:
                H.wait_for(lambda: not host.alive(upd), timeout=120, interval=0.5, what="the interrupted update to exit")
            except AssertionError:
                exited = False
            out = _read(spawn_log)
            rec = w.receipt()
            diag = (_facts(w, sigint=interrupted, update_exited=exited, previous_update_id=prev.get("update_id"))
                    + w.diag() + "\n--- sandbox ---\n" + host.ps_text()
                    + "\n--- interrupted update output ---\n" + out[-6000:])
    finally:
        _toggle(w, SLOW_WEB, False)
        started.unlink(missing_ok=True)

    assert w.head() == target, f"premise: the SIGINT landed before the release committed:\n{diag}"
    assert exited, f"the update did not exit after Ctrl-C:\n{diag}"
    assert INTERRUPTED_AFTER_COMMIT in out, \
        f"after Ctrl-C the user is not told the new code is in place and the rest is owed:\n{diag}"
    assert rec.get("update_id") != prev.get("update_id") and rec.get("outcome") == "interrupted", \
        f"latest.json is not this run's `interrupted` record (a `failed` one reads as the previous version):\n{diag}"
    assert _pending_markers(w), f"the interrupt disarmed the source-completion obligation:\n{diag}"

    cp = w.update("--no-gateway-restart")
    rec = w.receipt()
    diag = _facts(w, cp) + w.diag(cp)
    assert cp.returncode == 0 and rec.get("outcome") == "success", f"the update after Ctrl-C failed:\n{diag}"
    assert not _pending_markers(w), f"the update after Ctrl-C left the completion obligation armed:\n{diag}"
