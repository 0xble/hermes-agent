"""Session-persistent kernels for REMOTE terminal backends (docker/ssh/modal).

Remote backends offer one primitive — ``env.execute(cmd)``, run-to-completion
— so the three things the local kernel gets from owning a child are rebuilt:
a detached runner (``nohup ... &``, PID recorded, ``kill -0`` probed per cell);
a file-based CELL protocol in the kernel dir (``cell_req_NNNNNN.json`` /
``cell_res_NNNNNN.json``), sibling to the unchanged file-based TOOL-RPC protocol
(req_/res_) whose host-side ``_rpc_poll_loop`` starts per cell with the calling
thread's context (= per-cell tool authority); and death detection — a failed
liveness probe reads as *kernel died: state lost* and the next call respawns,
never a hung poll (every wait is bounded by the cell timeout).

Same invariants as local: owner = approval session key with the ``::child::``
qualifier (one resolver in tools.code_kernel), same generated tool stubs, same
output post-processing in the caller. ``reset=true`` kills and respawns. Spawn
failure fails OPEN to the per-call path with a note.
"""
from __future__ import annotations

import atexit
import json
import logging
import secrets
import shlex
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from tools.code_kernel import RUNNER_CELL_SOURCE, KernelRegistry, _current_caller, _log_kernel_lifecycle

logger = logging.getLogger(__name__)

# Host poll interval for a cell result file; each poll is one env.execute round-trip
# (0.1-0.4s on ssh/docker), so this is a floor, not a rate.
_CELL_POLL_INTERVAL = 0.5
# While a cell's result is missing, how often to check whether the runner is still there.
_CELL_LIVENESS_INTERVAL = 5.0

# The remote runner: a forever-loop that polls for cell request files, execs them in one
# persistent namespace, writes response files. Pure files + stdlib only (transport-agnostic);
# cells and tool-RPC share the kernel dir under distinct prefixes.
REMOTE_KERNEL_RUNNER_SOURCE = '''\
"""Auto-generated Hermes REMOTE session-kernel runner (file cell protocol)."""
import contextlib
import io
import json
import os
import sys
import time
import traceback

KDIR = os.environ["HERMES_KERNEL_DIR"]
CELLS = os.path.join(KDIR, "cells")
_CAPTURE_LIMIT = {capture_limit}
IDLE_EXIT_SECONDS = {idle_exit}

{cell_source}

def main():
    execution_count = 0
    last_activity = time.time()
    while True:
        pending = sorted(
            f for f in os.listdir(CELLS)
            if f.startswith("cell_req_") and f.endswith(".json")
        )
        if not pending:
            if time.time() - last_activity > IDLE_EXIT_SECONDS:
                return  # self-reap: nobody is talking to us anymore
            time.sleep(0.2)
            continue
        for name in pending:
            req_path = os.path.join(CELLS, name)
            try:
                with open(req_path, "r", encoding="utf-8") as f:
                    request = json.load(f)
            except Exception:
                # Partially-written request (ship in progress): retry next tick.
                continue
            try:
                os.remove(req_path)
            except FileNotFoundError:
                # The host reclaimed this request and told the agent it did not
                # run: it must never run. Removal is the claim, so fail closed.
                continue
            last_activity = time.time()
            execution_count += 1
            payload, _ = run_cell(request, execution_count)
            res_name = name.replace("cell_req_", "cell_res_")
            tmp = os.path.join(CELLS, res_name + ".tmp")
            # Cell results carry the executed code's output: owner-only, even if
            # the process umask is permissive.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, os.path.join(CELLS, res_name))
            # Idle is measured from the last result, so a cell longer than the
            # idle window cannot trigger self-exit right after it answers.
            last_activity = time.time()
            if payload["status"] == "exit":
                return


if __name__ == "__main__":
    main()
'''


def _sh(env, cmd: str, timeout: int = 15) -> str:
    """Run *cmd* on the remote from ``/`` and return its output text."""
    result = env.execute(cmd, cwd="/", timeout=timeout)
    return (result.get("output", "") if isinstance(result, dict) else "") or ""


@dataclass
class RemoteKernel:
    """Host-side record of one detached remote kernel process."""

    env: Any
    env_type: str
    kernel_dir: str
    pid: str
    rpc_token: str
    owner: str
    last_used: float = field(default_factory=time.monotonic)
    execution_count: int = 0
    cell_seq: int = 0
    # Cells currently running on this kernel. Reap/evict skip attached
    # kernels: killing one mid-cell tears the runner out from under a live
    # poll loop (same guard as tools.code_kernel, hermes-agent#101861).
    attached: int = 0
    # Owned by a live delegate_task child: exempt from LRU eviction (the child's teardown disposes it).
    pinned: bool = False
    # One record is registered before any transport work. The cell lock spans acquisition
    # and execution, so no second poll loop can borrow the first cell's authority.
    lock: Any = field(default_factory=threading.Lock, repr=False)
    lifecycle_lock: Any = field(default_factory=threading.RLock, repr=False)
    stop_event: Any = field(default_factory=threading.Event, repr=False)
    closed: bool = False
    teardown_claimed: bool = False
    kernel_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    current_task: str = ""
    current_caller: str = ""
    retire_reason: str = "teardown"

    def sh(self, cmd: str, timeout: int = 15) -> str:
        return _sh(self.env, cmd, timeout)

    def is_alive(self) -> bool:
        """Bounded liveness probe: kill -0 through the transport. Any transport
        failure counts as dead — a dropped ssh connection and a dead runner are
        indistinguishable from here, and both have the same correct answer (respawn)."""
        try:
            return "ALIVE" in self.sh(f"kill -0 {shlex.quote(self.pid)} 2>/dev/null && echo ALIVE")
        except Exception:
            return False

    def kill(self) -> None:
        """Signal pending spawn before waiting, and retire the eventual process exactly once."""
        self.stop_event.set()
        with self.lifecycle_lock:
            if self.teardown_claimed:
                return
            self.teardown_claimed = self.closed = True
            pid, kernel_dir = self.pid, self.kernel_dir
        _log_kernel_lifecycle(self, "retire", task_id=self.current_task,
                              caller=self.current_caller, reason=self.retire_reason)
        commands = []
        if pid:
            q_pid = shlex.quote(pid)
            commands.append(f"pkill -TERM -P {q_pid} 2>/dev/null; kill {q_pid} 2>/dev/null; true")
        if kernel_dir:
            commands.append(f"rm -rf {shlex.quote(kernel_dir)}")
        for cmd in commands:
            try:
                self.sh(cmd)
            except Exception:  # health: allow BLE001 -- best-effort transport cleanup must not log tokens or output from exceptions.
                logger.debug("remote kernel cleanup failed (details omitted)")


# The host reaper skips child-pinned kernels, so a pinned runner gets a longer self-exit window
# instead of none: a gateway killed without teardown must not leave it on the remote host forever.
PINNED_RUNNER_IDLE_FACTOR = 4


def _runner_idle_exit(idle_exit: int, pinned: bool) -> int:
    return idle_exit * PINNED_RUNNER_IDLE_FACTOR if pinned else idle_exit


def _kernel_key(owner: str, env_type: str, task_env_id: str, sandbox_tools: frozenset) -> tuple:
    """The hermes_tools stub module is generated from ``sandbox_tools`` once, at spawn, so a kernel
    is only reusable by calls with the SAME tool set; a different set gets its own kernel."""
    return (owner, "remote", env_type, task_env_id, tuple(sorted(sandbox_tools)))


# Registry + lock shared-shape with code_kernel; teardown runs outside the lock.
_REGISTRY = KernelRegistry(lambda kernel: kernel.kill())
_REMOTE_KERNELS: dict[tuple, RemoteKernel] = _REGISTRY.kernels


def shutdown_all_remote_kernels() -> None:
    _REGISTRY.shutdown()


def shutdown_remote_kernels_for_owner(owner: str) -> None:
    """Session-boundary disposal — wired to the same clear_session hook as
    local kernels, so /new and session close reap both kinds."""
    if owner:
        _REGISTRY.shutdown(owner)


def shutdown_remote_kernels_where(owner_matches: Callable[[str], bool]) -> None:
    """Dispose every remote kernel whose owner satisfies the predicate (a finished child's kernels)."""
    _REGISTRY.shutdown(owner_matches=owner_matches)


def _reap_unlocked(idle_timeout: int) -> list[RemoteKernel]:
    """Pop idle-expired, unattached remote kernels; caller tears them down outside the lock. The
    runner self-exits after the same idle window, so this clears the HOST-side entry — without it
    the map grew one entry per never-revisited (owner, env_type, task_env_id) for the gateway's life."""
    return _REGISTRY.pop_idle_unlocked(time.monotonic(), idle_timeout)


def _evict_over_cap_unlocked(cap: int) -> list[RemoteKernel]:
    return _REGISTRY.pop_over_cap_unlocked(cap)


def reap_remote_kernels() -> None:
    """Use the existing host reaper, never a second remote polling/reaping thread."""
    from tools.code_kernel import _lifecycle_limits
    cap, idle_timeout = _lifecycle_limits()
    with _REGISTRY.lock:
        doomed = _reap_unlocked(idle_timeout) + _evict_over_cap_unlocked(cap)
    for kernel in doomed:
        kernel.kill()


atexit.register(shutdown_all_remote_kernels)


def _spawn_remote_kernel(env, env_type: str, owner: str, task_env_id: str,
                         sandbox_tools: frozenset, *, idle_exit: int) -> Optional[RemoteKernel]:
    """Start a detached kernel runner on the remote. None on failure (dir removed)."""
    from tools.code_execution_rpc import _execute_checked, _private_dirs_cmd
    from tools.code_execution_tool import (
        MAX_STDOUT_BYTES, _ship_file_to_remote, _env_temp_dir,
        _ship_env_file_and_launch, generate_hermes_tools_module,
    )
    kernel_dir = f"{_env_temp_dir(env)}/hermes_rkernel_{uuid.uuid4().hex[:12]}"
    q_dir = shlex.quote(kernel_dir)
    kernel = None
    candidate = None
    try:
        # Private dirs: the kernel dir lives under a shared temp dir and carries
        # the RPC token (in req files), tool results, and cell code/output.
        # Fail closed on setup failure rather than ship secrets into a dir that
        # stayed permissive.
        _execute_checked(env, _private_dirs_cmd(kernel_dir, f"{kernel_dir}/cells",
                                                f"{kernel_dir}/rpc"),
                         "remote kernel dir setup", timeout=15)
        rpc_token = secrets.token_urlsafe(32)
        _ship_file_to_remote(env, f"{kernel_dir}/kernel_runner.py", REMOTE_KERNEL_RUNNER_SOURCE.format(
            cell_source=RUNNER_CELL_SOURCE, capture_limit=MAX_STDOUT_BYTES, idle_exit=idle_exit))
        _ship_file_to_remote(env, f"{kernel_dir}/hermes_tools.py",
                             generate_hermes_tools_module(list(sandbox_tools), transport="file"))
        # kernel.env is removed after sourcing: the runner's env keeps the
        # values, so the token file need not sit at rest for the kernel's
        # lifetime. runner.log is pre-created 600 so the launch redirect never
        # lands at the remote's default umask. The inner `&` stays inside the
        # subshell where `$!` resolves to the runner pid.
        launch_cmd = _ship_env_file_and_launch(
            env, kernel_dir, "kernel.env",
            "rm -f ./kernel.env && touch runner.log && chmod 600 runner.log && "
            '{ nohup python3 kernel_runner.py > runner.log 2>&1 & echo "PID:$!"; }',
            rpc_dir=f"{kernel_dir}/rpc", rpc_token=rpc_token,
            HERMES_KERNEL_DIR=kernel_dir, PYTHONPATH=kernel_dir)
        started = _sh(env, launch_cmd, timeout=20)
        pid = next((line.strip()[4:].strip() for line in started.splitlines()
                    if line.strip().startswith("PID:")), "")
        if not pid.isdigit():
            logger.warning("remote kernel spawn returned no PID (output omitted)")
        else:
            candidate = RemoteKernel(env=env, env_type=env_type, kernel_dir=kernel_dir,
                                     pid=pid, rpc_token=rpc_token, owner=owner,
                                     current_task=task_env_id, current_caller=_current_caller(task_env_id))
            if candidate.is_alive():
                kernel = candidate
            else:
                logger.warning("remote kernel died at spawn (output omitted)")
                candidate.retire_reason = "spawn_failed"
                candidate.kill()
                return None
    except Exception:
        logger.warning("remote kernel spawn failed (details omitted)")
        if candidate is not None:
            candidate.retire_reason = "spawn_failed"
            candidate.kill()
            return None
    if kernel is None:
        try:
            _sh(env, f"rm -rf {q_dir}")
        except Exception:
            pass
    return kernel


def _attach_remote_kernel(env, env_type, owner, task_env_id, sandbox_tools, *, reset, idle_exit):
    from agent.delegation_context import is_delegated_child_context
    from tools.code_kernel import _lifecycle_limits
    cap, _ = _lifecycle_limits()
    key = _kernel_key(owner, env_type, task_env_id, sandbox_tools)
    with _REGISTRY.lock:
        expired = _reap_unlocked(idle_exit)
        kernel = _REMOTE_KERNELS.get(key)
        state_reset = reset and kernel is not None
        if state_reset:
            dropped = _REMOTE_KERNELS.pop(key)
            dropped.retire_reason = "reset"
            if not dropped.attached:
                expired.append(dropped)
            kernel = None
        if kernel is None:
            kernel = RemoteKernel(env, env_type, "", "", "", owner)
            kernel.pinned = is_delegated_child_context()
            _REMOTE_KERNELS[key] = kernel
        kernel.attached += 1
        kernel.last_used = time.monotonic()
        expired.extend(_evict_over_cap_unlocked(cap))
    for doomed in expired:
        doomed.kill()
    return key, kernel, state_reset


def _acquire_remote_kernel(env, env_type: str, owner: str, task_env_id: str,
                           sandbox_tools: frozenset, *, reset: bool,
                           idle_exit: int) -> tuple[Optional[RemoteKernel], bool, bool, bool]:
    """Return an attached, cell-locked record. Caller releases it after the cell settles.

    Slow probes/spawn hold only this record's lifetime fence. Membership is established
    first, so shutdown can signal a pending spawn and no concurrent caller can launch twice.
    """
    from tools.code_kernel import _lifecycle_limits, _ensure_background_reaper
    state_lost = state_reset = False
    while True:
        key, kernel, was_reset = _attach_remote_kernel(
            env, env_type, owner, task_env_id, sandbox_tools, reset=reset, idle_exit=idle_exit)
        state_reset = state_reset or was_reset
        reset = False
        kernel.lock.acquire()
        selected = False
        try:
            with kernel.lifecycle_lock:
                kernel.current_task, kernel.current_caller = task_env_id, _current_caller(task_env_id)
                if kernel.closed or kernel.stop_event.is_set():
                    return None, False, state_reset, state_lost
                reused = bool(kernel.pid)
                if reused and not kernel.is_alive():
                    state_lost = True
                    _REGISTRY.discard(key, kernel, reason="process_lost")
                    continue
                if not reused:
                    candidate = _spawn_remote_kernel(
                        env, env_type, owner, task_env_id, sandbox_tools,
                        idle_exit=_runner_idle_exit(idle_exit, kernel.pinned))
                    if candidate is None:
                        _REGISTRY.discard(key, kernel, reason="spawn_failed")
                        return None, False, state_reset, state_lost
                    kernel.pid, kernel.kernel_dir, kernel.rpc_token = candidate.pid, candidate.kernel_dir, candidate.rpc_token
                    _log_kernel_lifecycle(kernel, "start", task_id=task_env_id,
                                          caller=kernel.current_caller, reason="spawn")
                if kernel.stop_event.is_set():
                    return None, False, state_reset, state_lost
            _ensure_background_reaper()
            selected = True
            return kernel, reused, state_reset, state_lost
        finally:
            if not selected:
                kernel.lock.release()
                cap, _ = _lifecycle_limits()
                _REGISTRY.detach(key, kernel, cap)


def _run_remote_cell(kernel: RemoteKernel, code: str, timeout: int) -> tuple[str, dict[str, Any]]:
    """Ship one cell request and poll for its result: (cell status, payload)."""
    from tools.code_execution_tool import _ship_file_to_remote
    kernel.cell_seq += 1
    seq = f"{kernel.cell_seq:06d}"
    q_cells, q_res = shlex.quote(f"{kernel.kernel_dir}/cells"), shlex.quote(f"cell_res_{seq}.json")
    # One round-trip: tmp write + rename publishes the request atomically.
    _ship_file_to_remote(kernel.env, f"{kernel.kernel_dir}/cells/cell_req_{seq}.json",
                         json.dumps({"id": seq, "code": code}, ensure_ascii=False), atomic=True)
    q_req = shlex.quote(f"{kernel.kernel_dir}/cells/cell_req_{seq}.json")

    def read_result() -> Optional[tuple[str, dict[str, Any]]]:
        try:
            body = kernel.sh(f"cat {q_cells}/{q_res} 2>/dev/null", timeout=20).strip()
        except Exception:  # health: allow BLE001 -- one flaky transport round-trip is not kernel death; liveness decides.
            # One flaky round-trip is not kernel death; liveness decides.
            return None
        if not body:
            return None
        try:
            payload = json.loads(body)
            status = payload.get("status", "error")
        except ValueError:
            payload, status = {}, "protocol-error"
        try:
            kernel.sh(f"rm -f {q_cells}/{q_res}", timeout=10)
        except Exception:  # health: allow BLE001 -- best-effort result cleanup must not expose cell output in transport exceptions.
            # Best-effort: the cell already ran, so raising here would send
            # the caller to its per-call fallback and run the code twice.
            # A leftover result file is harmless (seq is monotonic).
            logger.debug("remote kernel: cell result cleanup failed (details omitted)")
        return status, payload

    deadline = time.monotonic() + timeout
    next_liveness = time.monotonic() + _CELL_LIVENESS_INTERVAL
    while time.monotonic() < deadline:
        if time.monotonic() >= next_liveness:
            next_liveness = time.monotonic() + _CELL_LIVENESS_INTERVAL
            verdict = _probe_unanswered_cell(kernel, q_req, only_if_dead=True)
            if verdict == "reclaimed":
                return "not-started", {}
            if verdict == "gone":  # the runner took the cell, then exited
                return read_result() or ("died", {})
        if (answered := read_result()) is not None:
            return answered
        time.sleep(_CELL_POLL_INTERVAL)
    if _probe_unanswered_cell(kernel, q_req, only_if_dead=False) == "reclaimed":
        return "not-picked-up", {}
    return "timeout", {}


def _reclaim_command(q_req: str, pid: str, *, only_if_dead: bool) -> str:
    """Shell probe: ``RECLAIMED`` after renaming an unclaimed request, ``GONE`` when the
    runner is dead but the request was already taken, ``ALIVE`` while the runner lives."""
    reclaim = f"mv {q_req} {q_req}.cancelled 2>/dev/null && echo RECLAIMED"
    if not only_if_dead:
        return reclaim
    return f"if kill -0 {shlex.quote(pid)} 2>/dev/null; then echo ALIVE; else {reclaim} || echo GONE; fi"


def _probe_unanswered_cell(kernel: RemoteKernel, q_req: str, *, only_if_dead: bool) -> Optional[str]:
    """``reclaimed`` proves the cell never started, ``gone`` that its runner died mid-cell.

    The runner removes a request before executing it, fails closed if the file is gone,
    and only scans ``cell_req_*.json``. A successful rename therefore means the cell
    never started and never will. Transport failures prove nothing and return ``None``.
    """
    try:
        out = kernel.sh(_reclaim_command(q_req, kernel.pid, only_if_dead=only_if_dead), timeout=15)
    except Exception:  # health: allow BLE001 -- an unreadable transport leaves the cell's fate unknown, never "not started".
        logger.debug("remote kernel: unstarted-cell reclaim probe failed (details omitted)")
        return None
    if "RECLAIMED" in out:
        return "reclaimed"
    return "gone" if "GONE" in out else None


def execute_in_remote_kernel(
    code: str, *, env, env_type: str, task_env_id: str, sandbox_tools: frozenset,
    timeout: int, max_tool_calls: int, reset: bool, idle_exit: int = 1800,
) -> Optional[dict[str, Any]]:
    """Run one cell in the owner's remote kernel. Returns the raw cell result dict (caller
    post-processes output), or ``None`` when no kernel could be spawned (caller falls open to
    per-call). ``state_lost``/``state_reset``/``reused`` ride in the ``kernel`` sub-dict."""
    from tools.code_kernel import _resolve_owner
    owner = _resolve_owner(task_env_id)
    kernel, reused, state_reset, state_lost = _acquire_remote_kernel(
        env, env_type, owner, task_env_id, sandbox_tools, reset=reset, idle_exit=idle_exit)
    if kernel is None:
        return None  # fail open to per-call
    key = _kernel_key(owner, env_type, task_env_id, sandbox_tools)
    started = False
    try:
        if kernel.stop_event.is_set():
            return None
        _log_kernel_lifecycle(kernel, "cell_start", task_id=task_env_id,
                              caller=kernel.current_caller, reason="execute")
        started = True
        return _run_attached_cell(kernel, key, code, env=env, task_env_id=task_env_id,
                                  sandbox_tools=sandbox_tools, timeout=timeout, max_tool_calls=max_tool_calls,
                                  reused=reused, state_reset=state_reset, state_lost=state_lost)
    except Exception:  # health: allow BLE001 -- ambiguous cell failures return errors without replay or logging code/transport secrets.
        # Shipping/reading may have succeeded remotely even when its transport failed.
        # Return an error, never the spawn-only fail-open sentinel that would replay code.
        logger.warning("remote kernel cell failed; execution state unknown (details omitted)")
        _REGISTRY.discard(key, kernel, reason="cell_failed")
        from tools.code_execution_tool import _error_result
        body = json.loads(_error_result(
            "Remote kernel failed and was retired. The cell may have run; do not retry it automatically."))
        body["kernel"] = {"remote": True, "reused": reused,
                          "ended": True, "state_lost": True}
        if state_reset:
            body["kernel"]["state_reset"] = True
        return body
    finally:
        if started:
            _log_kernel_lifecycle(kernel, "cell_end", task_id=task_env_id,
                                  caller=kernel.current_caller, reason="settled")
        kernel.lock.release()
        from tools.code_kernel import _lifecycle_limits
        cap, _ = _lifecycle_limits()
        _REGISTRY.detach(key, kernel, cap)


def _run_attached_cell(kernel: RemoteKernel, key: tuple, code: str, *, env, task_env_id: str,
                       sandbox_tools: frozenset, timeout: int, max_tool_calls: int,
                       reused: bool, state_reset: bool, state_lost: bool) -> dict[str, Any]:
    from tools.code_execution_rpc import tool_errors_since
    from tools.code_execution_tool import _rpc_poll_loop
    from tools.thread_context import propagate_context_to_thread
    # Clean stale tool-RPC requests from a previous cell before arming this cell's poll loop, so
    # a background thread the last cell leaked cannot smuggle a call into this authority window.
    q_rpc = shlex.quote(kernel.kernel_dir + '/rpc')
    try:
        kernel.sh(f"rm -f {q_rpc}/req_* {q_rpc}/res_*", timeout=10)
    except Exception:
        pass
    tool_call_counter, tool_call_log, stop_event = [0], [], threading.Event()
    # Per-cell RPC thread carrying THIS call's approval/session context — the remote analogue
    # of CellAuthority: authority lives exactly as long as the cell's poll loop.
    rpc_thread = threading.Thread(
        target=propagate_context_to_thread(_rpc_poll_loop), daemon=True,
        args=(env, f"{kernel.kernel_dir}/rpc", task_env_id, tool_call_log, tool_call_counter,
              max_tool_calls, sandbox_tools, stop_event, kernel.rpc_token))
    rpc_thread.start()
    cell_status, cell_payload = "no-result", {}
    try:
        cell_status, cell_payload = _run_remote_cell(kernel, code, timeout)
    finally:
        stop_event.set()
        rpc_thread.join(timeout=5)
    kernel_info: dict[str, Any] = {"reused": reused, "remote": True}
    result: dict[str, Any] = {
        "status": "error", "stdout": cell_payload.get("stdout", ""), "stderr": cell_payload.get("stderr", ""),
        "traceback": cell_payload.get("traceback", ""), "tool_calls_made": tool_call_counter[0], "kernel": kernel_info,
        "tool_errors": tool_errors_since(tool_call_log),
    }
    if cell_status in ("not-started", "not-picked-up"):
        _REGISTRY.discard(key, kernel, reason=cell_status)
        why = ("exited before this cell started" if cell_status == "not-started"
               else "did not pick up this cell before the timeout and was killed")
        result["error"] = f"The remote session kernel {why}. The code did NOT run and is safe to retry."
        note = result["error"] + " Earlier kernel state was lost; the next call starts a fresh kernel."
        if tool_call_counter[0]:
            note += (f" {tool_call_counter[0]} tool call(s) were made during the wait by a background thread "
                     "left by an earlier cell, not by this code.")
        kernel_info.update(ended=True, state_lost=True, note=note)
        return result
    if cell_status == "died":
        _REGISTRY.discard(key, kernel, reason="runner_died")
        result["error"] = ("The remote session kernel died while running this cell. The code may have run "
                           "partially; do not retry it automatically.")
        kernel_info.update(ended=True, state_lost=True, note=result["error"])
        return result
    if cell_status in ("timeout", "protocol-error", "no-result"):
        # No safe way to interrupt one cell in place (same contract as local): kill, report, respawn.
        _REGISTRY.discard(key, kernel, reason=cell_status)
        if cell_status == "timeout":
            result["status"] = "timeout"
        kernel_info.update(ended=True, state_lost=True, note=(
            "Cell timed out; the remote session kernel was killed and its state was lost. The next call "
            "starts a fresh kernel." if cell_status == "timeout"
            else "Remote kernel protocol failure; kernel killed, state lost."))
        return result
    if cell_status == "exit":
        _REGISTRY.discard(key, kernel, reason="cell_exit")
        kernel_info["ended"] = True
    kernel.execution_count = kernel_info["execution_count"] = int(cell_payload.get("execution_count", 0) or 0)
    if cell_status in ("ok", "exit"):
        result["status"] = "success"
    result["stdout_clipped"] = bool(cell_payload.get("stdout_clipped"))
    result["stderr_clipped"] = bool(cell_payload.get("stderr_clipped"))
    if state_reset:
        kernel_info["state_reset"] = True
    if state_lost:
        kernel_info.update(state_lost=True, note=(
            "The previous remote kernel was gone (transport drop, container "
            "restart, or idle self-exit); state from earlier calls was lost and a fresh kernel was started."))
    if cell_status == "error" and result["traceback"]:
        result["error"] = result["traceback"].strip().splitlines()[-1]
    return result
