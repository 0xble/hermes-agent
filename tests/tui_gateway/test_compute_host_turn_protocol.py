"""Turn-protocol coverage for the compute host's REAL (non-seeded) frame path.

``turn.start`` → ``turn.started`` → streamed ``rpc`` event frames (``message.delta``) →
``turn.end`` carrying the session's ``history_version`` / ``message_count`` — the loop at
``ComputeHost._run_real_turn``. The Phase-0 ``session.seed`` / SpikeAgent surface is gone
(no production sender), so this drives the path the dashboard supervisor actually uses:
a ``server._sessions`` entry whose agent runs on the turn worker.
"""

from __future__ import annotations

import io
import json
import threading
import time
import types

import pytest

from tui_gateway import server
from tui_gateway.compute_host import ComputeHost


def _frames(out: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _wait(out: io.StringIO, predicate, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for frame in _frames(out):
            if predicate(frame):
                return frame
        time.sleep(0.01)
    raise AssertionError(f"timed out; saw={_frames(out)}")


@pytest.fixture()
def turn_env(monkeypatch, tmp_path):
    """Neutralize the turn pipeline's environment-heavy side paths (same set as the
    prompt.submit tests) so the frame protocol is what's under test. Threads stay REAL:
    ``_run_real_turn`` joins ``session["_run_thread"]`` itself before emitting ``turn.end``."""
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})


def _agent(deltas: list[str], *, delay_s: float = 0.0, interrupt: threading.Event | None = None):
    def run_conversation(prompt, *, conversation_history=None, stream_callback=None, **_kw):
        chunks = []
        for chunk in deltas:
            if interrupt is not None and interrupt.is_set():
                break
            chunks.append(chunk)
            if stream_callback is not None:
                stream_callback(chunk)
            if delay_s:
                time.sleep(delay_s)
        final = "".join(chunks)
        messages = [*(conversation_history or []), {"role": "user", "content": prompt},
                    {"role": "assistant", "content": final}]
        return {"final_response": final, "messages": messages}

    return types.SimpleNamespace(
        session_id="s1-key", run_conversation=run_conversation, clear_interrupt=lambda: None,
        hard_interrupt=lambda *a, **k: interrupt is not None and interrupt.set())


def _session(agent) -> dict:
    return {
        "agent": agent, "session_key": "s1-key", "history": [], "history_lock": threading.Lock(),
        "history_version": 0, "running": False, "attached_images": [], "image_counter": 0,
        "cols": 80, "slash_worker": None, "show_reasoning": False, "tool_progress_mode": "all",
        "inflight_turn": None, "active_session_lease": object(),
    }


def test_turn_start_streams_deltas_then_turn_end_with_history_identity(turn_env):
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    sid = "s1"
    server._sessions[sid] = _session(_agent(["a ", "b ", "c "]))
    try:
        host.handle_frame({"type": "turn.start", "sid": sid, "request_id": "turn", "prompt": "hello"})
        end = _wait(out, lambda f: f["type"] == "turn.end")
    finally:
        server._sessions.pop(sid, None)
        host.close()

    frames = _frames(out)
    kinds = [f["type"] for f in frames]
    assert kinds[0] == "turn.started"
    assert kinds[-1] == "turn.end"
    started = frames[0]
    assert started["sid"] == sid and started["request_id"] == "turn" and "started_ns" in started

    # Streamed output rides the host transport as ``rpc`` frames tagged with the sid, each an
    # ``event`` JSON-RPC notification the parent forwards to the client verbatim.
    deltas = [f for f in frames if f["type"] == "rpc"
              and f["message"]["method"] == "event" and f["message"]["params"]["type"] == "message.delta"]
    assert [d["message"]["params"]["payload"]["text"] for d in deltas] == ["a ", "b ", "c "]
    assert {d["sid"] for d in deltas} == {sid}
    assert any(f["type"] == "rpc" and f["message"]["params"]["type"] == "message.complete" for f in frames)

    # turn.end carries the transcript identity the parent uses to reconcile its mirror.
    assert end["sid"] == sid and end["request_id"] == "turn"
    assert end["session_key"] == "s1-key"
    assert end["history_version"] == 1
    assert end["message_count"] == 2
    assert end["interrupted"] is False
    assert end["session_info_emitted"] is True and isinstance(end["session_info"], dict)
    assert "ended_ns" in end


def test_turn_start_without_sid_is_a_turn_error(turn_env):
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    try:
        host.handle_frame({"type": "turn.start", "request_id": "nosid", "prompt": "x"})
        err = _wait(out, lambda f: f["type"] == "turn.error")
    finally:
        host.close()
    assert err["request_id"] == "nosid" and err["message"] == "sid required"


def test_second_turn_start_while_running_is_session_busy(turn_env):
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    sid = "s1"
    session = _session(_agent(["x"]))
    session["running"] = True
    server._sessions[sid] = session
    try:
        host.handle_frame({"type": "turn.start", "sid": sid, "request_id": "t2", "prompt": "hi"})
        err = _wait(out, lambda f: f["type"] == "turn.error")
    finally:
        server._sessions.pop(sid, None)
        host.close()
    assert err["request_id"] == "t2" and err["message"] == "session busy"


def test_stale_queued_prompt_generation_ends_turn_as_interrupted(turn_env):
    """A queued prompt whose generation was bumped by an interrupt must not run."""
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    sid = "s1"
    session = _session(_agent(["x"]))
    session["_queued_prompt_generation"] = 3
    server._sessions[sid] = session
    try:
        host.handle_frame({"type": "turn.start", "sid": sid, "request_id": "q", "prompt": "hi",
                           "queued_prompt_generation": 2})
        end = _wait(out, lambda f: f["type"] == "turn.end")
    finally:
        server._sessions.pop(sid, None)
        host.close()
    assert end["interrupted"] is True and end["request_id"] == "q"
    assert not any(f["type"] == "turn.started" for f in _frames(out))


def test_interrupt_frame_acks_and_marks_turn_interrupted(turn_env):
    """The turn runs on the host worker while ``interrupt`` arrives on the control path."""
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    sid = "s1"
    stop = threading.Event()
    server._sessions[sid] = _session(_agent([f"{i:03d} " for i in range(200)], delay_s=0.01, interrupt=stop))
    try:
        host.handle_frame({"type": "turn.start", "sid": sid, "request_id": "turn", "prompt": "go"})
        _wait(out, lambda f: f["type"] == "rpc" and f["message"]["params"]["type"] == "message.delta")
        host.handle_frame({"type": "interrupt", "sid": sid, "request_id": "stop"})
        ack = _wait(out, lambda f: f["type"] == "interrupt.ack")
        end = _wait(out, lambda f: f["type"] == "turn.end")
    finally:
        stop.set()
        server._sessions.pop(sid, None)
        host.close()

    assert ack["applied"] is True and ack["request_id"] == "stop" and "applied_ns" in ack
    assert end["interrupted"] is True
    deltas = sum(1 for f in _frames(out) if f["type"] == "rpc" and f["message"]["params"]["type"] == "message.delta")
    assert 0 < deltas < 200


def test_unknown_frame_type_is_an_error():
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    try:
        host.handle_frame({"type": "bogus", "request_id": "b"})
    finally:
        host.close()
    assert _frames(out) == [{"type": "error", "request_id": "b", "message": "unknown frame type: bogus",
                             "host_ns": _frames(out)[0]["host_ns"]}]


@pytest.mark.parametrize("kind", ["legacy", "hard-only", "dynamic-getattr"])
def test_compute_host_interrupt_uses_explicit_stop_compatibility(monkeypatch, kind):
    """Ported from the seeded-session version: the ``interrupt`` frame reaches the live
    agent through ``server._interrupt_session_turn`` → ``request_hard_interrupt``, which must
    prefer a real ``hard_interrupt`` but never trust one fabricated by ``__getattr__``."""
    calls = []

    class _Legacy:
        def interrupt(self):
            calls.append("legacy")

    class _HardOnly:
        def hard_interrupt(self):
            calls.append("hard")

    class _Dynamic:
        def interrupt(self):
            calls.append("legacy")

        def __getattr__(self, name):
            if name == "hard_interrupt":
                return lambda: calls.append("fabricated-hard")
            raise AttributeError(name)

    agent = {"legacy": _Legacy(), "hard-only": _HardOnly(), "dynamic-getattr": _Dynamic()}[kind]
    # The child never routes back to a supervisor (HERMES_COMPUTE_HOST_CHILD=1 in production).
    monkeypatch.setenv("HERMES_COMPUTE_HOST_CHILD", "1")
    out = io.StringIO()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    sid = "s1"
    session = _session(agent)
    session["running"] = True
    server._sessions[sid] = session
    try:
        host._handle_interrupt({"sid": sid, "request_id": "stop"})
    finally:
        server._sessions.pop(sid, None)
        host.close()

    assert calls == ["hard" if kind == "hard-only" else "legacy"]
    ack = _frames(out)[-1]
    assert ack["type"] == "interrupt.ack" and ack["applied"] is True
    assert session["_turn_cancel_requested"] is True


def test_host_builds_the_session_agent_with_the_frame_login(monkeypatch):
    """The host process has no record for a first turn, and its pipe names no login, so the agent is built
    from the login the frame carries and the new record keeps it for later rebuilds."""
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    captured = {}

    def fake_make_agent(sid, key, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(session_id=key)

    def fake_init_session(sid, key, agent, history, **kwargs):
        monkeypatch.setitem(server._sessions, sid, {"agent": agent, "session_key": key})

    monkeypatch.setattr(server, "_make_agent", fake_make_agent)
    monkeypatch.setattr(server, "_transfer_db_to_agent", lambda agent, db: False)
    monkeypatch.setattr(server, "_init_session", fake_init_session)
    server._sessions.pop("s-login", None)

    session = host._build_server_session(
        server, {"sid": "s-login", "session_key": "login-key", "history": [], "auth_user_id": "basic:alice"},
        "s-login")

    assert captured["auth_user_id"] == "basic:alice"
    assert session["auth_user_id"] == "basic:alice"
    assert server._session_auth_user_id(session) == "basic:alice"


@pytest.mark.parametrize("dependency", ["version", "tools"])
def test_interrupted_turn_settles_while_cold_metadata_is_blocked(turn_env, monkeypatch, dependency):
    """Cold discovery cannot gate settlement, but final-event delivery still must."""
    import builtins
    import queue

    from hermes_cli import banner, version_info
    from tools.registry import registry

    progress = queue.Queue()
    provider_started, stop = threading.Event(), threading.Event()
    metadata_entered, release_metadata = threading.Event(), threading.Event()
    release_peer, ended = threading.Event(), threading.Event()
    identity = version_info.VersionInfo("1.2.3", "1.2.3+4.gabcdef0", 4, "abcdef0", "main", "git")

    def block_metadata():
        metadata_entered.set()
        if threading.current_thread() is session.get("_run_thread"):
            progress.put("metadata-blocked-turn")
        assert release_metadata.wait(10), "cold metadata was never released"

    monkeypatch.setattr(version_info, "_cached_version_info", None if dependency == "version" else identity)
    monkeypatch.setattr(version_info, "_stamp_version_info", lambda: None)
    monkeypatch.setattr(version_info, "_resolve_repo_dir", lambda: ".")

    probes = []

    def slow_git(_repo):
        probes.append(True)
        block_metadata()
        return identity

    monkeypatch.setattr(version_info, "_git_version_info", slow_git)
    monkeypatch.setattr(banner, "get_available_skills", dict)
    monkeypatch.setattr(banner, "get_update_result", lambda **_: None)
    monkeypatch.setattr(registry, "get_toolset_for_tool", lambda _: "registered-tools")
    original_import = builtins.__import__

    def cold_import(name, globals=None, locals=None, fromlist=(), level=0):
        if dependency == "tools" and name == "model_tools" and "get_toolset_for_tool" in fromlist:
            block_metadata()
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", cold_import)

    class FinalEventStream(io.StringIO):
        def write(self, text):
            if text.strip():
                frame = json.loads(text)
                if frame.get("type") == "rpc" and frame["message"]["params"]["type"] == "session.info":
                    progress.put("final-event-ready")
                    assert release_peer.wait(10), "final-event peer was never released"
                result = super().write(text)
                if frame.get("type") == "turn.end":
                    ended.set()
                return result
            return super().write(text)

    def provider(*args, **kwargs):
        provider_started.set()
        assert stop.wait(10), "interrupt did not reach provider"
        return {"final_response": "interrupted", "interrupted": True}

    agent = types.SimpleNamespace(session_id="cold-metadata-key", run_conversation=provider,
                                  clear_interrupt=lambda: None, hard_interrupt=stop.set,
                                  tools=[{"function": {"name": "registered-tool"}}])
    sid = "cold-metadata"
    session = _session(agent)
    monkeypatch.setitem(server._sessions, sid, session)
    out = FinalEventStream()
    host = ComputeHost(stdout=out, heartbeat_secs=0)
    try:
        host.handle_frame({"type": "turn.start", "sid": sid, "request_id": "turn", "prompt": "go"})
        assert provider_started.wait(5)
        host.handle_frame({"type": "interrupt", "sid": sid, "request_id": "stop"})
        assert any(f["type"] == "interrupt.ack" and f["applied"] for f in _frames(out))
        assert progress.get(timeout=5) == "final-event-ready", "cold metadata blocked the interrupted worker"
        assert session["running"] is False
        assert not ended.is_set(), "turn.end overtook the blocked final event"
        release_peer.set()
        assert ended.wait(5), "turn.end still depended on cold metadata"
        frames = _frames(out)
        assert frames[-1]["type"] == "turn.end"
        assert frames[-1]["interrupted"] is True
        infos = [f["message"]["params"]["payload"] for f in frames
                 if f["type"] == "rpc" and f["message"]["params"]["type"] == "session.info"]
        assert infos[-1]["tools"] == {"registered-tools": ["registered-tool"]}
        if dependency == "version":
            assert metadata_entered.wait(5)
            assert not release_metadata.is_set()
            assert infos[-1]["version"] == ""
            assert probes == [True], "cold snapshots started competing version resolvers"
    finally:
        stop.set()
        release_metadata.set()
        release_peer.set()
        host._executor.shutdown(wait=True)
        prefetch = getattr(version_info, "_version_info_prefetch_thread", None)
        if prefetch is not None:
            prefetch.join(timeout=5)
            assert not prefetch.is_alive()
        host.close()
    assert server._session_info(agent, session)["version"] == "1.2.3"
