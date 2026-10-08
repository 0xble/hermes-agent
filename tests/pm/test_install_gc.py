"""Regression coverage for dependency-install orphan collection."""
from __future__ import annotations

import json
import os
from pathlib import Path

from pm.environments import install_key, record_install_use
from pm.install_gc import collect_install_orphans


def _setup(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _age(path: Path, timestamp: float) -> None:
    os.utime(path, (timestamp, timestamp))


def test_orphan_install_is_removed_after_grace(monkeypatch, tmp_path):
    import time

    home = _setup(monkeypatch, tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    state = home / "installs" / install_key(checkout)
    record_install_use(checkout)
    old = time.time() - 8 * 24 * 60 * 60
    _age(state / "install.json", old)
    checkout.rmdir()

    removed = collect_install_orphans(now=time.time())

    assert removed == [state]
    assert not state.exists()


def test_live_checkout_keeps_install(monkeypatch, tmp_path):
    import time

    home = _setup(monkeypatch, tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    state = home / "installs" / install_key(checkout)
    record_install_use(checkout)
    _age(state / "install.json", time.time() - 8 * 24 * 60 * 60)

    assert collect_install_orphans(now=time.time()) == []
    assert state.is_dir()


def test_leased_generation_keeps_orphan_install(monkeypatch, tmp_path):
    import time

    from hermes_cli.runtime_state import lease_directory

    home = _setup(monkeypatch, tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    state = home / "installs" / install_key(checkout)
    record_install_use(checkout)
    generation = state / "environments" / "generation"
    generation.mkdir(parents=True)
    (generation / ".lease-managed").touch()
    release = lease_directory(generation)
    _age(state / "install.json", time.time() - 8 * 24 * 60 * 60)
    checkout.rmdir()
    try:
        assert collect_install_orphans(now=time.time()) == []
        assert state.is_dir()
    finally:
        release()


def test_non_object_metadata_is_treated_as_legacy_not_a_crash(monkeypatch, tmp_path):
    import time

    home = _setup(monkeypatch, tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    state = home / "installs" / install_key(checkout)
    record_install_use(checkout)
    for payload in ("[]", "null", "3"):
        (state / "install.json").write_text(payload, encoding="utf-8")
        _age(state / "install.json", time.time() - 8 * 24 * 60 * 60)
        # Recent tree activity keeps a legacy install; the point is no exception.
        assert collect_install_orphans(now=time.time()) == []
        assert state.is_dir()


def test_pre_lease_generation_keeps_orphan_install_on_the_legacy_grace(monkeypatch, tmp_path):
    import time

    home = _setup(monkeypatch, tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    state = home / "installs" / install_key(checkout)
    record_install_use(checkout)
    # No .lease-managed marker: lease_directory() hands its readers a no-op lease.
    generation = state / "environments" / "legacy-generation"
    generation.mkdir(parents=True)
    (generation / "python").write_text("", encoding="utf-8")
    now = time.time()
    eight_days = now - 8 * 24 * 60 * 60
    for path in (*state.rglob("*"), state):
        _age(path, eight_days)
    checkout.rmdir()

    assert collect_install_orphans(now=now) == []
    assert state.is_dir()

    forty_days = now - 40 * 24 * 60 * 60
    for path in (*state.rglob("*"), state):
        _age(path, forty_days)
    assert collect_install_orphans(now=now) == [state]


def test_locked_install_keeps_orphan_install(monkeypatch, tmp_path):
    import time

    from pm.filesystem import lock_fd

    home = _setup(monkeypatch, tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    state = home / "installs" / install_key(checkout)
    record_install_use(checkout)
    _age(state / "install.json", time.time() - 8 * 24 * 60 * 60)
    checkout.rmdir()
    from pm.environments import install_state_lock_path
    lock_path = install_state_lock_path(state)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    assert lock_fd(fd, wait=True)
    try:
        assert collect_install_orphans(now=time.time()) == []
        assert state.is_dir()
    finally:
        os.close(fd)


def test_early_recovery_lock_fences_install_gc(monkeypatch, tmp_path):
    """Startup recovery holds the same stable lock as orphan collection."""
    import time

    from hermes_cli._early_recovery import _claim_recovery_lock

    home = _setup(monkeypatch, tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    state = home / "installs" / install_key(checkout)
    record_install_use(checkout)
    _age(state / "install.json", time.time() - 8 * 24 * 60 * 60)
    checkout.rmdir()
    fd = _claim_recovery_lock(checkout)
    assert fd is not None
    try:
        assert collect_install_orphans(now=time.time()) == []
        assert state.is_dir()
    finally:
        os.close(fd)


def test_legacy_install_stays_until_long_threshold(monkeypatch, tmp_path):
    home = _setup(monkeypatch, tmp_path)
    state = home / "installs" / ("a" * 16)
    state.mkdir(parents=True)
    payload = state / "facts.json"
    payload.write_text(json.dumps({"schema": 1, "packages": {}}), encoding="utf-8")
    old = 1_000.0
    _age(payload, old)
    _age(state, old)

    assert collect_install_orphans(now=old + 99, legacy_grace_seconds=100) == []
    assert state.is_dir()
    assert collect_install_orphans(now=old + 101, legacy_grace_seconds=100) == [state]
    assert not state.exists()


def test_corrupt_or_mismatched_metadata_fails_closed(monkeypatch, tmp_path):
    """Unreadable install ownership metadata must never enter legacy deletion."""
    import time

    home = _setup(monkeypatch, tmp_path)
    states = []
    for name, payload in (
        ("corrupt", "{"),
        ("mismatched", json.dumps({"schema": 1, "project_root": str(tmp_path / "other")})),
    ):
        checkout = tmp_path / name
        checkout.mkdir()
        state = home / "installs" / install_key(checkout)
        record_install_use(checkout)
        (state / "install.json").write_text(payload, encoding="utf-8")
        checkout.rmdir()
        old = time.time() - 40 * 24 * 60 * 60
        for path in (*state.rglob("*"), state):
            _age(path, old)
        states.append(state)

    assert collect_install_orphans(now=time.time(), legacy_grace_seconds=100) == []
    assert all(state.is_dir() for state in states)


def test_preparation_lock_fences_install_gc(monkeypatch, tmp_path):
    """GC must not delete an install while PM runtime preparation owns its lock."""
    import threading
    import time

    from pm.environments import install_state_lock
    from pm.filesystem import lock_fd

    home = _setup(monkeypatch, tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    state = home / "installs" / install_key(checkout)
    record_install_use(checkout)
    old = time.time() - 8 * 24 * 60 * 60
    _age(state / "install.json", old)
    checkout.rmdir()
    prepare_lock = state / "pm-runtime" / ".prepare.lock"
    prepare_lock.parent.mkdir(parents=True)
    ready = threading.Event()
    release = threading.Event()

    def prepare():
        with install_state_lock(state) as held:
            assert held
            fd = os.open(prepare_lock, os.O_CREAT | os.O_RDWR, 0o600)
            assert lock_fd(fd, wait=True)
            ready.set()
            assert release.wait(5)
            os.close(fd)

    worker = threading.Thread(target=prepare)
    worker.start()
    assert ready.wait(5)
    try:
        assert collect_install_orphans(now=time.time()) == []
        assert state.is_dir()
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive()


def test_lease_admission_fences_install_gc(monkeypatch, tmp_path):
    """GC must not delete after a reader is admitted to an install generation."""
    import time

    from hermes_cli.runtime_state import lease_generation

    home = _setup(monkeypatch, tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    state = home / "installs" / install_key(checkout)
    record_install_use(checkout)
    generation = state / "environments" / "generation"
    generation.mkdir(parents=True)
    (generation / ".lease-managed").touch()
    (generation / "venv").mkdir()
    _age(state / "install.json", time.time() - 8 * 24 * 60 * 60)
    checkout.rmdir()
    release = lease_generation(generation / "venv")
    try:
        assert collect_install_orphans(now=time.time()) == []
        assert state.is_dir()
    finally:
        release()


def test_lock_timeout_reader_is_fenced_from_install_gc(monkeypatch, tmp_path):
    """A reader that cannot take the install lock must not proceed unprotected."""
    import threading
    import time
    from contextlib import contextmanager

    from hermes_cli import runtime_state
    from pm.environments import runtime_facts_path

    home = _setup(monkeypatch, tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    state = home / "installs" / install_key(checkout)
    record_install_use(checkout)
    generation = state / "environments" / "generation" / "venv"
    generation.mkdir(parents=True)
    (generation.parent / ".lease-managed").touch()
    (generation / "pyvenv.cfg").write_text("version = 3.11.0\n", encoding="utf-8")
    (generation / "lib" / "python3.11" / "site-packages").mkdir(parents=True)
    facts = runtime_facts_path(checkout)
    facts.write_text(json.dumps({"schema": 1, "packages": {"venv": {"environment": str(generation)}}}),
                     encoding="utf-8")
    _age(state / "install.json", time.time() - 8 * 24 * 60 * 60)
    checkout.rmdir()
    timed_out = threading.Event()
    collector_done = threading.Event()

    @contextmanager
    def timed_out_lock(_project):
        timed_out.set()
        yield False

    monkeypatch.setattr(runtime_state, "runtime_lock", timed_out_lock)

    def unprotected_lease(_environment):
        assert timed_out.wait(5)
        assert collect_install_orphans(now=time.time()) == [state]
        collector_done.set()
        return lambda: None

    monkeypatch.setattr(runtime_state, "lease_generation", unprotected_lease)
    from pm.environments import activate_dependencies

    activate_dependencies(checkout)
    assert not collector_done.is_set()
    assert state.is_dir()
