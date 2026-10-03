"""Forward-only readiness requires a real, isolated, no-tool runner reply."""
import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.outbox import Outbox, recover
from gateway.session import SessionSource


def test_synthetic_reply_is_terminal_hidden_from_current_status_and_never_recovered(tmp_path):
    store = Outbox(tmp_path)
    row = store.enqueue_synthetic("nonce", {"chat_id": "loopback:nonce", "content": "nonce"})
    assert row.state == "failed_unsent"
    with sqlite3.connect(store.path) as db:
        state, status, attempts = db.execute("SELECT state,send_status,attempts FROM outbox").fetchone()
    assert (state, status, attempts) == ("failed_unsent", "synthetic", 0)
    assert store.pending() == store.scheduled() == store.ambiguous() == store.status() == []
    promoted = Outbox(tmp_path)
    adapter = AsyncMock()
    adapter.gateway_runner = None
    assert asyncio.run(recover(promoted, adapter)) == (0, 0)
    adapter.send.assert_not_called()
    assert not promoted.begin_send(row)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE outbox SET created_at=0")
    promoted.prune()
    assert promoted.all_rows() == []


@pytest.mark.asyncio
async def test_overlap_off_is_dormant(tmp_path):
    from gateway.startup_gate import run_startup_gate
    runner = type("Runner", (), {"config": GatewayConfig()})()
    verdict = await run_startup_gate(runner.config)
    assert verdict.verdict == "disabled"
    assert not verdict.ready
    assert verdict.evidence == {}


def test_wire_identity_cannot_grant_gate_privileges():
    from gateway.startup_gate import gate_for_source, LOOPBACK_PLATFORM
    source = SessionSource(platform=Platform(LOOPBACK_PLATFORM), chat_id="loopback:nonce", user_id="startup-gate")
    source._startup_gate = {"nonce": "nonce"}
    assert gate_for_source(source) is None
    assert gate_for_source(SessionSource.from_dict(source.to_dict())) is None

def install_provider_stub(mode="pass", calls=None, entered_path=None, forbidden_path=None):
    """Stub only physical model I/O, including in the disposable worker."""
    import httpx
    import re
    import time

    def send(_self, request, **kwargs):
        if not request.url.path.endswith("/chat/completions"):
            return httpx.Response(404, request=request)
        payload = json.loads(request.content)
        if calls is not None:
            calls.append(payload)
        token = re.search(r"nothing else: HERMES_READY ([\w-]+)", payload["messages"][-1]["content"])[1]
        reply = "HERMES_READY " + token
        if mode == "wrapped":
            reply = '  **`hermes_ready`**. "' + token + '!"  '
        elif mode == "wrong-nonce":
            reply = "HERMES_READY wrong-nonce"
        elif mode == "missing-nonce":
            reply = "HERMES_READY"
        if entered_path is not None:
            Path(entered_path).write_text("provider call entered", encoding="utf-8")
        if mode == "timeout":
            time.sleep(60)
        if mode == "shared-write":
            Path(forbidden_path).write_text("must be refused", encoding="utf-8")
        if mode == "exception":
            raise httpx.ConnectError("fixture provider unavailable", request=request)
        message = {"role": "assistant", "content": "" if mode == "empty" else reply}
        if mode == "tool":
            message["tool_calls"] = [{"id": "call_fixture", "type": "function", "function": {
                "name": "terminal", "arguments": '{"command":"touch forbidden"}'}}]
        base = {"id": "fixture-completion", "created": 1, "model": payload["model"]}
        finish = "tool_calls" if mode in {"tool", "tool-signal"} else "stop"
        if payload.get("stream"):
            delta = {k: v for k, v in message.items() if k != "role"}
            if mode == "tool":
                delta["tool_calls"][0]["index"] = 0
            chunks = [{**base, "object": "chat.completion.chunk", "choices": [{
                "index": 0, "delta": delta, "finish_reason": None}]},
                {**base, "object": "chat.completion.chunk", "choices": [{
                    "index": 0, "delta": {}, "finish_reason": finish}]}]
            body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, request=request, content=body,
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(200, request=request, json={**base, "object": "chat.completion", "choices": [{
            "index": 0, "message": message, "finish_reason": finish}]})
    httpx.Client.send = send


def forbid_worker_startup_effects():
    from gateway.run import GatewayRunner
    def forbidden(self):
        raise AssertionError("startup gate must not install software")
    GatewayRunner._init_startup_checks = forbidden
    GatewayRunner._get_proxy_url = lambda self: "remote-proxy"
    original_wire = GatewayRunner._wire_adapter_handlers
    def wire(self, adapter):
        assert self.hooks.loaded_hooks == []
        assert self._get_proxy_url() is None
        assert self._resolve_turn_toolsets({}, adapter._startup_gate_turn.source, 'local') == ([], None)
        return original_wire(self, adapter)
    GatewayRunner._wire_adapter_handlers = wire


@pytest.fixture
def model_io(monkeypatch):
    import httpx
    calls = []
    monkeypatch.setattr(httpx.Client, "send", httpx.Client.send)
    install_provider_stub(calls=calls)
    return calls


@pytest.fixture
def isolated_runner(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    home = tmp_path / "sandbox"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    token = set_hermes_home_override(home)
    cfg = GatewayConfig(sessions_dir=home / "sessions")
    cfg.streaming.enabled = False
    runner = GatewayRunner(cfg)
    yield runner
    runner.session_store.close_all_db_handles()
    reset_hermes_home_override(token)


def make_turn(tmp_path):
    from gateway.startup_gate import _LoopbackTurn
    turn = _LoopbackTurn("gate-nonce", "gate-fixture", {
        "provider": "custom", "requested_provider": "custom", "api_key": "fixture-only",
        "base_url": "http://127.0.0.1:9/v1", "api_mode": "chat_completions",
    }, tmp_path / "profile")
    turn.evidence["session_key"] = "startup-gate:" + turn.nonce
    return turn


@pytest.mark.parametrize("reply,completed,accepted", [
    ("HERMES_READY gate-nonce", True, True),
    ('  **`hermes_ready`**. "gate-nonce!"  ', True, True),
    ('"HERMES_READY!" gate-nonce', True, True),
    ("HERMES_READY gate-nonce extra", True, False),
    ("HERMES_UNRELATED gate-nonce", True, False),
    ("HERMES_READY wrong-nonce", True, False),
    ("HERMES_READY", True, False),
    ("gate-nonce", True, False),
    ("HERMES_READY gate-nonce", False, False),
    ("HERMES_READY gate-nonce", None, False),
])
def test_ready_requires_normalized_token_exact_nonce_and_completed_model_turn(tmp_path, reply, completed, accepted):
    from gateway.startup_gate import record_model_result
    turn = make_turn(tmp_path)
    result = {"final_response": reply, "messages": [], "completed": completed, "api_calls": 1}
    if accepted:
        record_model_result(turn, result)
        assert turn.evidence["model_reply"] and turn.evidence["turn_completed"]
    else:
        with pytest.raises(RuntimeError):
            record_model_result(turn, result)
        assert not turn.evidence.get("model_reply")


@pytest.mark.asyncio
async def test_real_runner_reply_and_explicit_zero_tools(isolated_runner, model_io, tmp_path):
    from gateway.startup_gate import _run_loopback
    turn = make_turn(tmp_path)
    evidence = await _run_loopback(isolated_runner, turn)
    assert evidence.get("runner_output"), evidence
    assert evidence["adapter_guard"] and evidence["runner_guard"] and evidence["zero_tools"]
    assert evidence["session_key"] == "startup-gate:" + turn.nonce
    assert len(model_io) == 1
    assert model_io[0]["model"] == turn.model
    assert not model_io[0].get("tools") and not model_io[0].get("functions")
    roles = [m["role"] for m in model_io[0]["messages"]]
    assert roles[-1] == "user" and all(a != b for a, b in zip(roles, roles[1:]))
    assert not turn.active
    assert not turn.adapter._background_tasks
    assert not isolated_runner._agent_cache
    assert not isolated_runner._running_agents
    rows = Outbox(turn.outbox_home).all_rows()
    assert len(rows) == 1 and rows[0].payload["content"] == "HERMES_READY " + turn.nonce


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [None, "gate-nonce"])
async def test_admission_or_adapter_text_is_not_runner_output(isolated_runner, model_io, tmp_path, reply):
    from gateway.startup_gate import _run_loopback
    isolated_runner._handle_message = AsyncMock(return_value=reply)
    turn = make_turn(tmp_path)
    evidence = await _run_loopback(isolated_runner, turn)
    assert evidence["admitted"]
    assert not evidence.get("runner_output")
    assert model_io == []
    assert Outbox(turn.outbox_home).all_rows() == []



def test_reserved_session_cannot_be_addressed_by_wire_source(tmp_path):
    from gateway.session import build_session_key
    from gateway.startup_gate import _LoopbackAdapter, gate_for_source
    import weakref
    turn = make_turn(tmp_path)
    adapter = _LoopbackAdapter(turn)
    source = SessionSource(platform=Platform.LOCAL, chat_id="loopback:" + turn.nonce,
                           user_id="startup-gate", chat_type="dm")
    source._transport_adapter_ref = weakref.ref(adapter)
    source._startup_gate_capability = turn
    turn.source = source
    assert build_session_key(source) == "startup-gate:" + turn.nonce
    wire = SessionSource.from_dict(source.to_dict())
    assert gate_for_source(wire) is None
    assert build_session_key(wire).startswith("agent:")
    assert build_session_key(wire) != build_session_key(source)
    # Even a copied transport reference cannot issue a second privileged source.
    wire._transport_adapter_ref = source._transport_adapter_ref
    assert gate_for_source(wire) is None
    turn.active = False
    assert build_session_key(source).startswith("agent:")


@pytest.fixture
def worker_provider(tmp_path, monkeypatch):
    """Launch the real disposable worker with fake SDK I/O, never a fake turn."""
    import sys
    real_spawn = asyncio.create_subprocess_exec
    modes = ["pass"]
    worker_homes = []
    spawned = []

    async def spawn(*args, **kwargs):
        worker_homes.append(Path(kwargs["env"]["HERMES_HOME"]))
        code = (
            "import sys,runpy; sys.path.insert(0," + repr(str(Path(__file__).parent)) + "); "
            "from test_startup_gate import install_provider_stub, forbid_worker_startup_effects; "
            "forbid_worker_startup_effects(); install_provider_stub(" + repr(modes[0]) +
            ",entered_path=" + repr(str(Path(kwargs["env"]["HERMES_HOME"]) / "provider-entered")) +
            ",forbidden_path=" + repr(str(tmp_path / "owner" / "forbidden")) + "); "
            "runpy.run_module('gateway.startup_gate',run_name='__main__')"
        )
        with (tmp_path / "worker-stderr").open("wb") as diagnostics:
            kwargs["stderr"] = diagnostics
            proc = await real_spawn(sys.executable, "-c", code, **kwargs)
        spawned.append(proc)
        async def observe_provider_entry():
            while proc.returncode is None:
                if (worker_homes[-1] / "provider-entered").exists():
                    runner.provider_entered = True
                    return
                await asyncio.sleep(0.02)
        asyncio.create_task(observe_provider_entry())
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    home = tmp_path / "owner"
    home.mkdir()
    # Disposable fixture config, not the operator's installed config.
    config = {"model": {"default": "gate-fixture", "provider": "custom",
                         "base_url": "http://127.0.0.1:9/v1", "api_key": "fixture-only", "api_mode": "chat_completions"},
              "gateway": {"overlap_handover": {"enabled": True}}}
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        sessions_dir=home / "sessions", overlap_handover_enabled=True)

    def write_config(deadline=45):
        import hermes_yaml as yaml
        if deadline is None:
            config["gateway"]["overlap_handover"].pop("startup_gate_timeout_seconds", None)
        else:
            config["gateway"]["overlap_handover"]["startup_gate_timeout_seconds"] = deadline
        (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    monkeypatch.setenv("HERMES_HOME", str(home))
    home_token = set_hermes_home_override(home)
    write_config(None)
    runner.provider_entered = False
    runner.user_config = config
    yield runner, modes, worker_homes, spawned, write_config
    reset_hermes_home_override(home_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["pass", "wrapped", "wrong-nonce", "missing-nonce", "empty", "tool", "tool-signal", "exception", "shared-write", "schema-override"])
@pytest.mark.live_system_guard_bypass  # Deadline may signal only this fixture's private worker group.
async def test_verdict_uses_real_worker_turn_and_is_private(worker_provider, mode):
    from gateway.startup_gate import run_startup_gate
    runner, modes, homes, spawned, write_config = worker_provider
    modes[0] = mode
    if mode == "schema-override":
        runner.user_config["model"]["provider"] = "custom:fixture"
        runner.user_config["custom_providers"] = [{
            "name": "fixture", "api_key": "fixture-only", "base_url": "http://127.0.0.1:9/v1",
            "extra_body": {"tools": [{"type": "function", "function": {
                "name": "terminal", "parameters": {"type": "object", "properties": {}}}}]},
        }]
        write_config(None)
    verdict = await run_startup_gate(runner.config)
    assert verdict.ready == (mode in {"pass", "wrapped"}), verdict.evidence_text
    assert verdict.verdict == ("passed" if mode in {"pass", "wrapped"} else "failed")
    assert verdict.elapsed_seconds > 0
    assert verdict.evidence["deadline_seconds"] == 45
    if mode == "schema-override":
        assert not spawned and not homes  # Reject tools before lending credentials to a worker.
    else:
        assert verdict.evidence["worker_reaped"]
    assert json.loads(verdict.evidence_text)["verdict"] == verdict.verdict
    assert all(not home.exists() for home in homes)
    assert all(proc.returncode is not None for proc in spawned)
    owner_home = runner.config.sessions_dir.parent
    # Config initialization may create an empty sessions directory in the owner.
    assert not runner.config.sessions_dir.exists() or not any(runner.config.sessions_dir.iterdir())
    assert not (owner_home / "state.db").exists()
    assert not (owner_home / "MEMORY.md").exists()
    assert not (owner_home / "forbidden").exists()
    # Normal owner-side resolution may seed SOUL/config backups, as an ordinary turn does.
    rows = Outbox(owner_home).all_rows()
    if mode in {"pass", "wrapped"}:
        assert verdict.evidence["model_reply"] and verdict.evidence["zero_tools"]
        assert len(rows) == 1 and rows[0].state == "failed_unsent"
        assert rows[0].payload["session_key"].startswith("startup-gate:")
    else:
        if mode in {"wrong-nonce", "missing-nonce"}:
            assert len(rows) == 1
            assert rows[0].state == "failed_unsent" and rows[0].message_id is None
            assert rows[0].payload["session_key"] == "startup-gate:" + verdict.evidence["nonce"]
            with sqlite3.connect(Outbox(owner_home).path) as conn:
                assert conn.execute("SELECT send_status,attempts FROM outbox").fetchall() == [("synthetic", 0)]
            assert Outbox(owner_home).pending() == []
        else:
            assert rows == []
        assert verdict.evidence["error"]
        if mode == "schema-override":
            assert not runner.provider_entered
        if mode in {"tool", "tool-signal"}:
            assert verdict.evidence["tool_attempted"]
            assert "refused" in verdict.evidence["error"]


@pytest.mark.asyncio
@pytest.mark.live_system_guard_bypass  # Only the fixture's private worker is spawned.
async def test_sandbox_rejects_hermes_homes_even_with_owner_tmpdir(worker_provider, monkeypatch, tmp_path):
    import tempfile
    import hermes_constants
    from gateway.startup_gate import run_startup_gate

    runner, _, homes, _, _ = worker_provider
    owner = runner.config.sessions_dir.parent
    scratch = owner / "cache" / "scratch"
    scratch.mkdir(parents=True)
    default = tmp_path / "default-hermes"
    default.mkdir()
    alias = tmp_path / "scratch-alias"
    alias.symlink_to(scratch, target_is_directory=True)
    safe = tmp_path / "safe"
    safe.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: default)
    monkeypatch.setattr(tempfile, "_candidate_tempdir_list", lambda: [str(scratch), str(alias), str(default), str(tmp_path / "missing"), str(safe)])
    monkeypatch.delenv("PREFIX", raising=False)

    verdict = await run_startup_gate(runner.config)
    assert verdict.ready, verdict.evidence_text
    assert len(homes) == 1 and homes[0].parent == safe
    assert all(not home.exists() for home in homes)
    assert len(verdict.evidence["sandbox_candidates"]) == 4
    assert [item["result"] for item in verdict.evidence["sandbox_candidates"]] == [
        "inside Hermes home", "inside Hermes home", "FileNotFoundError", "selected"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "unwritable", "inside-home"])
async def test_unusable_sandbox_root_returns_failed_evidence(worker_provider, monkeypatch, tmp_path, failure):
    import tempfile
    from gateway.startup_gate import run_startup_gate

    runner, _, homes, spawned, _ = worker_provider
    candidate = runner.config.sessions_dir.parent if failure == "inside-home" else tmp_path / "missing"
    monkeypatch.setattr(tempfile, "_candidate_tempdir_list", lambda: [str(candidate)])
    monkeypatch.delenv("PREFIX", raising=False)
    if failure == "unwritable":
        def denied(*args, **kwargs):
            raise PermissionError("fixture sandbox root is unwritable")
        monkeypatch.setattr(tempfile, "TemporaryDirectory", denied)

    verdict = await run_startup_gate(runner.config)
    assert verdict.verdict == "failed" and not verdict.ready
    assert verdict.evidence["error"] == "no usable sandbox root outside Hermes homes"
    result = {"missing": "FileNotFoundError", "unwritable": "PermissionError", "inside-home": "inside Hermes home"}[failure]
    assert verdict.evidence["sandbox_candidates"] == [{"path": str(candidate.resolve()), "result": result}]
    assert not homes and not spawned


@pytest.mark.asyncio
@pytest.mark.live_system_guard_bypass  # Real signals target only the worker this fixture spawned.
async def test_overall_timeout_reaps_private_worker(worker_provider):
    from gateway.startup_gate import run_startup_gate
    runner, modes, homes, spawned, write_config = worker_provider
    modes[0] = "timeout"
    # The overall deadline includes bootstrap as well as a blocked provider.
    # Shorten it through YAML, without relying on how quickly imports run.
    write_config(2)
    verdict = await run_startup_gate(runner.config)
    assert not verdict.ready and verdict.verdict == "failed", verdict.evidence_text
    assert verdict.evidence["deadline_seconds"] == 2
    assert "deadline exceeded" in verdict.evidence["error"]
    assert verdict.evidence["worker_reaped"]
    assert spawned[0].returncode is not None
    assert all(not home.exists() for home in homes)
    assert Outbox(runner.config.sessions_dir.parent).all_rows() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", [0, -1, 46, float("nan"), True])
async def test_invalid_yaml_deadline_fails_before_worker(worker_provider, deadline):
    from gateway.startup_gate import run_startup_gate
    runner, _, _, spawned, write_config = worker_provider
    write_config(deadline)
    verdict = await run_startup_gate(runner.config)
    assert verdict.verdict == "failed" and not verdict.ready
    assert "invalid config.yaml" in verdict.evidence["error"]
    assert spawned == []


def test_previous_release_outbox_reads_writes_and_never_recovers_synthetic(tmp_path):
    import subprocess
    import sys
    release = "738c502c9ba76ce515ebc1abe89d6b925c8b53c9"
    import hashlib
    module = Path(__file__).parent / "fixtures" / "outbox_738c502c.py"
    frozen = module.read_bytes().split(b"\n", 2)[2]
    blob = b"blob " + str(len(frozen)).encode("ascii") + b"\0" + frozen
    assert hashlib.sha1(blob).hexdigest() == "2756ddb96103628a366db3672e91569e1cf0a119"
    # Frozen 738c502c code requires its actual PyYAML pin, which current Hermes
    # replaced with hermes_yaml. Keep that dependency local to this old-code probe.
    legacy_site = tmp_path / "legacy-site"
    subprocess.run(["uv", "pip", "install", "--python", sys.executable,
                    "--target", str(legacy_site), "--no-deps", "pyyaml==6.0.3"],
                   check=True, capture_output=True, text=True, timeout=60)
    store = Outbox(tmp_path / "shared")
    store.enqueue_synthetic("compat", {"content": "compat", "chat_id": "loopback:compat"})
    with store._connect() as db:
        schema = db.execute("SELECT sql FROM sqlite_master WHERE name='outbox'").fetchone()[0]
        rootpage = db.execute("SELECT rootpage FROM sqlite_master WHERE name='outbox'").fetchone()[0]
    code = """
import asyncio, importlib.util, json, sys
from pathlib import Path
from unittest.mock import AsyncMock
sys.path.insert(0, sys.argv[4])
spec = importlib.util.spec_from_file_location('old_release_outbox', sys.argv[1])
old = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = old
spec.loader.exec_module(old)
store = old.Outbox(Path(sys.argv[2]))
assert store.all_rows()[0].state == 'failed_unsent'
assert store.pending() == store.scheduled() == store.ambiguous() == []
adapter = AsyncMock()
adapter.gateway_runner = None
assert asyncio.run(old.recover(store, adapter)) == (0, 0)
adapter.send.assert_not_called()
row = store.enqueue('old-normal', 'send', {'content': 'ordinary'})
assert store.begin_send(row)
store.receipt(row, message_id='old-receipt', success=True)
assert store.all_rows()[1].state == 'delivered'
print(json.dumps({'release': sys.argv[3], 'synthetic_recovered': False, 'old_write': 'delivered'}))
"""
    completed = subprocess.run([sys.executable, "-c", code, str(module), str(store.path.parent), release, str(legacy_site)],
                               capture_output=True, text=True,
                               cwd=Path(__file__).resolve().parents[2])
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["old_write"] == "delivered"
    with store._connect() as db:
        assert db.execute("SELECT sql FROM sqlite_master WHERE name='outbox'").fetchone()[0] == schema
        assert db.execute("SELECT rootpage FROM sqlite_master WHERE name='outbox'").fetchone()[0] == rootpage
        assert db.execute("SELECT send_status FROM outbox WHERE turn_id='startup-gate:compat'").fetchone()[0] == "synthetic"
    assert store.all_rows()[1].message_id == "old-receipt"


@pytest.mark.asyncio
@pytest.mark.live_system_guard_bypass  # Only the fixture's private worker can be signalled.
async def test_custom_session_storage_does_not_change_gate_profile_owner(worker_provider):
    from gateway.startup_gate import run_startup_gate
    runner, _, _, _, _ = worker_provider
    home = runner.config.sessions_dir.parent
    runner.config.sessions_dir = home / "custom-storage" / "sessions"
    verdict = await run_startup_gate(runner.config)
    assert verdict.ready, verdict.evidence_text
    assert len(Outbox(home).all_rows()) == 1
    assert not (home / "custom-storage").exists()


@pytest.mark.asyncio
@pytest.mark.live_system_guard_bypass  # All three disposable groups are created by this fixture.
@pytest.mark.parametrize("bom", [False, True])
async def test_profile_owner_is_bound_for_each_probe_a_b_a(worker_provider, bom):
    from gateway.startup_gate import run_startup_gate
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import is_multiplex_active, set_multiplex_active
    import hermes_yaml as yaml

    runner, _, homes, _, _ = worker_provider
    home_a = runner.config.sessions_dir.parent
    if bom:
        config_a_path = home_a / "config.yaml"
        config_a_path.write_text("\ufeff" + config_a_path.read_text(encoding="utf-8-sig"), encoding="utf-8")
    home_b = home_a.parent / "profile-b"
    home_b.mkdir()
    config_b = dict(runner.user_config)
    config_b["model"] = {**config_b["model"], "default": "gate-fixture-b", "api_key": "fixture-b"}
    (home_b / "config.yaml").write_text(("\ufeff" if bom else "") + yaml.safe_dump(config_b), encoding="utf-8")
    was_multiplex = is_multiplex_active()
    set_multiplex_active(True)
    try:
        first = await run_startup_gate(runner.config)
        token_b = set_hermes_home_override(home_b)
        try:
            second = await run_startup_gate(runner.config)
        finally:
            reset_hermes_home_override(token_b)
        third = await run_startup_gate(runner.config)
    finally:
        set_multiplex_active(was_multiplex)
    assert all(result.ready for result in (first, second, third)), [
        result.evidence_text for result in (first, second, third)]
    assert [result.evidence["model"] for result in (first, second, third)] == [
        "gate-fixture", "gate-fixture-b", "gate-fixture"]
    assert len(Outbox(home_a).all_rows()) == 2
    assert len(Outbox(home_b).all_rows()) == 1
    assert len(homes) == 3 and all(not home.exists() for home in homes)
    assert not (home_a / "state.db").exists() and not (home_b / "state.db").exists()


def rotating_oauth_worker_probe(payload):
    """Run in a disposable subprocess, using only synthetic OAuth credentials."""
    from gateway.run import GatewayRunner
    from gateway.startup_gate import _worker_main
    from hermes_cli import auth
    from hermes_cli.auth_codex import resolve_codex_runtime_credentials
    refresh_calls = []
    resolver_calls = []

    def rotate(*args, **kwargs):
        refresh_calls.append(True)
        return {"access_token": "synthetic-fresh-access", "refresh_token": "synthetic-rotated-refresh"}

    original_resolve = GatewayRunner._resolve_session_agent_runtime

    def resolve(self, **kwargs):
        if kwargs.get("source") is not None:
            return original_resolve(self, **kwargs)
        resolver_calls.append(True)
        resolve_codex_runtime_credentials()
        return payload["model"], payload["runtime"]

    auth.refresh_codex_oauth_pure = rotate
    GatewayRunner._resolve_session_agent_runtime = resolve
    install_provider_stub()
    try:
        evidence = _worker_main(payload)
        passed = bool(evidence.get("model_reply"))
    except Exception:
        passed = False
    try:
        (Path(payload["home"]) / "auth.json").read_bytes()
        store_read_refused = False
    except PermissionError:
        store_read_refused = True
    import hermes_constants
    global_root_is_private = hermes_constants.get_default_hermes_root().resolve() == hermes_constants.get_hermes_home().resolve()
    # Even a provider pointing the default-root resolver back at the owner must
    # find global auth borrowing disabled, independently of sandbox placement.
    hermes_constants.get_default_hermes_root = lambda: Path(payload["home"])
    # Report only counts/booleans, never synthetic or real credential values.
    return {"refresh_calls": len(refresh_calls), "resolver_calls": len(resolver_calls), "passed": passed,
            "store_read_refused": store_read_refused,
            "global_fallback_disabled": auth._global_auth_file_path() is None,
            "global_root_is_private": global_root_is_private}


def test_worker_never_refreshes_near_expiry_rotating_owner_oauth(tmp_path, monkeypatch):
    import base64
    import os
    import subprocess
    import sys
    import time
    import hermes_yaml as yaml
    from hermes_cli import auth

    owner = tmp_path / "owner"
    owner.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(owner))
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(owner)
    try:
        claims = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + 30}).encode()).decode().rstrip("=")
        auth._save_codex_tokens({"access_token": "e30." + claims + ".signature",
                                 "refresh_token": "synthetic-single-use-refresh"})
    finally:
        reset_hermes_home_override(token)
    (owner / "config.yaml").write_text(yaml.safe_dump({"model": {"provider": "openai-codex"}}), encoding="utf-8")
    original = (owner / "auth.json").read_bytes()
    sandbox = tmp_path / "worker"
    sandbox.mkdir()
    turn = make_turn(tmp_path)
    payload = {"home": str(owner), "nonce": turn.nonce, "model": turn.model, "runtime": turn.runtime}
    code = """
import contextlib, json, sys
sys.path.insert(0, sys.argv[1])
from test_startup_gate import rotating_oauth_worker_probe
payload = json.loads(sys.stdin.read())
with contextlib.redirect_stdout(sys.stderr):
    result = rotating_oauth_worker_probe(payload)
print(json.dumps(result))
"""
    env = {**os.environ, "HERMES_HOME": str(sandbox), "TMPDIR": str(sandbox), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-c", code, str(Path(__file__).parent)], input=json.dumps(payload),
                            text=True, capture_output=True, env=env, check=True,
                            cwd=Path(__file__).resolve().parents[2])
    reported = json.loads(result.stdout)
    assert reported["refresh_calls"] == reported["resolver_calls"] == 0
    assert reported["passed"]
    assert reported["store_read_refused"] and reported["global_fallback_disabled"]
    assert reported["global_root_is_private"]
    assert (owner / "auth.json").read_bytes() == original
    assert not (sandbox / "auth.json").exists()
    assert not (sandbox / ".anthropic_oauth.json").exists()


@pytest.mark.asyncio
@pytest.mark.live_system_guard_bypass
async def test_worker_sandbox_ignores_owner_tmpdir(worker_provider, monkeypatch):
    from gateway.startup_gate import run_startup_gate
    runner, _, homes, _, _ = worker_provider
    home = runner.config.sessions_dir.parent
    scratch = home / "cache" / "scratch"
    scratch.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(scratch))
    verdict = await run_startup_gate(runner.config)
    assert verdict.ready, verdict.evidence_text
    assert len(homes) == 1
    assert home.resolve() not in homes[0].resolve().parents
