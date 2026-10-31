"""Terminal PATH composition is cached per process (pm.install.cached_store_path_dirs).

Every terminal command composes its PATH through it. Recomposing per command (store-root
resolution, facts reads, Chromium's whole-tree rglob) was 23% of all GIL-held time on the live
gateway, which also stretched SQLite writer-lock holds queued behind the GIL.
"""
from __future__ import annotations

import pytest

import pm.install
from pm.filesystem import durable_write_bytes


@pytest.fixture(autouse=True)
def _fixed_cache_clock(monkeypatch):
    # These counts assume one TTL bucket. Even adjacent calls can straddle a real rollover.
    # The expiry test below advances its own clock to exercise invalidation.
    monkeypatch.setattr(pm.install, "_clock", lambda: 1000.0)


def _counting_compositions(monkeypatch):
    calls = []
    real = pm.install._lockfile

    def _lockfile():
        calls.append(1)
        return real()

    monkeypatch.setattr(pm.install, "_lockfile", _lockfile)
    return calls


def test_repeated_terminal_commands_compose_the_store_path_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    calls = _counting_compositions(monkeypatch)

    first = pm.install.cached_store_path_dirs()
    for _ in range(5):
        assert pm.install.cached_store_path_dirs() == first

    assert len(calls) == 1


def test_a_pm_write_or_a_new_runtime_dir_recomposes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    calls = _counting_compositions(monkeypatch)

    pm.install.cached_store_path_dirs()
    durable_write_bytes(tmp_path / "runtime" / "facts.json", b'{"schema": 1, "packages": {}}\n')
    pm.install.cached_store_path_dirs()
    assert len(calls) == 2

    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "other-runtime"))
    pm.install.cached_store_path_dirs()
    assert len(calls) == 3


def test_the_cache_expires_so_another_process_install_is_seen(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    calls = _counting_compositions(monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(pm.install, "_clock", lambda: now[0])

    pm.install.cached_store_path_dirs()
    now[0] += pm.install._STORE_PATH_DIRS_TTL_S
    pm.install.cached_store_path_dirs()
    assert len(calls) == 2


def test_another_process_install_shows_on_the_next_command(tmp_path, monkeypatch):
    """An agent's ``hermes pm install X`` runs in a child process; its next command must find X."""
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(runtime))
    calls = _counting_compositions(monkeypatch)

    pm.install.cached_store_path_dirs()
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "facts.json").write_text('{"schema": 1, "packages": {}}\n')  # not a PM write here
    pm.install.cached_store_path_dirs()
    assert len(calls) == 2


def test_terminal_run_envs_share_one_composition(tmp_path, monkeypatch):
    from tools.environments.local import _user_tools_first

    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    calls = _counting_compositions(monkeypatch)

    assert _user_tools_first("/usr/bin") == _user_tools_first("/usr/bin")
    assert len(calls) == 1


def test_concurrent_terminal_run_envs_share_one_composition(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from tools.environments.local import _user_tools_first

    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    calls = _counting_compositions(monkeypatch)
    ready = Barrier(8, timeout=10)

    def compose(_):
        ready.wait()
        return _user_tools_first("/usr/bin")

    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(compose, range(8)))

    assert paths == [paths[0]] * 8
    assert len(calls) == 1


def test_callers_cannot_mutate_the_cached_list(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(pm.install, "_store_path_dirs", lambda: ["/store/bin"])

    pm.install.cached_store_path_dirs().append("/mutated")
    assert pm.install.cached_store_path_dirs() == ["/store/bin"]


def test_activation_still_composes_live(tmp_path, monkeypatch):
    """Startup activation reports out-of-band damage, so it never reads the terminal cache."""
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    calls = _counting_compositions(monkeypatch)

    pm.install.cached_store_path_dirs()
    pm.install.store_first_path("/usr/bin")
    pm.install.store_first_path("/usr/bin")
    assert len(calls) == 3


def test_reset_tolerates_a_pm_install_import_still_in_progress(monkeypatch):
    """Hosted shards failed with "partially initialized module 'pm.install' has no attribute
    '_store_path_dirs_cache'" when a prior test's thread was mid-import at the next setup."""
    import sys
    import types

    from tests.conftest import reset_pm_path_cache

    monkeypatch.setitem(sys.modules, "pm.install", types.ModuleType("pm.install"))
    reset_pm_path_cache()
