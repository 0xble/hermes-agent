"""Package hooks resolve dependencies from the store being built, then release that scope."""
from pathlib import Path

import pytest

from pm import paths
from pm.build_operations import prepare_tools
from pm.install import _installed_location, _lockfile
from pm.lock import Lockfile
from pm.package import InstallError, Package
from pm.registry import _packages, get_package
from pm.store import current_target
from tests.pm._fixtures import make_tar, served as served


@pytest.fixture
def dependency_build(tmp_path, monkeypatch, served):
    target = current_target()
    ambient = tmp_path / "ambient"
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "user/.hermes"))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(ambient))
    monkeypatch.setattr(paths, "lockfile_path", lambda: tmp_path / "lock.json")

    class Leaf(Package):
        name = "lookup-leaf"

    class Reader(Package):
        name = "lookup-reader"
        deps = ("lookup-leaf",)
        phase = "stage"
        before_lookup = None

        def read_dependency(self, staged, target):
            if self.before_lookup is not None:
                self.before_lookup()
            dependency = get_package("lookup-leaf")
            location = _installed_location(dependency, _lockfile(), target)
            if location is None:
                raise InstallError(self.name, "dependency is absent from the selected store")
            (staged / "dependency-root").write_text(str(location[1].root), encoding="utf-8")
            # An explicit read remains authoritative even while a build is active.
            explicit = _installed_location(dependency, _lockfile(), target, roots=(ambient,))
            if explicit is not None:
                assert explicit[1].root == ambient

        def unpack(self, archive, staged, target):
            super().unpack(archive, staged, target)
            if self.phase == "unpack":
                self.read_dependency(staged, target)

        def stage(self, store, staged, version, target):
            if self.phase == "stage":
                self.read_dependency(staged, target)

    reader = Reader()
    directory, url = served
    lock = Lockfile(paths.lockfile_path())
    for package in (Leaf(), reader):
        monkeypatch.setitem(_packages, package.name, package)
        archive, sha = make_tar(directory, package.name + ".tgz", {"data": package.name})
        lock.set_pin(package.name, "1.0", {target: {"url": url + "/" + archive, "sha256": sha}})
    lock.save()
    return target, ambient, reader


@pytest.mark.parametrize("phase", ["unpack", "stage"])
@pytest.mark.parametrize("warm_ambient", [False, True])
def test_build_dependency_hooks_resolve_their_own_store(tmp_path, dependency_build, phase, warm_ambient):
    target, ambient, reader = dependency_build
    reader.phase = phase
    if warm_ambient:
        prepare_tools(["lookup-leaf"], out=ambient, target=target)
    build = tmp_path / "build"
    prepare_tools([reader.name], out=build, target=target)
    entry = build / reader.store_entry("1.0", target)
    assert Path((entry / "dependency-root").read_text(encoding="utf-8")) == build
    location = _installed_location(get_package("lookup-leaf"), _lockfile(), target)
    assert (location[1].root if location else None) == (ambient if warm_ambient else None)


def test_nested_failed_build_restores_dependency_lookup_scope(tmp_path, dependency_build):
    target, ambient, reader = dependency_build
    prepare_tools(["lookup-leaf"], out=ambient, target=target)
    outer, nested = tmp_path / "outer", tmp_path / "nested"

    def nested_then_fail():
        prepare_tools(["lookup-leaf"], out=nested, target=target)
        location = _installed_location(get_package("lookup-leaf"), _lockfile(), target)
        assert location[1].root == outer
        raise InstallError(reader.name, "injected hook failure")

    reader.before_lookup = nested_then_fail
    with pytest.raises(InstallError, match="injected hook failure"):
        prepare_tools([reader.name], out=outer, target=target)
    location = _installed_location(get_package("lookup-leaf"), _lockfile(), target)
    assert location[1].root == ambient
    reader.before_lookup = None
    prepare_tools([reader.name], out=outer, target=target)
    entry = outer / reader.store_entry("1.0", target)
    assert Path((entry / "dependency-root").read_text(encoding="utf-8")) == outer
