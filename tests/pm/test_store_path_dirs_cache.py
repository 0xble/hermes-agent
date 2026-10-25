"""Terminal PATH composition is cached per process (pm.install.cached_store_path_dirs).

Every terminal command composes its PATH through it. Recomposing per command (store-root
resolution, facts reads, Chromium's whole-tree rglob) was 23% of all GIL-held time on the live
gateway, which also stretched SQLite writer-lock holds queued behind the GIL.
"""
from __future__ import annotations

import pm.install
from pm.filesystem import durable_write_bytes


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
    monkeypatch.setattr("time.monotonic", lambda: now[0])

    pm.install.cached_store_path_dirs()
    now[0] += pm.install._STORE_PATH_DIRS_TTL_S
    pm.install.cached_store_path_dirs()
    assert len(calls) == 2


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
