"""Session publication interleavings, controlled by events rather than timing."""
import queue
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tui_gateway import event_replay, server
from tui_gateway.transport import FanoutTransport


class Peer:
    def __init__(self):
        self.frames = queue.Queue()

    def write(self, obj):
        self.frames.put(obj)
        return True

    def close(self):
        pass


class ObservedLock:
    """Let the test distinguish a blocked publisher from one that stamped early."""
    def __init__(self, observed):
        self.lock = threading.Lock()
        self.observed = observed

    def __enter__(self):
        if self.lock.acquire(blocking=False):
            return self
        if threading.current_thread().name == "publisher-B":
            self.observed.put("waiting")
        self.lock.acquire()
        return self

    def __exit__(self, *_):
        self.lock.release()


def publish(kind, sid, label):
    payload = {"tool_id": label, "name": "manage_connections"}
    if kind == "normal":
        return server.write_json(server._event_frame("tool.start", sid, payload))
    # The real connector projection + captured-owner path, not a transport-only test.
    return server._emit_tool_lifecycle("tool.start", sid, "manage_connections", {}, payload)


@pytest.mark.parametrize("peers", [1, 2])
@pytest.mark.parametrize("emitters", [("normal", "normal"), ("normal", "connector"),
                                     ("connector", "normal"), ("connector", "connector")])
def test_stamp_and_admission_share_one_session_order(monkeypatch, peers, emitters):
    sid = "publication-order"
    clients = [Peer() for _ in range(peers)]
    fan = FanoutTransport(*clients)
    observed = queue.Queue()
    fan._lock = ObservedLock(observed)
    monkeypatch.setattr(server, "_sessions", {sid: {"transport": fan}})
    event_replay.reset_replay_state()
    allocated = threading.Event()
    release = threading.Event()
    stamp = event_replay._stamp_event

    def pause_after_allocation(frame):
        stamp(frame)
        if frame["params"]["payload"]["tool_id"] == "A":
            allocated.set()
            assert release.wait(5)
        else:
            observed.put("stamped")

    monkeypatch.setattr(event_replay, "_stamp_event", pause_after_allocation)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(publish, emitters[0], sid, "A")
        if not allocated.wait(5):
            a.result(timeout=1)
            pytest.fail("publisher did not reach stamping")

        def emit_b():
            threading.current_thread().name = "publisher-B"
            return publish(emitters[1], sid, "B")

        b = pool.submit(emit_b)
        try:
            if observed.get(timeout=5) == "stamped":
                # Baseline: B can finish admission while A is paused after seq 1.
                b.result(timeout=5)
        finally:
            release.set()
        a.result(timeout=5)
        b.result(timeout=5)
    frames = [[client.frames.get(timeout=5) for _ in range(2)] for client in clients]
    assert [[frame["params"]["seq"] for frame in stream] for stream in frames] == [[1, 2]] * peers
    replay = event_replay.events_since(sid, 0)
    assert [event["seq"] for event in replay] == [1, 2]
    assert replay == [frame["params"] for frame in frames[0]]


class GatedPeer(Peer):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.first = True

    def write(self, obj):
        with self.lock:
            self.active += 1
            self.max_active = max(self.active, self.max_active)
            first, self.first = self.first, False
        try:
            if first:
                self.entered.set()
                assert self.release.wait(5)
            return super().write(obj)
        finally:
            with self.lock:
                self.active -= 1


def record(peer, tmp_path):
    token = server.bind_transport(peer)
    try:
        return server._deferred_session_record("publication-key", cols=80, cwd=str(tmp_path),
                                               history=[], lease=None)
    finally:
        server.reset_transport(token)


def test_membership_changes_keep_the_original_single_peer_writer(monkeypatch, tmp_path):
    sid = "publication-membership"
    a, b = GatedPeer(), Peer()
    session = record(a, tmp_path)
    publisher = session["transport"]
    monkeypatch.setattr(server, "_sessions", {sid: session})
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(publish, "normal", sid, "first")
        if not a.entered.wait(5):
            first.result(timeout=1)
            pytest.fail("publisher did not reach the peer")
        try:
            assert server._attach_session_transport(session, b)
            publish("normal", sid, "revoked")
            assert server._detach_session_transport(session, a)
            assert server._attach_session_transport(session, a)
            publish("normal", sid, "rejoined")
        finally:
            a.release.set()
        first.result(timeout=5)
    assert session["transport"] is publisher
    assert a.max_active == 1
    assert [a.frames.get(timeout=5)["params"]["payload"]["tool_id"] for _ in range(2)] == ["first", "rejoined"]
    assert [b.frames.get(timeout=5)["params"]["payload"]["tool_id"] for _ in range(2)] == ["revoked", "rejoined"]
    assert a.frames.empty() and b.frames.empty()


@pytest.mark.parametrize("replacement", ["replace", "finalize"])
def test_connector_rejects_stale_captured_owner_before_replay(monkeypatch, replacement):
    sid = "publication-owner"
    old_peer, new_peer = Peer(), Peer()
    publisher = FanoutTransport(old_peer)
    owner = {"transport": publisher}
    sessions = {sid: owner}
    monkeypatch.setattr(server, "_sessions", sessions)
    event_replay.reset_replay_state()
    observed = queue.Queue()
    publisher._lock = ObservedLock(observed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with publisher._lock:
            def emit():
                threading.current_thread().name = "publisher-B"
                return publish("connector", sid, "authorization-link")

            pending = pool.submit(emit)
            assert observed.get(timeout=5) == "waiting"
            if replacement == "replace":
                sessions[sid] = {"transport": FanoutTransport(new_peer)}
            else:
                owner["_finalized"] = True
        assert pending.result(timeout=5) is False
    assert event_replay.events_since(sid, 0) == []
    assert old_peer.frames.empty() and new_peer.frames.empty()


def test_send_and_reentrant_overflow_close_do_not_hold_publication_lock(monkeypatch):
    sid, other_sid = "publication-overflow", "publication-other"
    close_entered, close_release = threading.Event(), threading.Event()

    class OverflowPeer(GatedPeer):
        def close(self):
            assert not publisher.contains(self)  # re-entrant membership inspection
            close_entered.set()
            assert close_release.wait(5)

    blocked, healthy, other = OverflowPeer(), Peer(), Peer()
    publisher = FanoutTransport(blocked, healthy)
    publisher._MAX_PENDING_FRAMES = 1
    monkeypatch.setattr(server, "_sessions", {
        sid: {"transport": publisher}, other_sid: {"transport": FanoutTransport(other)}})
    publish("normal", sid, "first")
    assert blocked.entered.wait(5)
    assert healthy.frames.get(timeout=5)["params"]["payload"]["tool_id"] == "first"
    publish("normal", sid, "queued")
    assert healthy.frames.get(timeout=5)["params"]["payload"]["tool_id"] == "queued"
    with ThreadPoolExecutor(max_workers=1) as pool:
        overflowing = pool.submit(publish, "normal", sid, "overflow")
        try:
            assert close_entered.wait(5)
            assert healthy.frames.get(timeout=5)["params"]["payload"]["tool_id"] == "overflow"
            publish("normal", sid, "healthy")
            publish("normal", other_sid, "independent")
            assert healthy.frames.get(timeout=5)["params"]["payload"]["tool_id"] == "healthy"
            assert other.frames.get(timeout=5)["params"]["payload"]["tool_id"] == "independent"
        finally:
            close_release.set()
            blocked.release.set()
        overflowing.result(timeout=5)
    assert publisher.flush(timeout=5)
    assert blocked.frames.get(timeout=5)["params"]["payload"]["tool_id"] == "first"
    assert blocked.frames.empty()


@pytest.mark.parametrize("ending", ["close", "reap"])
def test_park_reconnect_and_reclaim_keep_then_release_publisher(monkeypatch, tmp_path, ending):
    import gc
    import weakref

    sid = "publication-parked"
    peer = Peer()
    session = record(peer, tmp_path)
    publisher = session["transport"]
    ref = weakref.ref(publisher)
    session["agent_ready"].set()
    session.update(last_active=0, created_at=0)
    monkeypatch.setattr(server, "_sessions", {sid: session})
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(server, "_session_owns_live_wakeup_schedule", lambda *_: False)
    scheduled, cancelled = [], []
    monkeypatch.setattr(server, "_schedule_ws_orphan_reap", scheduled.append)
    monkeypatch.setattr(server, "_cancel_ws_orphan_reap", cancelled.append)
    monkeypatch.setattr(server, "_reclaim_orphaned_leases", lambda: None)
    monkeypatch.setattr("hermes_cli.mem_trim.trim_memory", lambda **_: None)
    monkeypatch.setattr(server, "_SESSION_TTL_S", 1)
    event_replay.reset_replay_state()
    server._close_sessions_for_transport(peer)
    assert server._ws_session_is_detached(session)
    assert scheduled == [sid]
    assert session["transport"] is publisher
    assert not publish("normal", sid, "parked")
    assert [event["seq"] for event in event_replay.events_since(sid, 0)] == [1]
    reconnected = Peer()
    server._rebind_live_transport(sid, session, reconnected)
    assert cancelled == [sid]
    assert session["transport"] is publisher
    assert not server._ws_session_is_detached(session)
    publish("normal", sid, "reconnected")
    assert reconnected.frames.get(timeout=5)["params"]["seq"] == 2
    assert publisher.flush(timeout=5)
    server._close_sessions_for_transport(reconnected)
    if ending == "close":
        server._close_session_by_id(sid)
    else:
        server._reap_idle_sessions()
    assert sid not in server._sessions
    assert session["_finalized"]
    del publisher, session
    gc.collect()
    assert ref() is None


@pytest.mark.parametrize("mode", ["stdio", "compute", "compute-info-error", "compute-usage-error"])
def test_graceful_exit_drains_final_events_before_completion(monkeypatch, tmp_path, mode):
    import io
    import json

    from tui_gateway.compute_host import ComputeHost
    from tui_gateway.transport import StdioTransport

    class GateStream(io.StringIO):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.first = True

        def write(self, text):
            if not text.strip():
                return super().write(text)
            frame = json.loads(text)
            if self.first and (frame.get("method") == "event" or frame.get("type") == "rpc"):
                self.first = False
                self.entered.set()
                assert self.release.wait(5)
            return super().write(text)

    out = GateStream()
    host = ComputeHost(stdout=out, heartbeat_secs=0) if mode.startswith("compute") else None
    peer = host._transport if host else StdioTransport(lambda: out, threading.Lock())
    session = record(peer, tmp_path)
    sid = "publication-exit"
    monkeypatch.setattr(server, "_sessions", {sid: session})
    monkeypatch.setattr(server, "_get_db", lambda: None)
    publisher = session["transport"]
    flush_entered = threading.Event()
    flush = publisher.flush

    def observe_flush(*args, **kwargs):
        flush_entered.set()
        return flush(*args, **kwargs)

    monkeypatch.setattr(publisher, "flush", observe_flush)
    if host:
        monkeypatch.setattr(server, "_install_borrowed_lease", lambda *a: None)
        def usage(*args):
            if mode == "compute-usage-error":
                raise RuntimeError("usage failed")
            return {}

        def session_info(*args):
            if mode == "compute-info-error":
                raise RuntimeError("session info failed")
            return server._get_usage(None)

        monkeypatch.setattr(server, "_get_usage", usage)
        monkeypatch.setattr(server, "_session_info", session_info)
        monkeypatch.setattr(server, "_persist_branch_seed", lambda *a: None)

        def run_prompt(*args, **kwargs):
            publish("normal", sid, "first")
            publish("normal", sid, "final")

        monkeypatch.setattr(server, "_run_prompt_submit", run_prompt)
        finish = lambda: host._run_real_turn({"sid": sid, "request_id": "turn", "prompt": "hello"})
    else:
        for name in ("_flush_sessions_before_exit", "_release_gateway_wake_owner", "_stop_turns_before_exit"):
            monkeypatch.setattr(server, name, lambda: None)

        def close_session(sid, **kwargs):
            publish("normal", sid, "final")
            server._sessions.pop(sid)

        monkeypatch.setattr(server, "_close_session_by_id", close_session)
        publish("normal", sid, "first")
        finish = server._shutdown_sessions
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            done = pool.submit(finish)
            try:
                assert out.entered.wait(5)
                assert flush_entered.wait(5)
                assert not done.done()
            finally:
                out.release.set()
            done.result(timeout=5)
        frames = [json.loads(line) for line in out.getvalue().splitlines()]
        events = [frame["message"] if frame.get("type") == "rpc" else frame for frame in frames
                  if frame.get("method") == "event" or frame.get("type") == "rpc"]
        assert [event["params"]["payload"]["tool_id"] for event in events] == ["first", "final"]
        assert [event["params"]["seq"] for event in events] == sorted(event["params"]["seq"] for event in events)
        if host:
            assert frames[-1]["type"] == ("turn.error" if mode.endswith("error") else "turn.end")
    finally:
        if host:
            host.close()
