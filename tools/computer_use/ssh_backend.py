"""CuaDriver's existing stdio MCP transported through an SSH alias.

The remote app owns its TCC attribution and daemon authorization mode. This
adapter owns only the SSH child and a persistent, session-scoped MCP connection.
It never launches a controller desktop or falls back to the controller's CLI.
"""
from __future__ import annotations

import contextlib
import asyncio
import uuid
import threading

from tools.computer_use.cua_backend import CuaDriverBackend
from tools.computer_use.cua_backend_session import _AsyncBridge, _CuaDriverSession, _is_ended_session_result, _outcome_unknown
from tools.computer_use.node_routing import remaining
from tools.nodes import Node


class _SSHDriverSession(_CuaDriverSession):
    def __init__(self, node: Node):
        super().__init__(_AsyncBridge())
        from tools.environments.local import served_profile_child_env, _sanitize_subprocess_env
        self.node = node
        # Resolve the served profile on the caller thread, not the async bridge thread.
        self._child_env = _sanitize_subprocess_env(served_profile_child_env())
        self.broken = False
        self._cancel_scope = None
        self._closed = threading.Event()

    async def _lifecycle_coro(self):
        import anyio
        self._closed.clear()
        try:
            # Cancel through AnyIO's owning scope, not Future.cancel()/Task.cancel().
            # Native cancellation can interrupt the SDK's shielded SSH-child reap.
            with anyio.CancelScope() as scope:
                self._cancel_scope = scope
                await super()._lifecycle_coro()
        finally:
            self._cancel_scope = None
            self._closed.set()

    def _connection_parameters(self):
        from mcp import StdioServerParameters
        argv = self.node.ssh_argv(self.node.cua_driver, "mcp")
        return StdioServerParameters(command=argv[0], args=argv[1:], env=self._child_env)

    def _startup_timeout(self) -> float:
        return remaining()

    def _call_tool_via_cli(self, name, args, timeout):
        raise RuntimeError("Remote CuaDriver has no controller CLI fallback. Capture again on this node")

    def call_tool(self, name, args, timeout=30.0):
        # Never recreate/redeclare within an operation. EOF tombstones the remote
        # logical session. A write whose response was lost may already have happened.
        try:
            if self.broken or not self._started:
                raise RuntimeError("Remote MCP connection ended. Capture again before input")
            budget = remaining(timeout)
            future = asyncio.run_coroutine_threadsafe(self._call_tool_async(name, args), self._bridge._loop)
            try:
                result = future.result(timeout=budget)
            except Exception:
                future.cancel()
                raise
            if _is_ended_session_result(result):
                raise RuntimeError("Remote driver session ended. Capture again before input")
            return result
        except Exception as exc:
            self.broken = True
            self._notify_transport_reset()
            with self._lock:
                self._started = False
                self._stop_lifecycle_locked()
            self._bridge.stop()
            code = "timeout_outcome_unknown" if isinstance(exc, TimeoutError) else "transport_outcome_unknown"
            return _outcome_unknown(name, exc, code)

    def _stop_lifecycle_locked(self) -> None:
        self._signal_shutdown_locked()
        future, self._lifecycle_future = self._lifecycle_future, None
        if future is not None and not self._closed.wait(timeout=3.0):
            self._cancel_lifecycle()
            self._closed.wait(timeout=3.0)

    def _cancel_lifecycle(self):
        loop, scope = self._bridge._loop, self._cancel_scope
        if loop is not None and scope is not None:
            loop.call_soon_threadsafe(scope.cancel)

    def _signal_shutdown_locked(self):
        super()._signal_shutdown_locked()
        if self._session is None:
            self._cancel_lifecycle()


class SSHCuaDriverBackend(CuaDriverBackend):
    def __init__(self, node: Node, *, permission_mode: str = "standard"):
        super().__init__(permission_mode="standard")
        self._controller_permission_mode = permission_mode
        self._started = False
        self.node = node
        self._capture_id = None
        self._session = _SSHDriverSession(node)
        self._bridge = self._session._bridge
        self._session.set_transport_reset_callback(self._handle_transport_reset)

    @property
    def needs_restart(self) -> bool:
        return self._session.broken or (self._started and not self._session._started)

    def start(self) -> None:
        if self._started:
            return
        from tools.computer_use.cua_backend import _computer_use_cfg
        if self._controller_permission_mode == "bounded" or _computer_use_cfg().get("capability_manifest"):
            raise RuntimeError("Bounded mode needs a trusted node-side capability manifest. "
                               "The SSH adapter cannot apply the controller's local manifest to a remote daemon")

    def prepare_for_call(self) -> None:
        from tools.computer_use.node_doctor import NodeUnavailable, node_doctor
        report = node_doctor(self.node)
        if not report["ok"]:
            self._handle_transport_reset()
            raise NodeUnavailable(report)
        if self._started:
            return
        try:
            from pm import ensure_import
            ensure_import("computer-use")
            self._session.start()
            # `mcp` can auto-launch a default-standard public daemon if the
            # prepared daemon vanished after preflight. Observe again before
            # declaring a logical session or sending any desktop operation.
            report = node_doctor(self.node)
            if not report["ok"]:
                raise NodeUnavailable(report)
            self._session_id = f"hermes-{uuid.uuid4().hex[:12]}"
            result = self._session.call_tool("start_session", {"session": self._session_id})
            if result.get("isError"):
                raise RuntimeError(f"Remote start_session denied: {result.get('data', result)}")
            self._started = True
            self._configure_capture_limit()
            from tools.computer_use.cua_backend import _cua_no_overlay
            if _cua_no_overlay():
                self._best_effort("set_agent_cursor_enabled failed", lambda: self._session.call_tool(
                    "set_agent_cursor_enabled", {"enabled": False, "session": self._session_id}))
        except Exception:
            self._session.broken = True
            self.stop()
            raise

    def _configure_capture_limit(self) -> None:
        from tools.computer_use.cua_backend import _computer_use_max_image_dimension
        limit = _computer_use_max_image_dimension()
        if limit is not None:
            result = self._session.call_tool("set_config", {
                "max_image_dimension": limit, "session": self._session_id})
            if result.get("isError"):
                raise RuntimeError("Node rejected the session capture-size setting")

    def stop(self) -> None:
        # Closing MCP retires this logical session. Never stop the node's shared daemon.
        if self._started and not self._session.broken:
            with contextlib.suppress(Exception):
                self._session.call_tool("end_session", {"session": self._session_id}, timeout=1.0)
        self._started = False
        self._handle_transport_reset()
        # stop() is also required after setup failure, before _started was set.
        with self._session._lock:
            self._session._started = False
            self._session._stop_lifecycle_locked()
        self._session._bridge.stop()

    def _clear_active_target(self) -> None:
        self._capture_id = None
        super()._clear_active_target()

    def _set_active_target(self, target) -> None:
        self._capture_id = None
        super()._set_active_target(target)

    def _call_capture_tool(self, name, args):
        result = super()._call_capture_tool(name, args)
        if name in ("get_window_state", "screenshot"):
            capture_id = (result.get("structuredContent") or {}).get("capture_id")
            self._capture_id = capture_id if isinstance(capture_id, str) and not result.get("isError") else None
        return result

    def _action(self, name, args, *, inject_session=True):
        if self._capture_id and self._session.supports_input_property(name, "capture_id"):
            args.setdefault("capture_id", self._capture_id)
        return super()._action(name, args, inject_session=inject_session)

    def _cli_refetch(self, *args, **kwargs):
        return None

    def _empty_discovery_reason(self) -> str:
        return f"Node {self.node.name}: window discovery returned no windows. Run computer-use doctor --node {self.node.name}"
