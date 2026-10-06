"""Sandboxed forward-only readiness probe; never a public/model tool.

The disposable worker runs the real adapter -> GatewayRunner -> TurnRunner ->
AIAgent path. A process boundary is necessary: cancelling an asyncio task cannot
stop a provider call in the gateway's synchronous executor. No poller, scheduler,
goal dispatcher or user transport is started in the worker. Lifecycle promotion
and generation retirement belong to the caller, not this module.

Both generation startup paths call ``await run_startup_gate(config)`` after
claiming standby identity and before readiness or poller acquisition. Only
``verdict.ready`` permits activation. The caller records a failed verdict with
its evidence through the existing coordinator retirement path.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid
import weakref

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult, _ExtractedResponse
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource

LOOPBACK_PLATFORM = "local"
DEADLINE_SECONDS = 45.0
_REPLY_WRAPPERS = " \t\r\n\"'`*_.,!?:;"


def _matches_ready_reply(reply: str, nonce: str) -> bool:
    """Normalize presentation around readiness while preserving nonce identity."""
    parts = reply.strip(_REPLY_WRAPPERS).split()
    return (len(parts) == 2 and parts[0].strip(_REPLY_WRAPPERS).upper() == "HERMES_READY"
            and parts[1].strip(_REPLY_WRAPPERS) == nonce)


@dataclass(frozen=True)
class StartupGateVerdict:
    verdict: str
    evidence: dict
    elapsed_seconds: float

    @property
    def evidence_text(self) -> str:
        """Serializable durable coordinator receipt, including the measured duration."""
        return json.dumps({"verdict": self.verdict, "elapsed_seconds": self.elapsed_seconds,
                           **self.evidence}, sort_keys=True)

    @property
    def ready(self) -> bool:
        return self.verdict == "passed"


@dataclass(eq=False)
class _LoopbackTurn:
    nonce: str
    model: str
    runtime: dict
    outbox_home: Path
    source: SessionSource | None = None
    adapter: BasePlatformAdapter | None = None
    active: bool = True
    evidence: dict = field(default_factory=dict)


def gate_for_source(source) -> _LoopbackTurn | None:
    """Privilege is a live, issued object capability, never a wire identity/id.

    An external adapter can copy every public source field without gaining the
    capability. Neither source serialization nor metadata carries it.
    """
    ref = getattr(source, "_transport_adapter_ref", None)
    adapter = ref() if isinstance(ref, weakref.ReferenceType) else None
    turn = getattr(adapter, "_startup_gate_turn", None)
    if (isinstance(turn, _LoopbackTurn) and turn.active and turn.source is source
            and turn.adapter is adapter):
        return turn
    return None


def note_gate_guard(source, guard: str) -> None:
    turn = gate_for_source(source)
    if turn is not None:
        turn.evidence[guard] = True


class _LoopbackAdapter(BasePlatformAdapter):
    """No network implementation and no send-to-terminal-row fallback.

    Only the real final-reply bracket can create evidence, after runner success.
    Interim sends, exceptions, admission acks and media never count as a reply.
    """
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, turn: _LoopbackTurn):
        super().__init__(PlatformConfig(enabled=True, typing_indicator=False), Platform(LOOPBACK_PLATFORM))
        self._startup_gate_turn = turn
        turn.adapter = self

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=False, error="loopback has no outbound transport", pre_send=True)

    async def get_chat_info(self, chat_id):
        return {"name": "Startup gate", "type": "dm"}

    async def _extract_response_content(self, response, event, session_key, *, is_ephemeral_response):
        # Treat the model's output as plain text: never resolve URLs, files or TTS.
        return _ExtractedResponse(text_content=response, images=[], media_files=[],
                                  local_files=[], force_document_attachments=False, pre_extract=response)

    def _wants_auto_tts(self, *args, **kwargs):
        return False

    async def send_final_ledgered(self, event, session_key, text_content, metadata, **kwargs):
        turn = gate_for_source(event.source)
        if (turn is None or not getattr(event, "_agent_turn_succeeded", False)
                or not getattr(event, "_heartbeat_execution_started", False)
                or not turn.evidence.get("adapter_guard") or not turn.evidence.get("runner_guard") or not turn.evidence.get("model_reply")
                or not turn.evidence.get("turn_completed")
                or not _matches_ready_reply(text_content, turn.nonce)):
            return SendResult(success=False, error="no correlated runner reply", pre_send=True), self
        from gateway.outbox import Outbox
        row = await asyncio.to_thread(Outbox(turn.outbox_home).enqueue_synthetic, turn.nonce, {
            "chat_id": "loopback:" + turn.nonce, "content": text_content,
            "platform": LOOPBACK_PLATFORM, "nonce": turn.nonce, "session_key": session_key,
        })
        turn.evidence.update(runner_output=True, turn_id=row.turn_id, sequence=row.sequence,
                             state=row.state, send_status="synthetic")
        return SendResult(success=True, message_id=row.idempotency_key), self


async def _run_loopback(runner, turn: _LoopbackTurn) -> dict:
    """Run exactly one admitted turn on a runner with isolated session storage."""
    adapter = _LoopbackAdapter(turn)
    adapter.gateway_runner = runner
    source = SessionSource(platform=adapter.platform, chat_id="loopback:" + turn.nonce,
                           user_id="startup-gate", chat_type="dm")
    source._transport_adapter_ref = weakref.ref(adapter)
    source._startup_gate_capability = turn
    turn.source = source
    event = MessageEvent(text="Reply with exactly these tokens and nothing else: HERMES_READY " + turn.nonce,
                         source=source, message_id=turn.nonce, internal=True,
                         allow_gateway_control=False)
    runner.adapters[adapter.platform] = adapter
    runner._wire_adapter_handlers(adapter)
    key = runner._session_key_for_source(source)
    turn.evidence.update(nonce=turn.nonce, session_key=key, model=turn.model)
    try:
        await adapter.handle_message(event)
        turn.evidence["admitted"] = bool(getattr(event, "_gateway_accepted", False))
        task = adapter._session_tasks.get(key)
        if task is not None:
            await task
        return dict(turn.evidence)
    finally:
        turn.active = False
        await adapter.cancel_session_processing(key)
        runner._evict_cached_agent(key)
        runner.adapters.pop(adapter.platform, None)


def _create_sandbox(home: Path, evidence: dict) -> tempfile.TemporaryDirectory:
    """Try portable roots without trusting Hermes's ambient scratch variables."""
    from hermes_constants import get_default_hermes_root

    excluded = (home.resolve(), get_default_hermes_root().resolve())
    # Reuse the stdlib's uncached platform candidates, including Windows roots.
    # gettempdir() alone can cache a Hermes-owned TMPDIR and hide safe fallbacks.
    candidates = tempfile._candidate_tempdir_list()
    prefix = os.environ.get("PREFIX")
    if prefix:
        candidates.append(str(Path(prefix) / "tmp"))  # Termux's system scratch root.
    attempts = evidence["sandbox_candidates"] = []
    seen = set()
    for candidate in candidates:
        attempt = {"path": str(candidate)}
        try:
            root = Path(candidate).resolve()
            if root in seen:
                continue
            seen.add(root)
            attempt["path"] = str(root)
            attempts.append(attempt)
            if any(root.is_relative_to(excluded_home) for excluded_home in excluded):
                attempt["result"] = "inside Hermes home"
                continue
            sandbox = tempfile.TemporaryDirectory(prefix="hermes-startup-gate-", dir=root)
            attempt["result"] = "selected"
            return sandbox
        except (OSError, RuntimeError) as exc:
            if attempt not in attempts:
                attempts.append(attempt)
            attempt["result"] = type(exc).__name__
    evidence["error"] = "no usable sandbox root outside Hermes homes"
    raise OSError(evidence["error"])


async def run_startup_gate(config: GatewayConfig) -> StartupGateVerdict:
    """Run one private probe, returning pass/fail evidence without lifecycle writes.

    ``gateway.overlap_handover.startup_gate_timeout_seconds`` in config.yaml may
    shorten the default 45-second overall bound. No environment override exists.
    All agents and sessions live in a disposable worker process. Only the
    nonce-bound, already-terminal reply reaches the owning profile's outbox.
    """
    from gateway.generation import overlap_handover_enabled
    if not overlap_handover_enabled(config):
        return StartupGateVerdict("disabled", {}, 0.0)
    started = time.monotonic()
    nonce = uuid.uuid4().hex
    from hermes_constants import get_hermes_home
    home = get_hermes_home().resolve()
    evidence = {"nonce": nonce, "deadline_seconds": DEADLINE_SECONDS}
    proc = None
    verdict = "failed"
    from utils import fast_safe_load
    try:
        config_path = home / "config.yaml"
        config = fast_safe_load(config_path.read_text(encoding="utf-8-sig")) if config_path.exists() else {}
        value = ((config.get("gateway") or {}).get("overlap_handover") or {}).get(
            "startup_gate_timeout_seconds", DEADLINE_SECONDS)
        timeout_seconds = float(value)
        if isinstance(value, bool) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= DEADLINE_SECONDS:
            raise ValueError("deadline must be positive and at most 45 seconds")
        evidence["deadline_seconds"] = timeout_seconds
    except Exception:
        evidence["error"] = "invalid config.yaml startup gate deadline"
        return StartupGateVerdict("failed", evidence, time.monotonic() - started)

    try:
        with _create_sandbox(home, evidence) as sandbox:
            # No ambient provider secrets or refresh grants cross the process boundary.
            env = {k: v for k, v in os.environ.items() if k in {
                "PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
                "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "HERMES_TEST_ISOLATION"}}
            env["HOME"] = sandbox
            env["USERPROFILE"] = sandbox
            env["HERMES_HOME"] = sandbox
            env["TMPDIR"] = sandbox
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
            try:
                async with asyncio.timeout(max(0, timeout_seconds - (time.monotonic() - started))):
                    model, runtime = await asyncio.to_thread(_resolve_owner_runtime, home)
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable, "-m", "gateway.startup_gate", stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                        cwd=Path(__file__).resolve().parents[1], env=env, start_new_session=True)
                    evidence["worker_pid"] = proc.pid
                    stdout, _ = await proc.communicate(json.dumps({"nonce": nonce, "home": str(home),
                                                                  "model": model, "runtime": runtime}).encode("utf-8"))
                    if proc.returncode != 0:
                        evidence["error"] = "startup gate worker failed"
                    else:
                        reported = json.loads(stdout)
                        if reported.get("nonce") != nonce:
                            raise ValueError("startup gate worker nonce mismatch")
                        evidence.update(reported)
                        from gateway.outbox import Outbox
                        store = Outbox(home)
                        with store._connect() as db:
                            rows = db.execute("SELECT state, send_status, payload FROM outbox WHERE turn_id=?",
                                              ("startup-gate:" + nonce,)).fetchall()
                        if (len(rows) == 1 and rows[0]["state"] == "failed_unsent"
                                and rows[0]["send_status"] == "synthetic"
                                and json.loads(rows[0]["payload"]).get("nonce") == nonce
                                and _matches_ready_reply(json.loads(rows[0]["payload"]).get("content", ""), nonce)
                                and evidence.get("model_reply") and evidence.get("zero_tools")
                                and evidence.get("turn_completed")
                                and not evidence.get("tool_attempted")
                                and evidence.get("runner_output") and evidence.get("adapter_guard")
                                and evidence.get("runner_guard")):
                            verdict = "passed"
                        else:
                            evidence.setdefault("error", "no terminal nonce-correlated runner reply")
            except TimeoutError:
                evidence["error"] = "overall startup gate deadline exceeded"
            except Exception as exc:
                evidence["error"] = type(exc).__name__  # never expose provider credentials/errors
            finally:
                if proc is not None:
                    # Cancelling asyncio cannot stop a synchronous provider thread.
                    if proc.returncode is None:
                        from agent.deadline import kill_process_tree
                        await asyncio.to_thread(kill_process_tree, proc.pid)
                    await proc.wait()
                    evidence["worker_reaped"] = True
    except Exception as exc:
        verdict = "failed"
        evidence.setdefault("error", type(exc).__name__)
    elapsed = time.monotonic() - started
    if elapsed > timeout_seconds and verdict == "passed":
        verdict = "failed"
        evidence["error"] = "overall startup gate deadline exceeded"
    return StartupGateVerdict(verdict, evidence, elapsed)


def _resolve_owner_runtime(home: Path) -> tuple[str, dict]:
    """Normal owner-side resolution persists any rotation before lending a bearer."""
    from gateway.run import (
        _load_gateway_config, _profile_runtime_scope, _resolve_gateway_model,
        _resolve_runtime_agent_kwargs,
    )
    with _profile_runtime_scope(home):
        # No user session exists yet. Use the same default/fallback provider
        # resolution as an ordinary gateway turn without constructing a runner
        # against the owner's session DB, hooks or recovery state.
        runtime = _resolve_runtime_agent_kwargs()
        model = runtime.pop("model", None) or _resolve_gateway_model(_load_gateway_config())
        if not model and runtime.get("provider"):
            from hermes_cli.models import get_default_model_for_provider
            model = get_default_model_for_provider(runtime["provider"])
    if runtime.get("command") or runtime.get("api_mode") in {"acp", "codex_app_server"}:
        raise ValueError("process-backed providers are not supported by the text-only gate")
    if _has_tool_request_override(runtime.get("request_overrides")):
        raise ValueError("provider overrides contain tools")
    if not isinstance(runtime.get("api_key"), str) or not runtime["api_key"]:
        raise ValueError("startup gate requires an already-resolved static bearer")
    # Allowlist excludes pool objects, callbacks, refresh grants and provider state.
    return model, {key: runtime[key] for key in (
        "provider", "requested_provider", "api_key", "base_url", "api_mode",
        "max_tokens", "request_overrides", "capabilities",
    ) if key in runtime}


def _disable_worker_auth() -> None:
    """Process-local: no store fallback, credential resolution or OAuth recovery."""
    from hermes_cli import auth
    from agent import anthropic_credentials
    from hermes_cli import nous_auth_keepalive

    def refused(*args, **kwargs):
        raise PermissionError("startup gate cannot resolve or refresh credentials")

    auth._global_auth_file_path = lambda: None
    # Provider registry resolution and reactive retry both use these entry points.
    for name in (
        "resolve_codex_runtime_credentials", "resolve_xai_oauth_runtime_credentials",
        "resolve_nous_runtime_credentials", "resolve_minimax_oauth_runtime_credentials",
        "resolve_qwen_runtime_credentials", "refresh_codex_oauth_pure",
        "refresh_xai_oauth_pure", "refresh_nous_oauth_pure",
        "build_minimax_oauth_token_provider",
    ):
        setattr(auth, name, refused)
    anthropic_credentials.resolve_anthropic_token = refused
    anthropic_credentials.refresh_anthropic_oauth_pure = refused
    nous_auth_keepalive.start_nous_auth_keepalive = lambda *args, **kwargs: None


def install_gate_tool_refusal(turn: _LoopbackTurn, agent) -> None:
    """Stop on the first unsolicited call, before dispatch or model retry.

    The normal message-staging seam precedes dispatch and dropped-call retry
    nudges. Refuse both actual calls and bare tool-use finish signals there,
    preserving role staging without spending another model call.
    """
    original_build = agent._build_assistant_message

    def build(message, finish_reason):
        if (getattr(message, "tool_calls", None) or getattr(message, "function_call", None)
                or finish_reason in {"tool_calls", "function_call", "tool_use"}):
            turn.evidence.update(tool_attempted=True,
                                 error="model attempted a tool call, refused by empty toolset")
            raise RuntimeError(turn.evidence["error"])
        return original_build(message, finish_reason)

    agent._build_assistant_message = build


def record_model_result(turn: _LoopbackTurn, result: dict) -> None:
    """Refuse any attempted tool use, including invalid calls the core rejected.

    The empty schema and valid-name set already prevent execution. Inspect the
    real conversation result before any response normalization or final delivery,
    so diagnostic/recovery text cannot stand in for a model's reply.
    """
    messages = result.get("messages") or []
    tool_attempted = bool(turn.evidence.get("tool_attempted")) or any(
        m.get("tool_calls") or m.get("function_call") or m.get("role") == "tool"
        or m.get("finish_reason") in {"tool_calls", "function_call", "tool_use"} for m in messages)
    turn.evidence["tool_attempted"] = tool_attempted
    reply = result.get("final_response")
    if tool_attempted:
        turn.evidence["error"] = "model attempted a tool call, refused by empty toolset"
    elif not isinstance(reply, str) or not reply.strip():
        turn.evidence["error"] = "model returned an empty reply"
    elif (result.get("failed") or result.get("interrupted") or result.get("completed") is not True
          or result.get("api_calls") != 1):
        turn.evidence["error"] = "model turn failed or did not complete in one call"
    elif not _matches_ready_reply(reply, turn.nonce):
        # A refused text reply is still a probe result. Preserve it as terminal
        # evidence, never as sendable work that a later promotion could recover.
        from gateway.outbox import Outbox
        Outbox(turn.outbox_home).enqueue_synthetic(turn.nonce, {
            "chat_id": "loopback:" + turn.nonce, "content": reply,
            "platform": LOOPBACK_PLATFORM, "nonce": turn.nonce,
            "session_key": turn.evidence["session_key"],
        })
        turn.evidence["error"] = "model reply did not match the loopback token"
    else:
        turn.evidence.update(model_reply=True, turn_completed=True)
        return
    raise RuntimeError(turn.evidence["error"])


def _fence_worker_writes(sandbox: Path, outbox_home: Path) -> None:
    """Reject credential reads and shared writes, including global provider paths.

    The worker receives only a resolved bearer, never a refresh grant.
    This audit hook lives only in the disposable process. SQLite may open just
    private databases and the one shared outbox, never the user's session DB.
    """
    from urllib.parse import unquote, urlsplit
    sandbox = sandbox.resolve()
    outbox_home = outbox_home.resolve()
    outbox = outbox_home / "gateway-outbox.db"

    def private(path) -> bool:
        if isinstance(path, int):
            return True  # Already-open standard/private descriptors.
        resolved = Path(os.fsdecode(path)).resolve()
        return resolved == sandbox or sandbox in resolved.parents

    def audit(event, args):
        if event in {"subprocess.Popen", "os.system", "os.fork", "os.posix_spawn"}:
            raise PermissionError("startup gate cannot launch child processes")
        if event == "sqlite3.connect":
            db_path = os.fsdecode(args[0])
            if db_path.startswith("file:"):
                db_path = unquote(urlsplit(db_path).path)
            if db_path != ":memory:" and not private(db_path) and Path(db_path).resolve() != outbox:
                raise PermissionError("startup gate cannot open a shared session database")
        elif event == "open":
            path, mode, flags = args
            if not isinstance(path, int) and Path(os.fsdecode(path)).name in {
                    "auth.json", ".anthropic_oauth.json", ".env", "credentials", "credentials.json"}:
                raise PermissionError("startup gate cannot read credential stores")
            writing = (flags or 0) & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            if writing and not private(path):
                raise PermissionError("startup gate cannot write outside its private home")
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.truncate", "os.utime",
                       "os.rename", "os.link", "os.symlink"}:
            paths = args[:2] if event in {"os.rename", "os.link", "os.symlink"} else args[:1]
            for path in paths:
                # Outbox's mkdir(exist_ok=True) addresses its already-existing home.
                if event == "os.mkdir" and Path(path).resolve() == outbox_home and outbox_home.is_dir():
                    continue
                if not private(path):
                    raise PermissionError("startup gate cannot mutate shared files")

    sys.addaudithook(audit)


def _has_tool_request_override(value) -> bool:
    """SDK extra_body is merged into the request after Hermes builds its schema."""
    if isinstance(value, dict):
        return bool({"tools", "tool_choice", "functions", "function_call"} & value.keys()) or any(
            _has_tool_request_override(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_tool_request_override(item) for item in value)
    return False


def _worker_main(payload: dict) -> dict:
    from hermes_constants import get_hermes_home
    sandbox = get_hermes_home()
    home = Path(payload["home"])
    _fence_worker_writes(sandbox, home)
    _disable_worker_auth()
    from gateway.run import GatewayRunner

    class LoopbackRunner(GatewayRunner):
        def _init_startup_checks(self):
            # Readiness must not install software, even in its private home.
            pass

        def _init_session_db(self):
            super()._init_session_db()

        def _init_registries_and_clocks(self):
            super()._init_registries_and_clocks()
            from gateway.hooks import HookRegistry
            self.hooks = HookRegistry()

        def _get_proxy_url(self):
            # The probe must exercise this worker's empty tool schema.
            return None

        def _persist_active_agents(self):
            pass

        async def _run_post_turn_hooks(self, **kwargs):
            pass

    model, runtime = payload["model"], dict(payload["runtime"])
    runtime["credential_pool"] = None
    cfg = GatewayConfig(sessions_dir=sandbox / "sessions")
    cfg.streaming.enabled = False
    runner = LoopbackRunner(cfg)
    turn = _LoopbackTurn(payload["nonce"], model, runtime, Path(payload["home"]))
    try:
        return asyncio.run(_run_loopback(runner, turn))
    finally:
        # The process never survives a probe; no cached agent can become reusable.
        runner.session_store.close_all_db_handles()


if __name__ == "__main__":
    payload = json.loads(sys.stdin.buffer.read())
    with contextlib.redirect_stdout(sys.stderr):
        try:
            from gateway.startup_gate import _worker_main as worker_main
            result = worker_main(payload)
        except Exception as exc:
            result = {"nonce": payload["nonce"], "error": "worker exception: " + type(exc).__name__}
    print(json.dumps(result))
