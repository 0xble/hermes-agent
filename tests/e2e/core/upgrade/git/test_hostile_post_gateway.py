"""Hostile post: owed gateway restart.

See ``_hostile_post`` for the shared harness and full cell contract.
The module-scoped real install is shared only within this bounded file.
"""
from __future__ import annotations

from tests.e2e.core.upgrade.git import _hostile_post as C
from tests.e2e.core.upgrade.git._hostile_post import (
    BREAK_GATEWAY,
    G,
    H,
    OWED_RESTART,
    _clear_toggles,
    _facts,
    _followup_steps,
    _gateway_logs,
    _identify,
    _namespace,
    _ns_cli,
    _ns_update,
    _obligation,
    _publish_gateway_fault_release,
    _serving,
    _toggle,
    _warn_lines,
    contextlib,
    signal,
    w as w,
)
from tests.e2e.core.upgrade.git._hostile_diagnostics import (
    hostile_cell as hostile_cell,
    hostile_diagnostics as hostile_diagnostics,
)

pytestmark = C.pytestmark


def test_gateway_that_cannot_reboot_is_an_owed_restart_not_a_failed_update(w):
    """Fault: the RELEASE's gateway cannot boot (``run_gateway`` exits 1 while the toggle exists). The
    old gateway drains/stops normally; the relaunch runs the new code and dies at boot, so no gateway
    serves the new commit. A SIGSTOPped old gateway would not do: the survivor sweep SIGKILLs it and
    the relaunch then succeeds."""
    w.reset_clean()
    _clear_toggles(w)
    with _namespace(w) as host:
        try:
            gw = host.spawn([w.sb.hermes, "gateway", "run"], log=w.sb.root / "gateway-b.log")  # sandbox-writable
            before_sha = w.head()
            try:
                ident = H.wait_for(lambda: _serving(w, before_sha), timeout=240, interval=1.0,
                                   what="the cron-only gateway's control-socket identify")
            except AssertionError as exc:
                raise AssertionError(f"premise: the gateway never came up: {exc}\n{_gateway_logs(w)[-4000:]}\n"
                                     f"{host.ps_text()}") from None
            target = _publish_gateway_fault_release(w, "b")
            _toggle(w, BREAK_GATEWAY, True)
            cp = _ns_update(host, w)
            out = G.output(cp)
            rec = w.receipt()
            obligation_after_update = _obligation(w)
            status = _ns_cli(host, w, "status", timeout=300)
            facts = _facts(w, cp, gateway_before=f"pid {ident.get('pid')} sha {before_sha[:12]} (spawned {gw})",
                           identify_after=_identify(w), obligation_after_update=obligation_after_update)
            diag = (facts + w.diag(cp) + "\n--- gateway logs ---\n" + _gateway_logs(w)[-6000:]
                    + "\n--- sandbox ---\n" + host.ps_text() + "\n--- hermes status ---\n" + G.output(status)[-3000:])

            assert w.head() == target, f"premise: the release never committed:\n{diag}"
            assert not _serving(w, target), f"premise: a gateway serves the new commit; the restart fault never fired:\n{diag}"
            assert cp.returncode == 0, f"the code committed, yet a failed gateway restart failed `hermes update`:\n{diag}"
            assert _warn_lines(out), f"the failed gateway restart was not surfaced with a ⚠ line:\n{diag}"
            assert rec.get("outcome") == "success", f"receipt outcome is not success after the commit:\n{diag}"
            assert "gateway_restart" in _followup_steps(rec), \
                f"receipt followups do not name the failed `gateway_restart` step:\n{diag}"
            assert OWED_RESTART in G.output(status), \
                f"a later command does not warn that a gateway restart is still owed:\n{diag}"

            # The fault clears; the next plain update retries the owed restart. The gateway was a
            # manual `gateway run` with no supervisor, but the pre-swap pause recorded its argv, so the
            # retry relaunches it on HEAD. The obligation may discharge only because a gateway now
            # serves HEAD, never while none does.
            _toggle(w, BREAK_GATEWAY, False)
            cp = _ns_update(host, w)
            served = None
            with contextlib.suppress(AssertionError):
                served = H.wait_for(lambda: _serving(w, target), timeout=240, interval=1.0, what="a gateway on HEAD")
            status = _ns_cli(host, w, "status", timeout=300)
            diag = (_facts(w, cp, identify_after=_identify(w)) + w.diag(cp) + "\n--- gateway logs ---\n"
                    + _gateway_logs(w)[-6000:] + "\n--- sandbox ---\n" + host.ps_text()
                    + "\n--- hermes status ---\n" + G.output(status)[-3000:])
            assert cp.returncode == 0, f"the retrying update failed:\n{diag}"
            assert served, f"the retry did not relaunch the paused gateway on {target[:12]}:\n{diag}"
            assert OWED_RESTART not in G.output(status), \
                f"the owed-restart warning survived a gateway serving HEAD:\n{diag}"
        finally:
            _toggle(w, BREAK_GATEWAY, False)
            for p in host.procs():
                if p["pid"] > 2 and "gateway" in p["cmdline"]:
                    host.kill(p["pid"], signal.SIGKILL)  # windows-footgun: ok - linux-only (bwrap) module
    # The namespace (and its gateways) is gone; its PID file names a PID of a dead namespace.
    (w.sb.hermes_home / "gateway.pid").unlink(missing_ok=True)
