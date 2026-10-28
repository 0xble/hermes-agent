"""The retained native browser package keeps its installer identity and explicit opt-out."""
from __future__ import annotations

import argparse
import json

import pm
import pm.cli
from pm import paths
from pm.lock import Lockfile
from tests.pm.test_install_default_closure import _bare_install, install_spy as install_spy  # noqa: PLC0414


def test_native_browser_package_and_chromium_dependency_are_pinned():
    package = pm.get_package("agent-browser")
    lock = Lockfile(paths.lockfile_path())
    assert package.default and package.optional
    assert package.deps == ("chromium",)
    assert lock.version(package.name) is not None
    from pm.store import current_target
    target = current_target()
    if package.missing_reason(target) is None:
        assert lock.artifacts(package.name, target)


def test_native_browser_opt_out_and_opt_back_in_keep_the_same_package(install_spy):
    from pm.defaults import declined, declined_path
    from pm.defaults import canonical_default_name

    assert canonical_default_name("agent-browser") == "agent-browser"
    assert "agent-browser" not in _bare_install(install_spy, without=["agent-browser"])
    assert declined() == frozenset({"agent-browser"})
    assert json.loads(declined_path().read_text(encoding="utf-8"))["declined"] == ["agent-browser"]
    install_spy["names"] = None
    assert pm.cli.cmd_install(argparse.Namespace(names=["agent-browser"], tools_only=False)) == 0
    assert install_spy["names"] == ["agent-browser"]
    assert declined() == frozenset()
    assert "agent-browser" in _bare_install(install_spy)
