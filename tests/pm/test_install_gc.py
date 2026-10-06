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
    fd = os.open(state / ".install.lock", os.O_CREAT | os.O_RDWR, 0o600)
    assert lock_fd(fd, wait=True)
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
