"""Physical mutation locks are shared without sharing session read authority."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.tools.test_named_node_execution import call, peers  # noqa: F401
from tools.file_operations import ShellFileOperations

pytestmark = pytest.mark.platforms("posix")


def test_same_remote_file_edits_from_distinct_sessions_do_not_lose_updates(peers, monkeypatch):
    roots, path, *_ = peers
    target = path + "/same.txt"
    first_ready, release_first, second_write, second_attempt = (threading.Event() for _ in range(4))
    import time
    from tools import file_state
    registry = file_state.get_registry()
    blocked, elapsed = [], []

    class ObservedLock:
        def __init__(self, lock):
            self.lock = lock

        def acquire(self):
            if threading.current_thread().name.startswith("second-editor"):
                started = time.monotonic()
                acquired = self.lock.acquire(False)
                blocked.append(not acquired)
                second_attempt.set()
                if not acquired:
                    self.lock.acquire()
                elapsed.append(time.monotonic() - started)
            else:
                self.lock.acquire()

        def release(self):
            self.lock.release()

    class ObservedLocks(dict):
        def setdefault(self, key, default=None):
            return super().setdefault(key, ObservedLock(default))

    monkeypatch.setattr(registry, "_path_locks", ObservedLocks(registry._path_locks))
    original = ShellFileOperations._atomic_write

    def controlled_write(self, destination, content):
        if threading.current_thread().name.startswith("first-editor"):
            first_ready.set()
            assert release_first.wait(10), "first writer was not released"
        else:
            second_write.set()
        return original(self, destination, content)

    monkeypatch.setattr(ShellFileOperations, "_atomic_write", controlled_write)
    with ThreadPoolExecutor(1, thread_name_prefix="first-editor") as first_pool, ThreadPoolExecutor(1, thread_name_prefix="second-editor") as second_pool:
        first = first_pool.submit(call, "patch", {"node": "macstudio", "path": target,
                                 "old_string": "macstudio", "new_string": "first"}, task="task-a", session="chat-a")
        assert first_ready.wait(10)
        second = second_pool.submit(call, "patch", {"node": "macstudio", "path": target,
                                   "old_string": "second", "new_string": "second-edited"}, task="task-b", session="chat-b")
        try:
            assert second_attempt.wait(10), "second writer did not reach the lock"
            assert blocked == [True], "second writer did not contend on the first writer's physical mutex"
            assert not second_write.wait(2), "another session entered this file's mutation while its lock was held"
        finally:
            release_first.set()
        assert not first.result(timeout=15).get("error")
        assert not second.result(timeout=15).get("error")
    assert elapsed[0] >= 2, "serialization interval started before the actual lock attempt"
    assert (roots["macstudio"] / "same.txt").read_text() == "first\nsecond-edited\n"
    assert (roots["macbook"] / "same.txt").read_text() == "macbook\nsecond\n"


def test_lock_sharing_does_not_share_read_authority(peers):
    _, path, *_ = peers
    target = path + "/same.txt"
    assert "content" in call("read_file", {"node": "macstudio", "path": target}, task="a", session="one")
    refused = call("write_file", {"node": "macstudio", "path": target, "content": "unauthorized overwrite"},
                   task="b", session="two")
    assert refused.get("stale_write_blocked"), refused


def test_connection_aliases_share_locks_but_other_nodes_do_not(peers):
    import json
    from tools.node_execution import _execution, Execution
    from tools.nodes import Node
    from tools import file_state

    _, path, *_, home = peers
    nodes = [Node("original", "same-ssh", "macos"), Node("renamed", "same-ssh", "macos"),
             Node("other", "different-ssh", "macos")]
    entered, release = threading.Event(), threading.Event()

    def lock(node, task, ready=None):
        token = _execution.set(Execution(node, "node:" + task, task))
        try:
            with file_state.lock_path(path + "/same.txt"):
                if ready is not None:
                    ready.set()
                if node == nodes[0]:
                    assert release.wait(10)
        finally:
            _execution.reset(token)

    with ThreadPoolExecutor(3) as pool:
        first = pool.submit(lock, nodes[0], "a", entered)
        assert entered.wait(5)
        alias_entered, other_entered = threading.Event(), threading.Event()
        alias = pool.submit(lock, nodes[1], "b", alias_entered)
        other = pool.submit(lock, nodes[2], "c", other_entered)
        try:
            assert other_entered.wait(5), "unrelated nodes were serialized"
            assert not alias_entered.wait(0.3), "same connection was separated by session or registry label"
        finally:
            release.set()
        first.result(timeout=5)
        alias.result(timeout=5)
        other.result(timeout=5)
    assert file_state.get_registry()._path_locks == {}
