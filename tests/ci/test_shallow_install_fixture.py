"""The shallow-install split selects one original world per bounded runner file."""
from types import SimpleNamespace

import pytest

from tests.e2e.core.upgrade.git import _shallow_install as cell
from tests.e2e.core.upgrade.git import test_shallow_install as depth1
from tests.e2e.core.upgrade.git import test_shallow_install_checked as checked
from tests.e2e.core.upgrade.git import test_shallow_install_prefetched as prefetched


@pytest.mark.parametrize("module", [depth1, checked, prefetched])
def test_module_fixture_selects_only_its_world_and_closes_it(module, tmp_path_factory, request, monkeypatch):
    built, closed, dumps = [], [], []
    class WorldContext:
        def __exit__(self, *args):
            closed.append(module.WORLD)
    def build(name):
        def one(root):
            built.append(name)
            return {"cm": WorldContext(), "world": name}
        return one
    for name in ("depth1", "checked", "prefetched"):
        monkeypatch.setattr(cell, "_" + name, build(name))
    monkeypatch.setattr(cell.faulthandler, "dump_traceback_later", lambda seconds, **kw: dumps.append(seconds))
    monkeypatch.setattr(cell.faulthandler, "cancel_dump_traceback_later", lambda: dumps.append("cancelled"))
    selected = SimpleNamespace(module=module, config=request.config)
    fixture = cell.run.__wrapped__(selected, tmp_path_factory)
    assert next(fixture)["world"] == module.WORLD
    assert built == [module.WORLD]
    assert closed == []
    with pytest.raises(StopIteration):
        next(fixture)
    assert closed == [module.WORLD]
    assert dumps == [840, "cancelled"]
    assert module.run is cell.run


def test_four_original_assertions_are_split_across_three_files():
    counts = [sum(name.startswith("test_") for name in vars(module)) for module in (depth1, checked, prefetched)]
    assert counts == [2, 1, 1]
