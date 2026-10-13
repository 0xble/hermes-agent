"""Hostile post: ordinary post-commit failures.

See ``_hostile_post`` for the shared harness and full cell contract.
The module-scoped real install is shared only within this bounded file.
"""
from __future__ import annotations

from tests.e2e.core.upgrade.git import _hostile_post as C
from tests.e2e.core.upgrade.git._hostile_post import (
    BREAK_DEPS,
    BREAK_WEB,
    DEPS_FAULT_TEXT,
    DEPS_OWED,
    G,
    WEB_FAULT_TEXT,
    _clear_toggles,
    _config_version,
    _edit_config,
    _facts,
    _followup_steps,
    _followups,
    _pending_markers,
    _publish_deps_fault_release,
    _publish_web_release,
    _read,
    _toggle,
    _update_log,
    _warn_lines,
    w as w,
)
from tests.e2e.core.upgrade.git._hostile_diagnostics import (
    hostile_cell as hostile_cell,
    hostile_diagnostics as hostile_diagnostics,
)

pytestmark = C.pytestmark


def test_failed_web_build_after_commit_is_a_followup_not_a_failed_update(w):
    w.reset_clean()
    _clear_toggles(w)
    latest = _config_version(w)
    if latest is None:
        from hermes_cli.config_defaults import DEFAULT_CONFIG

        latest = int(DEFAULT_CONFIG["_config_version"])
    owed = latest - 1
    _edit_config(w, lambda cfg: cfg.update(_config_version=owed))
    target = _publish_web_release(w, "a")
    log0 = len(_read(_update_log(w)))

    _toggle(w, BREAK_WEB, True)
    try:
        cp = w.update("--no-gateway-restart")
    finally:
        _toggle(w, BREAK_WEB, False)
    out = G.output(cp)
    log = _read(_update_log(w))[log0:]
    rec = w.receipt()
    facts = _facts(w, cp, config_version_lowered_to=owed, latest_config_version=latest)
    diag = facts + w.diag(cp)

    assert w.head() == target, f"premise: the release never committed, so this is no post-commit cell:\n{diag}"
    assert WEB_FAULT_TEXT in out + log, f"premise: the hostile web build never ran (web UI not seen stale?):\n{diag}"
    assert cp.returncode == 0, f"the code committed, yet a failed web build failed `hermes update`:\n{diag}"
    assert _warn_lines(out), f"the failed web build was not surfaced with a ⚠ line:\n{diag}"
    assert rec.get("outcome") == "success", f"receipt outcome is not success after the commit:\n{diag}"
    assert "build" in _followup_steps(rec), f"receipt followups do not name the failed `build` step:\n{diag}"
    assert _pending_markers(w), f"the failed build disarmed the source-completion obligation:\n{diag}"
    assert (_config_version(w) or 0) >= latest, (
        f"the failed web build skipped the independent config migration (_config_version still "
        f"{_config_version(w)}, latest {latest}):\n{diag}")

    # The fault clears; the next plain update (no new release) pays the owed tail.
    cp = w.update()
    rec = w.receipt()
    diag = _facts(w, cp) + w.diag(cp)
    assert cp.returncode == 0 and w.head() == target, f"the healing update failed:\n{diag}"
    assert not _followups(rec), f"the healing update still reports followups:\n{diag}"
    assert not _pending_markers(w), f"the healing update left the completion obligation armed:\n{diag}"
    assert (w.checkout / "hermes_cli" / "web_dist" / "index.html").is_file(), \
        f"the healing update did not build the web UI:\n{diag}"


def test_dependency_sync_failure_after_commit_is_a_followup_not_a_failed_update(w):
    w.reset_clean()
    _clear_toggles(w)
    target = _publish_deps_fault_release(w, "e")
    _toggle(w, BREAK_DEPS, True)
    try:
        cp = w.update("--no-gateway-restart")
    finally:
        _toggle(w, BREAK_DEPS, False)
    out = G.output(cp)
    rec = w.receipt()
    diag = _facts(w, cp) + w.diag(cp)

    assert w.head() == target, f"premise: the release never committed, so this is no post-commit cell:\n{diag}"
    assert DEPS_FAULT_TEXT in out, f"premise: the hostile dependency sync never ran:\n{diag}"
    assert cp.returncode == 0, f"the code committed, yet a failed dependency sync failed `hermes update`:\n{diag}"
    assert any(DEPS_OWED in line for line in _warn_lines(out)), \
        f"no ⚠ line says the dependencies are not installed yet:\n{diag}"
    assert rec.get("outcome") == "success", f"receipt outcome is not success after the commit:\n{diag}"
    assert "dependencies" in _followup_steps(rec), f"receipt followups do not name `dependencies`:\n{diag}"
    assert _pending_markers(w), f"the failed dependency sync disarmed the source-completion obligation:\n{diag}"

    # The fault clears; the next plain update completes the owed dependencies and tail.
    cp = w.update("--no-gateway-restart")
    rec = w.receipt()
    diag = _facts(w, cp) + w.diag(cp)
    assert cp.returncode == 0 and w.head() == target, f"the completing update failed:\n{diag}"
    assert not _followups(rec), f"the completing update still reports followups:\n{diag}"
    assert not _pending_markers(w), f"the completing update left the completion obligation armed:\n{diag}"
