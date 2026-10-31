"""Chaos child environments tolerate pytest changing the parent's environment."""

from collections import UserDict
from types import SimpleNamespace

import pytest

from tests.e2e.core.chaos import _helpers


@pytest.mark.parametrize("disappearing", ["HERMES_HONCHO_HOST", "LC_ALL"])
def test_hermetic_env_tolerates_removed_parent_variable(monkeypatch, tmp_path, disappearing):
    class ChangingEnvironment(UserDict):
        def __iter__(self):
            keys = list(self.data)
            self.data.pop(disappearing, None)
            return iter(keys)

    parent = ChangingEnvironment({
        disappearing: "hermes",
        "PATH": "/test/bin",
        "LANG": "",
        "OPENAI_API_KEY": "parent-secret",
        "HERMES_HOME": "/parent/home",
        "PYTEST_CURRENT_TEST": "parent-test",
    })
    monkeypatch.setattr(_helpers, "os", SimpleNamespace(environ=parent))
    home = tmp_path / "home"
    hermes_home = home / ".hermes"

    env = _helpers.hermetic_env(home, hermes_home, "owned-child")

    assert disappearing not in env
    assert env["PATH"] == "/test/bin"
    assert env["LANG"] == ""
    assert env["HOME"] == str(home)
    assert env["HERMES_HOME"] == str(hermes_home)
    assert env["OPENAI_API_KEY"] != "parent-secret"
    assert "PYTEST_CURRENT_TEST" not in env
    assert env["CHAOS_TAG"] == "owned-child"
