"""Named-node process receipts and supervision over the original SSH connection.

Only a run's own remote supervisor signals its unreaped child. Controllers send
an incarnation-scoped stop request over existing SSH, never a remote PID to
``kill``. The UUID is a run identity, not a separate service credential. A missing
supervisor or unreachable host leaves the handle retryable, not exited.
"""
from __future__ import annotations

import base64
import codecs
from dataclasses import asdict, dataclass
import json
import logging
import re
import shlex
import threading
import time
import uuid


@dataclass(frozen=True)
class SSHProcessBinding:
    node_name: str
    ssh: str
    platform: str
    execution_key: str
    token: str
    profile_home: str


def binding_record(binding):
    return asdict(binding) if binding is not None else None


def restore_binding(record):
    if record is None:
        return None
    binding = SSHProcessBinding(**record)
    if not all(isinstance(value, str) and value for value in asdict(binding).values()):
        raise ValueError("Incomplete SSH process identity")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", binding.node_name):
        raise ValueError("Invalid SSH process node name")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@:-]*", binding.ssh):
        raise ValueError("Invalid SSH process target")
    if not re.fullmatch(r"[a-f0-9]{32}", binding.token):
        raise ValueError("Invalid SSH process run token")
    return binding


def process_provenance(session):
    binding = session.ssh_binding
    if binding is None:
        return {}
    result = {"node": binding.node_name, "ssh": binding.ssh, "pid_scope": "ssh"}
    if session.remote_error:
        result.update(remote_status="unavailable", remote_error=session.remote_error)
    return result


# These programs use only the target's existing Python 3 and POSIX shell. Files
# stay on that target, outside both the controller checkpoint and SSH file sync.
_REMOTE_WORKER = r'''
import fcntl, json, os, pathlib, signal, subprocess, sys, time
request = json.loads(sys.argv[1])
run = pathlib.Path(request["run"])
token = request["token"]
def publish(record):
    record["token"] = token
    temporary = run / "receipt.tmp"
    temporary.write_text(json.dumps(record), encoding="utf-8")
    os.replace(temporary, run / "receipt.json")
with (run / "lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    child = None
    try:
        with (run / "output").open("ab", buffering=0) as output:
            child = subprocess.Popen(["bash", "-c", request["command"]],
                cwd=os.path.expanduser(request["cwd"]) if request["cwd"] else None,
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True)
            publish({"state": "running", "pid": child.pid})
            killed = False
            source = ""
            while True:
                # No poll/wait has reaped the child between this check and the
                # signal. Its PID/process group cannot be recycled onto a stranger.
                stop = run / "stop.json"
                if stop.exists() and json.loads(stop.read_text())["token"] == token:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    killed = True
                    source = json.loads(stop.read_text()).get("source", "process.kill")
                    code = child.wait()
                    break
                code = child.poll()
                if code is not None:
                    break
                time.sleep(0.1)
            publish({"state": "exited", "pid": child.pid, "exit_code": code,
                     "reason": "killed" if killed else "exited", "source": source})
    except Exception as exc:
        if child is None:
            publish({"state": "exited", "exit_code": -1, "reason": "failed_start", "error": str(exc)})
        else:
            # A broken supervisor is not evidence that its command exited.
            publish({"state": "unavailable", "pid": child.pid, "error": str(exc)})
'''

_REMOTE_REQUEST = r'''
import base64, fcntl, json, os, pathlib, subprocess, sys, time
request = json.loads(sys.argv[1])
token = request["token"]
run = pathlib.Path.home() / ".cache" / "hermes" / "processes" / token
os.umask(0o077)
def receipt():
    try:
        result = json.loads((run / "receipt.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"token": token, "state": "unavailable"}
    if result.get("token") != token:
        raise RuntimeError("SSH process receipt token mismatch")
    return result
def supervised():
    with (run / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
if request["action"] == "launch":
    run.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also refuses accidental transport-level launch retries.
    run.mkdir()
    worker_request = {**request, "run": str(run)}
    with (run / "supervisor.log").open("ab") as log:
        subprocess.Popen([sys.executable, "-c", request["worker"], json.dumps(worker_request)],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (run / "receipt.json").exists():
        time.sleep(0.02)
result = receipt()
if result["state"] == "running" and not supervised():
    result["state"] = "unavailable"
if request["action"] == "stop":
    if result["state"] == "running":
        # The token-scoped request is consumed by the original supervisor. A stale
        # receipt can never authorize a PID signal from a new SSH connection.
        temporary = run / ("stop." + str(os.getpid()) + ".tmp")
        temporary.write_text(json.dumps({"token": token, "source": request.get("source", "process.kill")}), encoding="utf-8")
        os.replace(temporary, run / "stop.json")
        deadline = time.monotonic() + request.get("wait", 3)
        while time.monotonic() < deadline:
            result = receipt()
            if result["state"] == "exited":
                break
            time.sleep(0.02)
    if result["state"] != "exited":
        result["state"] = "unavailable"
if request["action"] != "launch":
    try:
        with (run / "output").open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            offset = min(request.get("offset", 0), size)
            offset = max(offset, size - request.get("max_output", 200000))
            stream.seek(offset)
            result.update(offset=offset, size=size,
                          output=base64.b64encode(stream.read()).decode("ascii"))
    except FileNotFoundError:
        pass
print(json.dumps(result))
'''


class SSHLaunchUncertain(RuntimeError):
    """An addressable run exists, but launch acknowledgement was lost."""

    def __init__(self, session, message):
        super().__init__(message)
        self.session = session


class SSHProcessMixin:
    @staticmethod
    def _matches_ssh_scope(session, home, node=None):
        binding = session.ssh_binding
        if binding is None:
            return node is None
        return binding.profile_home == home and (node is None or
            (binding.node_name, binding.ssh, binding.platform) == (node.name, node.ssh, node.platform))

    def _ssh_environment(self, session):
        if session.env_ref is None:
            from tools.environments.ssh import SSHEnvironment
            # Never consult nodes.default or a mutable named-node registry here.
            session.env_ref = SSHEnvironment(host=session.ssh_binding.ssh, user="", sync_files=False)
            from tools.nodes import Node
            binding = session.ssh_binding
            session.env_ref._hermes_node = Node(binding.node_name, binding.ssh, binding.platform)
        if getattr(session.env_ref, "host", session.ssh_binding.ssh) != session.ssh_binding.ssh:
            raise RuntimeError("SSH environment target changed since this process was launched")
        return session.env_ref

    def _ssh_request(self, session, action, *, timeout=10, **extra):
        request = {"action": action, "token": session.ssh_binding.token, **extra}
        result = self._ssh_environment(session)._run_ssh(
            shlex.join(["python3", "-c", _REMOTE_REQUEST, json.dumps(request)]), timeout=timeout)
        if result.returncode != 0:
            raise RuntimeError(f"SSH process transport failed: {result.stderr.strip()}")
        record = json.loads(result.stdout)
        if record.get("token") != session.ssh_binding.token:
            raise RuntimeError("SSH process receipt token mismatch")
        return record

    def _spawn_ssh(self, env, node, command, cwd, task_id, owner_task_id, session_key,
                   timeout, persist_on_release):
        from tools.node_execution import current_execution
        execution = current_execution()
        if execution is None or execution.node != node:
            raise ValueError("Named-node background processes require a bound node execution")
        from hermes_constants import get_hermes_home
        binding = SSHProcessBinding(node.name, node.ssh, node.platform, execution.key,
                                    uuid.uuid4().hex, str(get_hermes_home()))
        session = self._new_session(command, execution.task_id, owner_task_id or execution.task_id or task_id, session_key, cwd,
                                    env_ref=env, pid_scope="ssh", ssh_binding=binding,
                                    persist_on_release=persist_on_release)
        # Persist the token before the non-idempotent remote launch. Even a dropped
        # response leaves an addressable handle whose next operation only inspects.
        with self._lock:
            self._running[session.id] = session
        try:
            self._write_checkpoint(required=True)
        except Exception:
            with self._lock:
                self._running.pop(session.id, None)
            raise
        try:
            snapshot = getattr(env, "_snapshot_path", None)
            if snapshot and getattr(env, "_snapshot_ready", False):
                command = f"source {shlex.quote(snapshot)}; builtin cd -- {shlex.quote(cwd or env.cwd)}; {command}"
            record = self._ssh_request(session, "launch", timeout=timeout,
                                       command=command, cwd=cwd or "", worker=_REMOTE_WORKER)
            session.pid = record.get("pid")
            if record.get("state") == "unavailable":
                raise RuntimeError("Remote launch receipt is not available yet")
        except Exception as exc:
            session.remote_error = str(exc)
            self._start_ssh_poller(session)
            raise SSHLaunchUncertain(session,
                f"SSH launch outcome unknown for {session.id} on {node.name} ({node.ssh}): {exc}. "
                "Do not re-run the command. Poll this handle to reconcile it.") from exc
        self._start_ssh_poller(session)
        self._write_checkpoint()
        return session

    def _start_ssh_poller(self, session):
        from contextvars import copy_context
        reader = threading.Thread(target=copy_context().run, args=(self._ssh_poller_entry, session),
                                  name=f"proc-ssh-poller-{session.id}", daemon=True)
        session._reader_thread = reader
        reader.start()

    def _ssh_poller_entry(self, session):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        token = set_hermes_home_override(session.ssh_binding.profile_home)
        try:
            self._ssh_poller_loop(session)
        finally:
            reset_hermes_home_override(token)

    def _ssh_poller_loop(self, session):
        # A transport outage is not an exit. Retry the same receipt, never launch.
        while not session._completion_event.wait(2):
            if self._survivor_pollers_stopped.is_set():
                return
            self._refresh_ssh(session)

    def _refresh_ssh(self, session, *, action="inspect", timeout=10):
        if session.exited:
            return True
        if action == "stop":
            # Stop is an idempotent token-scoped control request, independent of
            # the output decoder/offset lock held by an in-flight long poll.
            deadline = time.monotonic() + timeout
            try:
                record = self._ssh_request(session, "stop", timeout=timeout,
                                           wait=max(0, min(3, timeout - 0.5)), max_output=0,
                                           source=session._ssh_kill_source)
                if record.get("state") not in {"running", "exited"}:
                    raise RuntimeError("Remote supervisor/receipt unavailable; stop is unconfirmed")
            except Exception as exc:
                session.remote_error = str(exc)
                return False
            remaining = max(0.0, deadline - time.monotonic())
            if not session._ssh_lock.acquire(timeout=remaining):
                session.remote_error = "Remote stop sent but refresh is still in flight; retry the same handle"
                return False
            timeout = max(0.0, deadline - time.monotonic())
        elif not session._ssh_lock.acquire(blocking=False):
            return False
        try:
            if session.exited:
                return True
            if action == "stop" and timeout <= 0:
                session.remote_error = "Remote stop sent but confirmation timed out; retry the same handle"
                return False
            return self._refresh_ssh_locked(session, action="inspect", timeout=timeout)
        finally:
            session._ssh_lock.release()

    def _refresh_ssh_locked(self, session, *, action, timeout):
        try:
            record = self._ssh_request(session, action, timeout=timeout,
                                       offset=session._ssh_offset, max_output=session.max_output_chars,
                                       wait=max(0, min(3, timeout - 0.5)), source=session._ssh_kill_source)
            if record.get("state") not in {"running", "exited"}:
                raise RuntimeError("Remote supervisor/receipt unavailable; process state is unknown")
            raw = base64.b64decode(record.get("output", ""), validate=True)
            if record.get("offset", session._ssh_offset) != session._ssh_offset:
                session._ssh_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            text = session._ssh_decoder.decode(raw, final=record["state"] == "exited")
            session._ssh_offset = record.get("size", session._ssh_offset)
            if text:
                self._ingest_output(session, text, unless_exited=True)
            if record.get("error") and record["state"] == "exited":
                self._ingest_output(session, f"{record['error']}\n", unless_exited=True)
            session.pid = record.get("pid", session.pid)
            session.remote_error = ""
            if record["state"] == "exited":
                reason = record.get("reason", "exited")
                source = record.get("source", session._ssh_kill_source) if reason == "killed" else ""
                self._finish_exited(session, record["exit_code"], reason=reason, source=source)
            return True
        except Exception as exc:
            logging.getLogger(__name__).debug("SSH process receipt unavailable", exc_info=True)
            session.remote_error = str(exc)
            return False

    def _kill_ssh(self, session, *, deadline=None):
        timeout = 5 if deadline is None else max(0, deadline - time.monotonic())
        if timeout <= 0 or not self._refresh_ssh(session, action="stop", timeout=timeout) or not session.exited:
            return {"status": "error", "session_id": session.id, "process_running": not session.exited,
                    "error": session.remote_error or "Remote stop is unconfirmed; retry the same handle",
                    **process_provenance(session)}
        return {"status": "killed" if session.completion_reason == "killed" else "already_exited",
                "session_id": session.id, **self._exit_snapshot(session, "killed" if session.completion_reason == "killed"
                                                               else "already_exited"),
                **process_provenance(session)}

    def _recover_ssh(self, entry):
        from tools.process_registry import ProcessSession, _CHECKPOINT_FIELDS, _CHECKPOINT_DEFAULTS
        fields = {key: entry.get(key, _CHECKPOINT_DEFAULTS[key]) for key in _CHECKPOINT_FIELDS}
        fields["ssh_binding"] = restore_binding(fields["ssh_binding"])
        if fields["ssh_binding"] is None:
            raise ValueError("SSH checkpoint has no immutable node/run identity")
        session = ProcessSession(id=entry["session_id"], detached=True, **fields)
        with self._lock:
            self._running[session.id] = session
        self._start_ssh_poller(session)
        return session


def handle_process(args, **kw):
    from tools import process_registry as source
    from tools.nodes import NodeError, resolve_node
    from tools.registry import tool_error
    from tools.process_registry_wait_policy import apply_wait_poll_policy

    action = args.get("action", "")
    session_id = str(args.get("session_id", "")) if args.get("session_id") is not None else ""
    node = None
    session = None
    try:
        # An omitted node must not resolve today's configured default. The
        # original connection is part of the handle, including after recovery.
        if "node" in args:
            from tools.terminal_scope import enforce_no_refusal, TerminalPolicyUnavailable
            try:
                enforce_no_refusal()
            except TerminalPolicyUnavailable as exc:
                raise NodeError(str(exc)) from exc
            if not isinstance(args["node"], str) or not args["node"]:
                raise NodeError("node must be a nonempty configured name")
            node = resolve_node(args["node"])
        if action not in {*source._SESSION_ACTIONS, "list", "handoff"}:
            raise NodeError(f"Unknown process action: {action}. Use: list, poll, log, wait, kill, write, submit, close, handoff")
        if action == "list":
            result = source._list_processes(kw.get("task_id"), **({"node": node} if node is not None else {}))
            if node is not None:
                result["processes"] = [item for item in result["processes"]
                                       if item.get("node") == node.name and item.get("ssh") == node.ssh]
                result.update(node=node.name, ssh=node.ssh)
            return json.dumps(result, ensure_ascii=False)
        if not session_id:
            raise NodeError(f"session_id is required for {action}")
        session = source.process_registry._lookup_session(session_id)
        if session is not None and session.ssh_binding is not None:
            from hermes_constants import get_hermes_home
            if session.ssh_binding.profile_home != str(get_hermes_home()):
                raise NodeError("Process handle belongs to a different profile")
            from tools.terminal_scope import enforce_no_refusal, TerminalPolicyUnavailable
            try:
                enforce_no_refusal()
            except TerminalPolicyUnavailable as exc:
                raise NodeError(str(exc)) from exc
        if node is not None and session is not None:
            binding = session.ssh_binding
            if binding is None or (binding.node_name, binding.ssh, binding.platform) != (node.name, node.ssh, node.platform):
                raise NodeError("Explicit node conflicts with the process handle's original node/SSH identity")
        try:
            if action == "handoff":
                result = source._handoff_process(session_id, args, kw.get("task_id"))
            elif action in source._SESSION_ACTIONS:
                handler, redact = source._SESSION_ACTIONS[action]
                result = apply_wait_poll_policy(action, session_id, handler(session_id, args))
                if redact:
                    result = source._redact_process_result(result)
            else:
                return tool_error(f"Unknown process action: {action}. Use: list, poll, log, wait, kill, write, submit, close, handoff")
        finally:
            if session is not None and session.ssh_binding is not None:
                from tools.node_execution import record_result_environment, UnavailableResultEnvironment
                from tools.nodes import Node
                binding = session.ssh_binding
                # Capturing an archive owner must not connect before an action
                # can report cached state or an unknown remote outcome.
                record_result_environment(session.env_ref or UnavailableResultEnvironment(
                    Node(binding.node_name, binding.ssh, binding.platform)))
    except NodeError as exc:
        result = {"status": "error", "error": str(exc)}
    if node is not None:
        result.update(node=node.name, ssh=node.ssh)
    if session is not None:
        # For a conflict, identify the actual owner rather than falsely naming
        # the requested node as the process's execution target.
        result.update(process_provenance(session))
    elif "node" in args and node is None:
        result["node"] = args["node"]
    return json.dumps(result, ensure_ascii=False)
