"""Runtime-state isolation must survive tests clearing the complete environment."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tests import conftest
from gateway import status


def test_cleared_environment_cannot_overwrite_default_runtime_state(tmp_path, monkeypatch):
    account_home = tmp_path / "account"
    default_home = account_home / ".hermes"
    default_home.mkdir(parents=True)
    sentinel = default_home / "gateway_state.json"
    sentinel.write_text('{"pid": "sentinel"}')
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: account_home))
    monkeypatch.setattr(conftest, "_GATEWAY_RUNTIME_DENY_ROOTS", (default_home,), raising=False)
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(pytest.fail.Exception, match="gateway runtime isolation"):
            status.write_runtime_status(gateway_state="running")
    assert sentinel.read_text() == '{"pid": "sentinel"}'


def test_runtime_state_remains_writable_in_explicit_test_home(tmp_path, monkeypatch):
    home = tmp_path / "explicit-profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    status.write_runtime_status(gateway_state="running")
    record = status.read_runtime_status()
    assert record is not None
    assert record["gateway_state"] == "running"
    assert record["pid"] == os.getpid()
    assert (home / "gateway_state.json").is_file()
