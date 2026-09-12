"""Cross-backend task authority tested without starting any browser."""
import os
import subprocess
import sys

import pytest


IDENTITY = dict(alias="personal", identity_key="key", user_id="user", session_key="session")


def test_process_claim_race_has_exactly_one_backend_winner(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    program = """
import sys
from hermes_cli.browser_identity import BrowserIdentityProcessLock
from tools.browser_tool import _claim_browser_identity_binding
from tools.browser_camofox_state import claim_camofox_binding
try:
    if sys.argv[1] == 'chrome':
        _claim_browser_identity_binding('race', 'personal', 'key')
    else:
        claim_camofox_binding('race', dict(alias='personal', identity_key='key', user_id='user', session_key='session'))
except (RuntimeError, ValueError):
    sys.exit(4)
"""
    children = [subprocess.Popen([sys.executable, "-c", program, backend], env=os.environ.copy(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for backend in ("chrome", "camofox")]
    results = []
    for child in children:
        stdout, stderr = child.communicate(timeout=30)
        results.append(child.returncode)
        assert child.returncode in (0, 4), (stdout, stderr)
    assert sorted(results) == [0, 4]
    assert (tmp_path / "browser-task-locks").is_dir()


@pytest.mark.parametrize("backend", ["chrome", "camofox"])
def test_same_claim_is_idempotent_but_competing_or_corrupt_backend_refuses(tmp_path, monkeypatch, backend):
    from tools import browser_tool as chrome
    from tools import browser_camofox_state as camofox
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    claim = (lambda: chrome._claim_browser_identity_binding("task", "personal", "key")) if backend == "chrome" else (
        lambda: camofox.claim_camofox_binding("task", IDENTITY))
    claim()
    claim()
    opposite = camofox._binding_dir("task") if backend == "chrome" else chrome._browser_identity_binding_dir("task")
    opposite.mkdir(parents=True)
    with pytest.raises((RuntimeError, ValueError)):
        claim()
    # Identical task ids in another home remain independent.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other"))
    claim()


def test_chrome_followup_first_refuses_camofox_before_session_creation(tmp_path, monkeypatch):
    from tools import browser_tool as chrome
    from tools import browser_camofox_state as camofox
    from tools import browser_tool_session as session
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    camofox.claim_camofox_binding("followup", IDENTITY)
    monkeypatch.setattr("hermes_cli.browser_identity.read_browser_identity_config", lambda: {
        "default_identity": "personal", "real_profile_identities": {"personal": {"browser": "chrome", "source_profile": "Default"}}})
    monkeypatch.setattr(chrome, "_start_browser_cleanup_thread", lambda: None)
    monkeypatch.setattr(session, "_create_session_for_key", lambda *a: pytest.fail("created browser"))
    with pytest.raises((RuntimeError, ValueError), match="another backend"):
        chrome._get_session_info("followup")


@pytest.mark.parametrize("warm", [False, True])
def test_camofox_recovered_and_warm_sessions_recheck_opposite_claim(tmp_path, monkeypatch, warm):
    from tools import browser_tool as chrome
    from tools import browser_camofox as camofox
    from tools import browser_camofox_state as state
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(camofox, "_get_camofox_config", lambda: {})
    monkeypatch.setattr(camofox, "_global_user_id_override", lambda _: False)
    monkeypatch.setattr(camofox, "resolve_camofox_identity", lambda *a: IDENTITY)
    monkeypatch.setattr(camofox, "_adopt_existing_tab", lambda _: pytest.fail("attached browser"))
    state.claim_camofox_binding("task", IDENTITY)
    if warm:
        camofox._sessions[camofox._session_cache_key("task")] = dict(named=True, alias="personal", tab_id="tab")
    chrome._browser_identity_binding_dir("task").mkdir(parents=True)
    try:
        with pytest.raises((RuntimeError, ValueError), match="another backend"):
            camofox._get_session("task")
    finally:
        camofox._sessions.pop(camofox._session_cache_key("task"), None)
