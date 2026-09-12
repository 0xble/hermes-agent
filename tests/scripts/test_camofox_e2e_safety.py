"""Exercise live-harness safety paths with disposable state and offline API stubs."""
import json
import subprocess
import sys
from unittest.mock import Mock

import pytest

from scripts import verify_camofox_named_identities_e2e as harness


def test_optimized_restart_guard_refuses_before_launchctl():
    program = """
from scripts import verify_camofox_named_identities_e2e as h
h.camofox_api = lambda *a, **k: {'tabs': [{'userId': 'unrelated'}]}
h.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(Exception('launchctl reached'))
try:
    h.restart_service('synthetic', {'test'})
except RuntimeError as e:
    assert 'refusing' in str(e)
else:
    raise Exception('guard skipped')
"""
    run = subprocess.run([sys.executable, "-O", "-c", program], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr


def test_cleanup_attempts_every_early_bound_identity_and_retains_receipt(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    server = Mock()
    calls = []
    monkeypatch.setattr("tools.browser_camofox_state.read_camofox_binding", lambda task: {"user_id": task})
    def api(method, path, key):
        calls.append(path)
        if "first" in path:
            raise RuntimeError("synthetic sensitive diagnostic must not be persisted")
        return {}
    monkeypatch.setattr(harness, "camofox_api", api)
    with pytest.raises(RuntimeError, match="cleanup incomplete"):
        harness.cleanup_synthetic_state("secret", home, ["first", "second"], set(), server)
    assert calls == ["/sessions/first/storage_state", "/sessions/second/storage_state"]
    receipt = json.loads((home / "cleanup-recovery.json").read_text())
    assert receipt == {"success": False, "unresolved_user_ids": ["first"], "unreadable_task_ids": []}
    server.shutdown.assert_called_once()
    server.server_close.assert_called_once()


def test_clean_cleanup_removes_home_only_after_all_deletes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("tools.browser_camofox_state.read_camofox_binding", lambda _: None)
    calls = []
    def api(*args):
        assert home.exists()
        calls.append(args)
        return {}
    monkeypatch.setattr(harness, "camofox_api", api)
    harness.cleanup_synthetic_state("secret", home, [], {"one", "two"}, Mock())
    assert len(calls) == 2
    assert not home.exists()
