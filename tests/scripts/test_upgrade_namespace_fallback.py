"""Keep shared-runner signal injection inside the fixture HOME without bubblewrap."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import TYPE_CHECKING, Iterator

import pytest

if TYPE_CHECKING:
    from tests.e2e.core.upgrade.handoff._nshost import NamespaceHost

pytestmark = pytest.mark.platforms("linux")


@contextmanager
def _host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[NamespaceHost]:
    from tests.e2e.core.upgrade import _helpers as H
    from tests.e2e.core.upgrade.handoff._nshost import NamespaceHost

    monkeypatch.setattr(H, "BWRAP_OK", False)
    monkeypatch.setenv("CI", "1")
    root = tmp_path / "machine"
    root.mkdir()
    home = root / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "HERMES_HOME": str(home / ".hermes")}
    host = NamespaceHost(root, env)
    try:
        yield host
    finally:
        assert host.close() == []


def test_fallback_neither_exposes_nor_signals_another_fixture(tmp_path, monkeypatch):
    sibling = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"],
        env={**os.environ, "HOME": str(tmp_path / "machine" / "home-other")},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with _host(tmp_path, monkeypatch) as host:
            owned = host.spawn([sys.executable, "-c", "import time; time.sleep(300)"],
                               log=host.root / "owned.log")
            rows = host.procs()
            assert owned in {row["pid"] for row in rows}
            assert sibling.pid not in {row["pid"] for row in rows}
            assert not host.kill(sibling.pid, signal.SIGKILL)
            assert sibling.poll() is None
            assert host.kill(owned, signal.SIGKILL)
        assert sibling.poll() is None
    finally:
        sibling.terminate()
        sibling.wait(timeout=10)


def test_fallback_close_reaps_a_detached_completion_child(tmp_path, monkeypatch):
    from tests.e2e.core.dashboard import _reaper

    with _host(tmp_path, monkeypatch) as host:
        result = host.run([
            sys.executable, "-c",
            "import subprocess,sys; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)'], "
            "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL, "
            "stderr=subprocess.DEVNULL); print(child.pid)",
        ])
        assert result.returncode == 0, result.stderr
        detached = int(result.stdout.strip())
        assert host.alive(detached)
    assert _reaper.start_time(detached) is None
