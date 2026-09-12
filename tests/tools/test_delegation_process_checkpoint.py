"""A terminal receipt is not proof of completion of its background process."""
import json
import shlex
import sys
import threading
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tools.delegate_tool_checkpoint import checkpoint_child_resume


@pytest.mark.parametrize("boundary", ["running", "unread", "silent_unread", "consumed", "foreign", "exit_transition"])
def test_real_registry_blocks_unsafe_resume_before_advertising(tmp_path, monkeypatch, boundary):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import process_registry as pr
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("child", source="tool", model_config={"_delegation_completed": False})
    paused, release = threading.Event(), threading.Event()
    move = registry._move_to_finished
    def pause_after_exit(session):
        if boundary == "exit_transition":
            assert session.exited
            paused.set()
            assert release.wait(10)
        return move(session)
    monkeypatch.setattr(registry, "_move_to_finished", pause_after_exit)
    duration = 0.05 if boundary == "exit_transition" else 60
    proc = registry.spawn_local(shlex.quote(sys.executable) + f" -c 'import time; time.sleep({duration})'", cwd=str(tmp_path), task_id="terminal-vm", owner_task_id="foreign" if boundary == "foreign" else "child-run")
    proc.notify_on_complete = boundary != "silent_unread"
    try:
        calls = [{"id": "bg", "type": "function", "function": {"name": "terminal", "arguments": '{"background":true}'}}]
        messages = [{"role": "user", "content": "Work"}, {"role": "assistant", "content": "", "tool_calls": calls}, {"role": "tool", "tool_call_id": "bg", "content": json.dumps({"session_id": proc.id, "status": "running"})}]
        for row in messages:
            db.append_message("child", **row)
        child = SimpleNamespace(_session_db=db, session_id="child", _delegation_named_type="advisor", provider="fixture", model="m", _process_owner_task_ids={"child-run"})
        if boundary in {"unread", "silent_unread", "consumed"}:
            registry.kill_process(proc.id, consume_output=False)
            proc.process.wait(timeout=5)
            if proc.notify_on_complete:
                registry.completion_queue.get(timeout=5)
            if boundary == "consumed":
                registry.read_log(proc.id)
        if boundary == "exit_transition":
            assert paused.wait(5)
            # Even a read during this gap cannot establish durable completion.
            registry.read_log(proc.id)
        entry = {"status": "interrupted"}
        checkpoint_child_resume(child, {"messages": messages}, entry, child_task_id="child-run")
        safe = boundary in {"consumed", "foreign"}
        assert entry["resume_available"] is safe
        if not safe:
            assert entry["resume_blocked_reason"] == "unreconciled_background_processes"
        db.close()
        db = SessionDB(db_path=tmp_path / "state.db")
        config = json.loads(db.get_session("child")["model_config"])
        assert config["_delegation_completed"] is safe
        assert bool(db.claim_delegated_resumes(["child"], claim_id="after-restart")) is safe
    finally:
        release.set()
        registry.kill_process(proc.id, consume_output=False)
        proc.process.wait(timeout=5)
        db.close()

@pytest.mark.parametrize("pruning", ["ttl", "capacity"])
@pytest.mark.parametrize("reopen_registry", [False, True])
@pytest.mark.parametrize("observation_proof", [False, "missing", None, "true"])
def test_pruned_unobserved_result_still_fences_reopened_resume(tmp_path, monkeypatch, pruning, reopen_registry, observation_proof):
    import os
    import time
    from tools import process_registry as pr
    from tools import process_registry_results as receipts
    from gateway.session_context import scoped_current_session_id

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("child", source="tool", model_config={"_delegation_completed": False})
    session = pr.ProcessSession(id="proc_fenced", command="fixture completed effect", cwd=str(tmp_path),
        task_id="vm", owner_task_id="child-run", parent_session_id="child", started_at=time.time() - 3600,
        exited=True, exit_code=0, output_buffer="effect complete")
    registry._running[session.id] = session
    registry._move_to_finished(session)
    receipt = tmp_path / "logs" / "process-results" / f"{session.id}.json"
    record = json.loads(receipt.read_text())
    if observation_proof == "missing":
        del record["result_observed"]  # Pre-disposition receipt format.
    else:
        record["result_observed"] = observation_proof
    receipt.write_text(json.dumps(record))
    if pruning == "ttl":
        old = time.time() - receipts.RESULT_RETENTION_SECONDS - 10
        os.utime(receipt, (old, old))
    else:
        session.started_at = time.time()
        for i in range(max(pr.MAX_PROCESSES, receipts.MAX_RETAINED_RESULTS) + 1):
            other = pr.ProcessSession(id=f"proc_other{i}", command="other", cwd=str(tmp_path),
                task_id="vm", owner_task_id="foreign", started_at=time.time() + i, exited=True, exit_code=0,
                _result_observed=True)
            registry._finished[other.id] = other
            receipts.save_completed_result(other)
    assert receipt in receipts._result_paths()
    with registry._lock:
        registry._prune_if_needed()
    assert session.id not in registry._finished
    receipt.write_text(json.dumps(record))  # Keep the base format across restart too.
    if reopen_registry:
        registry = pr.ProcessRegistry()
        monkeypatch.setattr(pr, "process_registry", registry)
    messages = [{"role": "user", "content": "Work"}]
    db.append_message("child", **messages[0])
    def checkpoint():
        child = SimpleNamespace(_session_db=db, session_id="child", _delegation_named_type="advisor",
            provider="fixture", model="m", _process_owner_task_ids={"child-run"})
        entry = {"status": "interrupted"}
        checkpoint_child_resume(child, {"messages": messages}, entry, child_task_id="child-run")
        return entry
    try:
        assert checkpoint()["resume_available"] is False
        db.close()
        db = SessionDB(db_path=tmp_path / "state.db")
        assert not db.claim_delegated_resumes(["child"], claim_id="pruned")
        # Owner-scoped exact retrieval reconciles only this process, across restart.
        with scoped_current_session_id("child"):
            assert registry.read_log("proc_fenced")["output"] == "effect complete"
        assert checkpoint()["resume_available"] is True
        assert not pr.ProcessRegistry().unresolved_owned_processes({"child-run"})
        db.close()
        db = SessionDB(db_path=tmp_path / "state.db")
        assert db.claim_delegated_resumes(["child"], claim_id="observed")
        # Observation releases the durable obligation; ordinary history expiry resumes.
        old = time.time() - receipts.RESULT_RETENTION_SECONDS - 10
        os.utime(receipt, (old, old))
        assert receipt not in receipts._result_paths()
    finally:
        db.close()


@pytest.mark.parametrize("corruption", ["truncated", "non_object", "missing_owner", "wrong_id"])
def test_corrupt_receipt_blocks_resume_and_cannot_expire(tmp_path, monkeypatch, corruption):
    import os
    import time
    from tools import process_registry as pr
    from tools import process_registry_results as receipts

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session = pr.ProcessSession(id="proc_corrupt", command="effect", task_id="vm",
        owner_task_id="child-run", parent_session_id="child", exited=True)
    assert receipts.save_completed_result(session)
    path = tmp_path / "logs" / "process-results" / f"{session.id}.json"
    record = json.loads(path.read_text())
    if corruption == "missing_owner":
        del record["owner_task_id"]
    elif corruption == "wrong_id":
        record["id"] = "proc_foreign"
    raw = "{" if corruption == "truncated" else "[]" if corruption == "non_object" else json.dumps(record)
    path.write_text(raw)
    os.utime(path, (0, 0))
    monkeypatch.setattr(receipts, "MAX_RETAINED_RESULTS", 0)
    registry = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("child", source="tool", model_config={"_delegation_completed": False})
    db.append_message("child", role="user", content="Work")
    child = SimpleNamespace(_session_db=db, session_id="child", _delegation_named_type="advisor",
        provider="fixture", model="m", _process_owner_task_ids={"child-run"})
    try:
        entry = {"status": "interrupted"}
        checkpoint_child_resume(child, {"messages": [{"role": "user", "content": "Work"}]}, entry, child_task_id="child-run")
        assert entry["resume_available"] is False
        assert not db.claim_delegated_resumes(["child"], claim_id="corrupt")
        assert path.read_text() == raw
        # A corrupt neighbour prevents pruning, not publication of an exact
        # fresh receipt. Successful atomic write must not be reported as failure.
        other = pr.ProcessSession(id="proc_other", command="other", owner_task_id="other", exited=True)
        assert receipts.save_completed_result(other)
        assert not other._result_persist_failed
    finally:
        db.close()


def test_newer_observation_receipt_wins_over_stale_fallback_checkpoint(tmp_path, monkeypatch):
    from tools import process_registry as pr
    from tools import process_registry_results as receipts
    import utils

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    checkpoint_path = tmp_path / "processes.json"
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", checkpoint_path)
    registry = pr.ProcessRegistry()
    session = pr.ProcessSession(id="proc_stale", command="effect", owner_task_id="child-run",
        parent_session_id="child", exited=True, output_buffer="exact output")
    registry._running[session.id] = session
    def fail_write(*a, **kw):
        raise OSError("injected write failure")
    with monkeypatch.context() as failed:
        failed.setattr(receipts, "atomic_json_write", fail_write)
        registry._move_to_finished(session)
    fallback = checkpoint_path.read_text()
    with monkeypatch.context() as failed:
        failed.setattr(utils, "atomic_json_write", fail_write)
        assert registry.read_log(session.id)["output"] == "exact output"
    assert checkpoint_path.read_text() == fallback
    registry = pr.ProcessRegistry()
    assert registry.recover_from_checkpoint() == 0
    assert registry.unresolved_owned_processes({"child-run"}) == []
    assert json.loads(checkpoint_path.read_text()) == []
    assert registry.completion_queue.empty()
    assert registry.pending_watchers == []


@pytest.mark.parametrize("raw_owner", ["missing", ""])
@pytest.mark.parametrize("damaged_output", [False, True])
def test_fallback_without_raw_owner_blocks_reopened_resume(tmp_path, monkeypatch, raw_owner, damaged_output):
    from tools import process_registry as pr
    from tools import process_registry_results as receipts
    from gateway.session_context import scoped_current_session_id

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    producer = pr.ProcessRegistry()
    session = pr.ProcessSession(id="proc_unknownowner", command="effect", task_id="default",
        owner_task_id="child-run", parent_session_id="child", exited=True, output_buffer="effect output")
    producer._running[session.id] = session
    def fail_receipt(*args, **kwargs):
        raise OSError("injected receipt publication failure")
    with monkeypatch.context() as failed:
        failed.setattr(receipts, "atomic_json_write", fail_receipt)
        producer._move_to_finished(session)
    assert session._result_persist_failed
    assert not (tmp_path / "logs" / "process-results" / f"{session.id}.json").exists()
    entries = json.loads(pr.CHECKPOINT_PATH.read_text())
    record = entries[0]["completed_result"]
    assert record["owner_task_id"] == "child-run" and record["task_id"] == "default"
    if raw_owner == "missing":
        del record["owner_task_id"]
    else:
        record["owner_task_id"] = raw_owner
    if damaged_output:
        record["output"] = []
    pr.CHECKPOINT_PATH.write_text(json.dumps(entries))
    # A healthy ordinary receipt is still retrievable beside unknown uncertainty.
    good = pr.ProcessSession(id="proc_goodneighbor", command="good", owner_task_id="ordinary-run",
        parent_session_id="ordinary", exited=True, output_buffer="healthy output")
    assert receipts.save_completed_result(good)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("ordinary", source="telegram")
    db.create_session("child", source="tool", model_config={"_delegation_completed": False})
    messages = [{"role": "user", "content": "Work"}]
    db.append_message("child", role="user", content="Work")
    try:
        for attempt in range(2):
            registry = pr.ProcessRegistry()
            monkeypatch.setattr(pr, "process_registry", registry)
            assert registry.recover_from_checkpoint() == 0
            registry._write_checkpoint()  # Preserve uncertainty after later lifecycle writes too.
            assert json.loads(pr.CHECKPOINT_PATH.read_text()) == entries
            with scoped_current_session_id("ordinary"):
                assert registry.read_log(good.id)["output"] == "healthy output"
            with pytest.raises(ValueError, match="owner"):
                registry.unresolved_owned_processes({"child-run"})
            with pytest.raises(ValueError, match="owner"):
                registry.unresolved_owned_processes({"unrelated"})
            child = SimpleNamespace(_session_db=db, session_id="child", _delegation_named_type="advisor",
                provider="fixture", model="m", _process_owner_task_ids={"child-run"})
            entry = {"status": "interrupted"}
            checkpoint_child_resume(child, {"messages": messages}, entry, child_task_id="child-run")
            assert entry["resume_available"] is False
            db.close()
            db = SessionDB(db_path=tmp_path / "state.db")
            assert not db.claim_delegated_resumes(["child"], claim_id=f"unknown-owner-{attempt}")
            assert registry.completion_queue.empty() and registry.pending_watchers == []
    finally:
        db.close()


@pytest.mark.parametrize("metadata", ["root", "compressed", "child", "unknown"])
def test_durable_owner_metadata_bounds_only_ordinary_history(tmp_path, monkeypatch, metadata):
    import os
    import time
    from tools import process_registry as pr
    from tools import process_registry_results as receipts

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    monkeypatch.setattr(receipts, "MAX_RETAINED_RESULTS", 2)
    db = SessionDB(db_path=tmp_path / "state.db")
    parent = "conversation"
    if metadata != "unknown":
        db.create_session(parent, source="subagent" if metadata == "child" else "telegram",
                          model_config={"_delegation_launch": {}} if metadata == "child" else {})
    if metadata == "compressed":
        db.end_session(parent, end_reason="compression")
        db.create_session("tip", source="telegram", parent_session_id=parent)
        parent = "tip"
    registry = pr.ProcessRegistry()
    directory = tmp_path / "logs" / "process-results"
    try:
        for i in range(5):
            session = pr.ProcessSession(id=f"proc_gateway{i}", command="completed", task_id="vm",
                owner_task_id="run", parent_session_id=parent, exited=True, notify_on_complete=True)
            registry._running[session.id] = session
            registry._move_to_finished(session)
            # Queue arrival/drain never claims observation. Root metadata, not
            # delivery, distinguishes bounded history from child obligations.
            assert registry.completion_queue.get_nowait()["session_id"] == session.id
        ordinary = metadata in {"root", "compressed"}
        assert len(list(directory.glob("proc_*.json"))) == (2 if ordinary else 5)
        for path in directory.glob("proc_*.json"):
            assert json.loads(path.read_text())["result_observed"] is False
            old = time.time() - receipts.RESULT_RETENTION_SECONDS - 1
            os.utime(path, (old, old))
        retained = receipts._result_paths()
        assert len(retained) == (0 if ordinary else 5)
        assert len(pr.ProcessRegistry().unresolved_owned_processes({"run"})) == (0 if ordinary else 5)
    finally:
        db.close()


@pytest.mark.parametrize("identity_source", ["receipt", "checkpoint", "unknown"])
def test_corrupt_neighbor_preserves_healthy_history_and_owner_isolation(tmp_path, monkeypatch, identity_source):
    from tools import process_registry as pr
    from tools import process_registry_results as receipts
    from gateway.session_context import scoped_current_session_id

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    bad = pr.ProcessSession(id="proc_broken", command="bad", owner_task_id="bad-owner",
                           parent_session_id="bad-child", exited=True)
    good = pr.ProcessSession(id="proc_healthy", command="good", owner_task_id="good-owner",
                            parent_session_id="good-child", exited=True, output_buffer="healthy output")
    receipts.save_completed_result(bad)
    receipts.save_completed_result(good)
    path = tmp_path / "logs" / "process-results" / "proc_broken.json"
    record = json.loads(path.read_text())
    if identity_source == "receipt":
        del record["output"]
        raw = json.dumps(record)
    else:
        raw = "{"
    if identity_source == "checkpoint":
        pr.CHECKPOINT_PATH.write_text(json.dumps([{"session_id": bad.id, "completed_result": record}]))
    path.write_text(raw)
    registry = pr.ProcessRegistry()
    if identity_source == "checkpoint":
        assert registry.recover_from_checkpoint() == 0
    with scoped_current_session_id("good-child"):
        assert registry.read_log(good.id)["output"] == "healthy output"
    if identity_source == "unknown":
        with pytest.raises(ValueError, match="owner"):
            registry.unresolved_owned_processes({"good-owner"})
    else:
        assert registry.unresolved_owned_processes({"good-owner"}) == []
    with pytest.raises(ValueError):
        registry.unresolved_owned_processes({"bad-owner"})
    assert path.read_text() == raw


@pytest.mark.parametrize("raw_owner", ["missing", "", "child-run"])
def test_corrupt_receipt_localization_never_uses_completed_container_key(tmp_path, monkeypatch, raw_owner):
    from tools import process_registry as pr
    from tools import process_registry_results as receipts
    from gateway.session_context import scoped_current_session_id

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    bad = pr.ProcessSession(id="proc_badmetadata", command="bad", task_id="default",
        owner_task_id="child-run", parent_session_id="child", exited=True)
    record = receipts.completed_result_record(bad)
    if raw_owner == "missing":
        del record["owner_task_id"]
    else:
        record["owner_task_id"] = raw_owner
    pr.CHECKPOINT_PATH.write_text(json.dumps([{"session_id": bad.id, "completed_result": record}]))
    good = pr.ProcessSession(id="proc_intactneighbor", command="good", owner_task_id="ordinary-run",
        parent_session_id="ordinary", exited=True, output_buffer="healthy output")
    assert receipts.save_completed_result(good)
    path = tmp_path / "logs" / "process-results" / f"{bad.id}.json"
    path.write_text("{")
    # No recovery: force the receipt reader itself to consult checkpoint metadata.
    with pytest.raises(ValueError, match="owner"):
        receipts.load_completed_results(unresolved_owners={"child-run"})
    if raw_owner == "child-run":
        assert receipts.load_completed_results(unresolved_owners={"unrelated"}) == {}
    else:
        with pytest.raises(ValueError, match="owner"):
            receipts.load_completed_results(unresolved_owners={"unrelated"})
    with scoped_current_session_id("ordinary"):
        assert pr.ProcessRegistry().read_log(good.id)["output"] == "healthy output"
    assert path.read_text() == "{"


@pytest.mark.parametrize("legacy_live_owner", [False, True])
def test_mixed_checkpoint_preserves_bad_fallback_and_recovers_live_watcher(tmp_path, monkeypatch, legacy_live_owner):
    import os
    from tools import process_registry as pr

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    producer = pr.ProcessRegistry()
    live = pr.ProcessSession(id="proc_live", command="live", pid=os.getpid(), pid_scope="host",
        owner_task_id="live-owner", watcher_interval=20,
        host_start_time=producer._safe_host_start_time(os.getpid()))
    producer._running[live.id] = live
    producer._write_checkpoint()
    invalid = {"session_id": "proc_broken", "completed_result": {"id": "proc_broken", "owner_task_id": "bad-owner"}, "pid": 999999}
    entries = [invalid] + json.loads(pr.CHECKPOINT_PATH.read_text())
    if legacy_live_owner:
        del entries[1]["owner_task_id"]
        entries[1]["task_id"] = "live-owner"
    pr.CHECKPOINT_PATH.write_text(json.dumps(entries))
    registry = pr.ProcessRegistry()
    assert registry.recover_from_checkpoint() == 1
    recovered_live = registry.get(live.id)
    assert recovered_live is not None and recovered_live.detached
    assert recovered_live.owner_task_id == "live-owner"
    assert [w["session_id"] for w in registry.pending_watchers] == [live.id]
    assert registry.completion_queue.empty()
    assert "proc_broken" not in registry._running
    registry._write_checkpoint()  # Not just the first recovery write.
    assert invalid in json.loads(pr.CHECKPOINT_PATH.read_text())
    with pytest.raises(ValueError, match="owner"):
        registry.unresolved_owned_processes({"bad-owner"})
    assert registry.unresolved_owned_processes({"unrelated"}) == []


@pytest.mark.parametrize("boundary", ["exit", "failed_publication_eviction"])
def test_receipt_scan_releases_registry_lock_and_rechecks_exit_boundary(tmp_path, monkeypatch, boundary):
    from concurrent.futures import ThreadPoolExecutor
    from tools import process_registry as pr
    from tools import process_registry_results as receipts

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    registry = pr.ProcessRegistry()
    scanned, release = threading.Event(), threading.Event()
    session = pr.ProcessSession(id="proc_racing", command="effect", owner_task_id="child-run", exited=True)
    if boundary == "failed_publication_eviction":
        session._result_persist_failed = True
        registry._finished[session.id] = session
    real_load = pr.load_completed_results
    scans = 0
    def paused_load(*args, **kwargs):
        nonlocal scans
        results = real_load(*args, **kwargs)
        scans += 1
        if scans == 1:
            scanned.set()
            assert release.wait(5)
        return results
    monkeypatch.setattr(pr, "load_completed_results", paused_load)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(registry.unresolved_owned_processes, {"child-run"})
        try:
            assert scanned.wait(5)
            acquired = registry._lock.acquire(timeout=1)
            assert acquired, "history I/O must not hold the registry lock"
            if acquired:
                registry._lock.release()
            # Spawn, exit and evict during the disk read. A final in-memory-only
            # snapshot would miss this completed effect; generation must retry.
            if boundary == "exit":
                registry._running[session.id] = session
                registry._move_to_finished(session)
                with registry._lock:
                    registry._finished.pop(session.id)
            else:
                with registry._lock:
                    registry._prune_if_needed()
                assert session.id not in registry._finished
        finally:
            release.set()
        assert [s.id for s in result.result(timeout=5)] == ["proc_racing"]
    assert scans >= 2


def test_publication_and_observation_never_scan_history_under_registry_lock(tmp_path, monkeypatch):
    from tools import process_registry as pr
    from tools import process_registry_results as receipts

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    registry = pr.ProcessRegistry()
    real_paths = receipts._result_paths
    calls = []
    def checked_paths():
        assert not registry._lock.locked()
        calls.append(True)
        return real_paths()
    monkeypatch.setattr(receipts, "_result_paths", checked_paths)
    session = pr.ProcessSession(id="proc_locktest", command="effect", owner_task_id="child-run", exited=True)
    registry._running[session.id] = session
    registry._move_to_finished(session)
    registry._observe_completed_result(session, consumed=True)
    with registry._lock:
        registry._prune_if_needed()
    assert len(calls) == 2


@pytest.mark.parametrize("failure_at", ["completion", "observation"])
def test_failed_receipt_successful_checkpoint_recovers_exact_unresolved_effect(tmp_path, monkeypatch, failure_at):
    from tools import process_registry as pr
    from tools import process_registry_results as receipts
    from gateway.session_context import scoped_current_session_id

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    checkpoint_path = tmp_path / "processes.json"
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", checkpoint_path)
    registry = pr.ProcessRegistry()
    session = pr.ProcessSession(id="proc_failedreceipt", command="completed effect", task_id="vm",
        owner_task_id="child-run", parent_session_id="child", exited=True, exit_code=0,
        pid=12345, watcher_interval=1, notify_on_complete=True, started_at=1, output_buffer="exact output")
    registry._running[session.id] = session
    real_write = receipts.atomic_json_write
    def fail_receipt(*args, **kwargs):
        raise OSError("injected receipt disk failure")
    if failure_at == "observation":
        registry._move_to_finished(session)
    monkeypatch.setattr(receipts, "atomic_json_write", fail_receipt)
    if failure_at == "completion":
        registry._move_to_finished(session)
    else:
        registry.read_log(session.id)
    # Real successful checkpoint write must retain the failed publication, even
    # after another unrelated lifecycle write. No in-memory-only fence suffices.
    with registry._lock:
        registry._prune_if_needed()
    assert session.id in registry._finished  # Failed receipt cannot be evicted.
    registry._write_checkpoint()
    assert json.loads(checkpoint_path.read_text())
    for _ in range(2):
        registry = pr.ProcessRegistry()
        monkeypatch.setattr(registry, "_host_pid_is_ours", lambda *a: pytest.fail("must not probe completed PID"))
        assert registry.recover_from_checkpoint() == 0
        with scoped_current_session_id("child"):
            restored = registry.get(session.id)
        assert restored.exited and restored.process is None and restored.pid is None
        assert not registry.has_any_active()
        assert registry.completion_queue.empty()
        assert registry.pending_watchers == []
        assert [s.id for s in registry.unresolved_owned_processes({"child-run"})] == [session.id]
        assert registry.unresolved_owned_processes({"foreign"}) == []
    monkeypatch.setattr(pr, "process_registry", registry)
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("child", source="tool", model_config={"_delegation_completed": False})
    messages = [{"role": "user", "content": "Work"}]
    db.append_message("child", **messages[0])
    child = SimpleNamespace(_session_db=db, session_id="child", _delegation_named_type="advisor",
        provider="fixture", model="m", _process_owner_task_ids={"child-run"})
    def checkpoint():
        entry = {"status": "interrupted"}
        checkpoint_child_resume(child, {"messages": messages}, entry, child_task_id="child-run")
        return entry
    try:
        assert checkpoint()["resume_available"] is False
        assert not db.claim_delegated_resumes(["child"], claim_id="failed-receipt")
        monkeypatch.setattr(receipts, "atomic_json_write", real_write)
        with scoped_current_session_id("child"):
            assert registry.read_log(session.id)["output"] == "exact output"
        assert checkpoint()["resume_available"] is True
        assert json.loads(checkpoint_path.read_text()) == []
        fresh = pr.ProcessRegistry()
        assert fresh.recover_from_checkpoint() == 0
        assert fresh.unresolved_owned_processes({"child-run"}) == []
        assert fresh.completion_queue.empty()
    finally:
        db.close()
