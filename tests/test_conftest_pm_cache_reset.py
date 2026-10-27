"""The shared test fixture clears pm.install's PATH caches only when they exist.

A test module whose import chain is still executing ``pm.install`` leaves the module in
``sys.modules`` before its caches are defined. The autouse fixture must not fail on it
(hosted runs 40450773041 and others: ``partially initialized module 'pm.install' has no
attribute '_store_path_dirs_cache'``).
"""
import sys
import types

from tests.conftest import clear_pm_install_path_caches


def test_partially_initialized_pm_install_is_tolerated(monkeypatch):
    monkeypatch.setitem(sys.modules, "pm.install", types.ModuleType("pm.install"))
    clear_pm_install_path_caches()


def test_both_path_caches_are_cleared(monkeypatch):
    module = types.SimpleNamespace(_store_path_dirs_cache={("k",): ["/bin"]},
                                   _store_roots_cache={("k",): ("/store",)})
    monkeypatch.setitem(sys.modules, "pm.install", module)
    clear_pm_install_path_caches()
    assert module._store_path_dirs_cache == {}
    assert module._store_roots_cache == {}


def test_absent_pm_install_is_a_no_op(monkeypatch):
    monkeypatch.delitem(sys.modules, "pm.install", raising=False)
    clear_pm_install_path_caches()
