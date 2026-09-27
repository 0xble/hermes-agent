"""Native mutation observers see the locked disk snapshots, never stale store state."""
from contextlib import contextmanager

import pytest

from tools.memory_tool import MemoryStore


def test_observer_sees_actual_serialized_snapshots(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools.memory_transactions import register_memory_transaction_observer
    events = []

    @contextmanager
    def observe(transaction):
        events.append((transaction.before, transaction.after))
        yield

    unregister = register_memory_transaction_observer("test", observe)
    try:
        a, b = MemoryStore(), MemoryStore()
        a.load_from_disk()
        b.load_from_disk()
        a.add("memory", "A")
        b.add("memory", "B")
        b.add("memory", "B")  # no-op
        assert events == [("", "A"), ("A", "A\n§\nB")]
    finally:
        unregister()


def test_observer_prepare_failure_prevents_write(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools.memory_transactions import register_memory_transaction_observer

    @contextmanager
    def refuse(transaction):
        raise OSError("journal unavailable")
        yield

    unregister = register_memory_transaction_observer("test", refuse)
    try:
        with pytest.raises(OSError, match="journal unavailable"):
            MemoryStore().add("memory", "A")
        assert not (tmp_path / "memories/MEMORY.md").exists()
    finally:
        unregister()


def test_restore_compares_under_native_lock_and_preserves_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = MemoryStore()
    store.add("memory", "A")
    store.load_from_disk()
    prompt = dict(store._system_prompt_snapshot)
    store.add("memory", "B")
    assert not store.compare_and_restore("memory", expected="A", replacement="")["success"]
    assert store.compare_and_restore("memory", expected="A\n§\nB", replacement="A")["success"]
    assert (tmp_path / "memories/MEMORY.md").read_text() == "A"
    assert store._system_prompt_snapshot == prompt


def test_observer_registration_is_profile_scoped(tmp_path, monkeypatch):
    from tools.memory_transactions import register_memory_transaction_observer
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "a"))
    seen = []

    @contextmanager
    def observe(transaction):
        seen.append(transaction.after)
        yield

    unregister = register_memory_transaction_observer("test", observe)
    try:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "b"))
        MemoryStore().add("memory", "B")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "a"))
        MemoryStore().add("memory", "A")
        assert seen == ["A"]
    finally:
        unregister()


def test_restore_creates_fresh_profile_and_preserves_write_error_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "fresh"))
    store = MemoryStore()
    assert store.compare_and_restore("memory", expected="", replacement="A")["success"]
    path = tmp_path / "fresh/memories/MEMORY.md"
    assert path.read_text() == "A"

    def fail_write(*args, **kwargs):
        raise OSError("synthetic write failure")

    monkeypatch.setattr("tools.memory_tool_store.atomic_write_text", fail_write)
    with pytest.raises(RuntimeError, match="Failed to write memory file"):
        store.compare_and_restore("memory", expected="A", replacement="B")
    assert path.read_text() == "A"
    assert store.memory_entries == ["A"]
