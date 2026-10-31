"""A failed home probe is retryable in the cached backend (regression for #913)."""
import subprocess

from tests.tools.test_named_node_execution import call, peers  # noqa: F401
from tools.environments.ssh import SSHEnvironment
from tools import terminal_tool as tt

import pytest

pytestmark = pytest.mark.platforms("posix")


def test_failed_home_detection_recovers_without_replacing_cached_node(peers, monkeypatch):
    roots, *_ = peers
    original = SSHEnvironment._run_ssh
    unavailable = [True]
    probes = []

    def transient_home(self, command, timeout):
        if command == "echo $HOME":
            probes.append(self.host)
            if unavailable[0]:
                raise subprocess.TimeoutExpired("fixture home probe", timeout)
        return original(self, command, timeout)

    monkeypatch.setattr(SSHEnvironment, "_run_ssh", transient_home)
    for _ in range(2):
        refused = call("read_file", {"node": "macstudio", "path": "~/document.txt"})
        assert "home is unavailable" in refused.get("error", ""), refused
    cached = next(iter(tt._active_environments.values()))
    assert cached._remote_home_detected is False
    unavailable[0] = False
    (roots["macstudio"] / "home/document.txt").write_text("selected target")
    recovered = call("read_file", {"node": "macstudio", "path": "~/document.txt"})
    assert "selected target" in recovered.get("content", ""), recovered
    assert next(iter(tt._active_environments.values())) is cached
    assert cached._remote_home == str(roots["macstudio"] / "home")
    assert len(probes) >= 3 and set(probes) == {"macstudio"}
