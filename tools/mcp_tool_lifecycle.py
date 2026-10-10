"""MCP process lifecycle: stdio child PID tracking and orphan cleanup, graceful
server shutdown and draining of the background MCP loop."""

import logging
import asyncio
import os
import time
from typing import Dict, Optional
from tools.mcp_tool_common import _core
from tools import mcp_tool_loop as _loop

logger = logging.getLogger("tools.mcp_tool")

# Live stdio MCP children (pid -> server_name), added after connection and removed on normal
# shutdown, so they can be force-killed if SDK teardown fails.
_stdio_pids: Dict[int, str] = {}
# PIDs that survived their session context exit (detected in _run_stdio's finally, reaped by
# _kill_orphaned_mcp_children). Separate from _stdio_pids so sweeps never race active sessions.
_orphan_stdio_pids: set = set()
_orphan_stdio_pid_servers: Dict[int, str] = {}
# pid -> pgid captured at spawn. The SDK spawns with start_new_session=True (PGID == PID);
# grandchildren keep that PGID after the direct child exits, so killpg still reaches them.
# Separate from _stdio_pids so the PGID survives the child's removal. Empty on Windows.
_stdio_pgids: Dict[int, int] = {}
# POSIX session IDs captured with the PGID. A surviving member in the original session
# proves a recorded group was not recycled into an unrelated session.
_stdio_sessions: Dict[int, int] = {}
# psutil handles cache (PID, create-time) at spawn; retain descendant witnesses
# after the direct child exits. A numeric PID/PGID alone is never signal authority.
_stdio_processes: dict[int, dict] = {}


def _mcp_process_state(proc):
    """Return True/False/None for alive/dead/unverifiable incarnation state."""
    import psutil
    if proc is None:
        return False
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, ProcessLookupError):
        return False
    except (psutil.AccessDenied, PermissionError, OSError):
        return None


def _mcp_process_alive(proc) -> bool:
    return _mcp_process_state(proc) is True


def _mcp_process_in_group(proc, pgid: int) -> bool:
    try:
        return _mcp_process_alive(proc) and os.getpgid(proc.pid) == pgid
    except (AttributeError, OSError):
        return False


def _remember_mcp_processes(pids: set) -> None:
    """Capture incarnation handles while the newly spawned children are ours."""
    import psutil
    for pid in pids:
        try:
            proc = psutil.Process(pid)
            proc.create_time()  # require a readable birth identity, not a PID-only handle
            members = {proc.pid: proc}
            for child in proc.children(recursive=True):
                try:
                    child.create_time()
                    members[child.pid] = child
                except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                    continue
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
        with _core._lock:
            _stdio_processes[pid] = members


class _McpGroupScan(dict):
    """Live group members plus a distinction for a positively foreign group."""

    def __init__(self, members=None, *, foreign: bool = False):
        super().__init__(members or {})
        self.foreign = foreign


def _enumerate_mcp_group(pgid: int, session_id: Optional[int]) -> Optional[dict]:
    """Return live members, or ``None`` when group identity is unverifiable.

    A recorded MCP group is safe to signal after its leader exits only when every
    member we can inspect is still in the original session.  A recycled group has
    a different session (and normally a new leader incarnation), so it is rejected
    rather than adopted from its numeric PGID.  The empty result for a positively
    foreign group carries ``foreign=True`` so its supervisor registration can be
    released without confusing it with an unreadable group.
    """
    import psutil
    discovered = {}
    for proc in psutil.process_iter():
        try:
            if os.getpgid(proc.pid) != pgid:
                continue
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                continue
            if session_id is not None and os.getsid(proc.pid) != session_id:
                return _McpGroupScan(foreign=True)
            proc.create_time()
            discovered[proc.pid] = proc
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            # An inaccessible member could be the recycled process we must not
            # signal.  Fail closed only after it has matched the target group.
            try:
                if os.getpgid(proc.pid) == pgid:
                    return None
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        except PermissionError:
            return None
        except OSError:
            continue
    return discovered


def _owned_mcp_processes(members: dict, pgid: Optional[int], my_pgid: Optional[int],
                         *, leader_pid: Optional[int] = None,
                         session_id: Optional[int] = None,
                         allow_unwitnessed_session: bool = False) -> dict:
    """Refresh descendants/groups through verified process incarnations.

    A live witness allows a group refresh.  Once any process incarnation has
    been recorded, a refresh requires a live verified witness.  The release path
    may use the recorded-session check once to discover a late-spawned member;
    the reaper never uses it after a recorded incarnation has gone away.
    """
    import psutil
    alive = {pid: proc for pid, proc in members.items() if _mcp_process_alive(proc)}
    if pgid is not None and pgid != my_pgid:
        witnesses = [proc for proc in alive.values() if _mcp_process_in_group(proc, pgid)]
        if witnesses or (session_id is not None and (allow_unwitnessed_session or not members)):
            discovered = _enumerate_mcp_group(pgid, session_id)
            if discovered is None or getattr(discovered, "foreign", False):
                return {}
            if allow_unwitnessed_session and session_id is not None and leader_pid is not None:
                leader = members.get(leader_pid)
                if leader is not None and pgid in discovered:
                    try:
                        if discovered[pgid].create_time() != leader.create_time():
                            return {}
                    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                        return {}
            # A live witness keeps the enumeration tied to the original group.
            # The release-only session check is allowed to discover late members,
            # but the reaper never adopts them without a recorded live witness.
            if witnesses:
                if not any(_mcp_process_in_group(proc, pgid) for proc in witnesses):
                    return {}
                alive.update(discovered)
            elif discovered:
                alive = discovered
    elif alive:
        for proc in list(alive.values()):
            try:
                for child in proc.children(recursive=True):
                    child.create_time()
                    if _mcp_process_alive(child):
                        alive[child.pid] = child
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
    return alive


def _snapshot_child_pids() -> set:
    """Current direct-child PIDs: /proc on Linux, else psutil, else empty set."""
    my_pid = os.getpid()
    # /proc/<pid>/task/<tid>/children is per-THREAD, and stdio_client() spawns from the MCP
    # loop thread, so union every task's children — reading only the main thread's file
    # returns an empty set on every Linux install.
    try:
        # ``/proc/<pid>/task/<tid>/children`` is per-THREAD — a child forked from thread T is listed only
        # under T's task dir. stdio_client() spawns from the background MCP loop thread, so reading only the
        # main thread's file (``task/<pid>/children``) returned an empty set on every Linux install and left
        # ``_stdio_child_pids`` / ``_stdio_pids`` empty: the #81995 dead-child fast-fail, the #96452 respawn
        # signal, and the killpg shutdown sweep never saw the subprocess.
        task_dir = f"/proc/{my_pid}/task"
        found: set = set()
        for tid in os.listdir(task_dir):
            try:
                with open(f"{task_dir}/{tid}/children", encoding="utf-8-sig") as f:
                    found.update(int(p) for p in f.read().split() if p.strip())
            except (FileNotFoundError, OSError, ValueError):
                continue  # thread exited between listdir and open
        return found
    except (FileNotFoundError, OSError, ValueError):
        pass
    try:
        import psutil
        return {c.pid for c in psutil.Process(my_pid).children()}
    except Exception:
        return set()


# argv markers of non-MCP gateway children that can race into the snapshot delta during an
# MCP spawn (defense-in-depth; LSP/slash_worker already use start_new_session). Matched against
# argv[1:] because Python/Java children start with the interpreter path.
_NON_MCP_CHILD_CMDLINE_MARKERS: tuple[str, ...] = (
    "tui_gateway.slash_worker", "tui_gateway.entry",
    "-dorg.eclipse.equinox.launcher", "eclipse.jdt.ls", "org.eclipse.equinox.launcher_",  # jdtls
)


def _filter_mcp_children(pids: set) -> set:
    """Drop non-MCP children from a PID snapshot delta. Tracking a stray child in _stdio_pgids
    is catastrophic if it lacks start_new_session: its pgid can be the TUI parent's, so the
    shutdown killpg() would kill the TUI itself."""
    if not pids:
        return pids
    try:
        import psutil
    except ImportError:
        return pids  # keep all PIDs (prior behavior)
    kept = set()
    for pid in pids:
        try:
            argv = psutil.Process(pid).cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue  # raced away or zombie — cannot be our fresh server, unsafe to track
        if not any(marker in arg for arg in argv[1:] for marker in _NON_MCP_CHILD_CMDLINE_MARKERS):
            kept.add(pid)
    return kept


def _clear_connect_cooldowns(keys=None) -> None:
    """Drop connect-retry cooldowns: a restart must re-attempt every server immediately, not
    honour a stale per-server backoff. Caller holds ``_core._lock``."""
    if keys is None:
        _core._server_connect_retry_after.clear()
        _core._server_connect_failures.clear()
    else:
        for key in keys:
            _core._server_connect_retry_after.pop(key, None)
            _core._server_connect_failures.pop(key, None)


def _reregister_orphaned_adopters() -> None:
    """Re-run MCP registration for profiles whose ADOPTED shared connection an owner's
    ``/reload-mcp`` just tore down. Their tools vanished with the owner's teardown and nothing
    re-runs their discovery until THEY reload, so they sat tool-less behind a healthy-looking
    status (#106005). Runs after the owner's rediscovery, under each adopter's own home + secret
    scope (its ``${VAR}`` refs must resolve to ITS credentials): the adopter re-adopts the owner's
    new identical connection or connects its own."""
    with _core._lock:
        pending = dict(_core._orphaned_adopters)
        _core._orphaned_adopters.clear()
    if not pending:
        return
    from pathlib import Path
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools import mcp_tool_discovery as _discovery
    from tools.mcp_tool_config import _load_mcp_config
    for adopter, names in pending.items():
        home_token = secret_token = None
        try:
            home_token = set_hermes_home_override(adopter)
            secret_token = set_secret_scope(build_profile_secret_scope(Path(adopter)), profile_home=adopter)
            servers = {n: c for n, c in (_load_mcp_config() or {}).items() if n in names}
            if servers:
                _discovery.register_mcp_servers(servers)
        except Exception:
            logger.debug("MCP: re-registration for profile scope %s failed", adopter, exc_info=True)
        finally:
            if secret_token is not None:
                reset_secret_scope(secret_token)
            if home_token is not None:
                reset_hermes_home_override(home_token)


def shutdown_mcp_servers(*, scope: Optional[str] = None, names: Optional[set] = None,
                         timeout: float = 15.0):
    """Close MCP server connections (in parallel) and stop the background loop. Each server
    Task is signalled to exit its own ``async with`` so the anyio cancel-scope cleanup runs in
    the Task that opened it. ``scope`` restricts teardown to one multiplexed profile's servers
    (its ``/reload-mcp`` must not kill other profiles') and leaves the shared loop running if
    anything else is still connected. ``names`` restricts it further to those server names
    (dropped-from-config pruning); other servers' bookkeeping is untouched. Only the bare call
    (no ``scope``, no ``names``) is the process-wide wildcard: the launch profile's registry
    scope IS ``None``, so ``scope=None, names={...}`` prunes that unscoped owner's servers and
    must leave a served profile's same-named ``(B, name)`` connection alone. ``timeout`` bounds
    the wait for the close to land on the MCP loop — a caller running one pass per served
    profile under a total budget divides it, or N profiles × 15s starve the wildcard pass that
    actually stops the loop."""
    from tools.mcp_tool_scope import _key_name
    wildcard = scope is None and names is None
    with _core._lock:
        selected = [key for key in _core._servers if wildcard or _core._server_scope_keys.get(key) == scope]
        if names is not None:
            selected = [key for key in selected if _key_name(key) in names]
        servers_snapshot = [_core._servers[key] for key in selected]
        if names is not None:
            selected_status = set(selected)
        elif wildcard:
            selected_status = (
                set(_core._servers) | set(_core._server_scope_keys)
                | set(_core._server_tool_scopes)
                | set(_core._server_connecting) | set(_core._server_connect_errors))
        else:
            selected_status = {key for key, owner in _core._server_scope_keys.items() if owner == scope}
        # Adopters of the connections being torn down lose their overlays with the tasks' own
        # ``_deregister_tools``; remember them so the next discovery pass re-registers them
        # (``_reregister_orphaned_adopters``).
        if not wildcard:
            for key in selected:
                for adopter in _core._server_tool_scopes.get(key, ()):
                    if adopter != scope:
                        _core._orphaned_adopters.setdefault(adopter, set()).add(_key_name(key))

    def clear_selected_status():
        _core._server_connecting.difference_update(selected_status)
        for key in selected_status:
            _core._server_connect_errors.pop(key, None)
            _core._server_scope_keys.pop(key, None)
            _core._server_tool_scopes.pop(key, None)

    # Fast path: nothing to shut down. The connect-cooldown maps can still be populated here — a server that
    # failed to connect is never recorded in ``_servers`` (that is the very premise of the #50394 cooldown),
    # so "no live servers" is the MOST likely state in which stale backoff entries exist. Clear them so a
    # post-shutdown restart re-attempts every configured server immediately.
    if servers_snapshot:
        async def _shutdown():
            results = await asyncio.gather(*(server.shutdown() for server in servers_snapshot), return_exceptions=True)
            for server, result in zip(servers_snapshot, results):
                if isinstance(result, Exception):
                    logger.debug("Error closing MCP server '%s': %s", server.name, result)
            with _core._lock:
                for key in selected:
                    _core._servers.pop(key, None)
                    _core._server_scope_keys.pop(key, None)
                clear_selected_status()
                _clear_connect_cooldowns(None if wildcard else selected_status)

        with _core._lock:
            loop = _core._mcp_loop
        if loop is not None and loop.is_running():
            from agent.async_utils import safe_schedule_threadsafe
            future = safe_schedule_threadsafe(_shutdown(), loop, logger=logger, log_message="MCP shutdown: failed to schedule")
            if future is not None:
                try:
                    future.result(timeout=timeout)
                except BaseException as exc:
                    logger.debug("Error during MCP shutdown: %s", exc)

    # Unconditional final sweep: whether ``_shutdown`` ran, timed out, or was never scheduled
    # (a server that failed to connect is never in ``_servers`` — the most likely state for
    # stale backoff entries), no connect-cooldown state may survive shutdown.
    with _core._lock:
        if not servers_snapshot:
            clear_selected_status()
        _clear_connect_cooldowns(None if wildcard else selected_status)
    _loop._stop_mcp_loop(only_if_idle=not wildcard)
    # A removed subset still shares its profile's log with the remaining servers.
    # Full/profile shutdown must also release handles left by completed CLI/UI probes.
    if names is None:
        from tools.mcp_tool_config import _close_mcp_stderr_logs
        _close_mcp_stderr_logs(scope=scope)


def _take_reapable_pids(include_active: bool, server_name: Optional[str]) -> tuple[dict, dict, dict, dict]:
    """Atomically pop selected owners, groups, session IDs and spawn witnesses."""
    def _owned(entries: Dict[int, str]) -> Dict[int, str]:
        return {pid: owner for pid, owner in entries.items() if server_name is None or owner == server_name}

    with _core._lock:
        pids = _owned({opid: _orphan_stdio_pid_servers.get(opid, "orphan") for opid in _orphan_stdio_pids})
        _orphan_stdio_pids.difference_update(pids)
        for opid in pids:
            _orphan_stdio_pid_servers.pop(opid, None)
        if include_active:
            active = _owned(_stdio_pids)
            pids.update(active)
            for pid in active:
                _stdio_pids.pop(pid, None)
        pgids = {pid: _stdio_pgids.pop(pid) for pid in pids if pid in _stdio_pgids}
        sessions = {pid: _stdio_sessions.pop(pid) for pid in pids if pid in _stdio_sessions}
        processes = {pid: _stdio_processes.pop(pid, {}) for pid in pids}
    return pids, pgids, sessions, processes


def _signal_mcp_processes(members: dict, sig: int, pgid: Optional[int], my_pgid: Optional[int]) -> None:
    """Signal a group only through a live incarnation witness; PID fallback uses psutil's reuse guard."""
    import psutil
    alive = {pid: proc for pid, proc in members.items() if _mcp_process_alive(proc)}
    killpg = getattr(os, "killpg", None)
    if pgid is not None and pgid != my_pgid and killpg is not None:
        for proc in alive.values():
            try:
                if os.getpgid(proc.pid) == pgid and _mcp_process_alive(proc):
                    killpg(pgid, sig)
                    return
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
    for proc in alive.values():
        try:
            if os.name == "nt":
                _kill_windows_process_tree(proc.pid, sig, parent=proc)
            proc.send_signal(sig)  # psutil checks cached PID/create-time before signalling
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass


def _kill_windows_process_tree(pid: int, sig: int, *, parent=None) -> None:
    """Windows counterpart of the POSIX killpg path (#61059): after the direct child is signalled,
    terminate every still-alive descendant (npx.cmd → node.exe) so graceful teardown cannot leave
    orphans reparented with ParentId=null. Best-effort, per-descendant; never raises."""
    import signal as _signal
    try:
        import psutil
    except ImportError:
        return
    try:
        if parent is None:  # standalone helper calls; production passes its spawn-time handle
            parent = psutil.Process(pid)
        elif not _mcp_process_alive(parent):
            return
        descendants = parent.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return
    for child in descendants:
        try:
            child.terminate()
        except Exception:  # noqa: BLE001 - raced away or refused; sweep continues
            pass
    if sig == getattr(_signal, "SIGKILL", _signal.SIGTERM):  # force pass: don't wait for graceful exit
        _, alive = psutil.wait_procs(descendants, timeout=0)
        for child in alive:
            try:
                child.kill()
            except Exception:  # noqa: BLE001
                pass


def _kill_orphaned_mcp_children(include_active: bool = False, server_name: Optional[str] = None) -> None:
    """Best-effort reap of stdio MCP subprocesses: SIGTERM, wait 2s, SIGKILL survivors. By
    default only ``_orphan_stdio_pids`` are reaped so concurrent cron jobs / live sessions are
    untouched; ``include_active=True`` also kills every ``_stdio_pids`` entry and is only for
    final shutdown after the MCP loop has stopped. ``server_name`` limits the sweep to one
    server (stdio reconnects cleaning up their old transport)."""
    import signal as _signal
    import psutil
    pids, pgids, sessions, processes = _take_reapable_pids(include_active, server_name)
    if not pids:
        return

    try:  # our own pgid, so we never killpg() the gateway itself
        my_pgid = os.getpgrp()
    except (AttributeError, OSError):
        my_pgid = None  # Windows or restricted environment

    try:
        owned = {
            pid: _owned_mcp_processes(
                processes[pid], pgids.get(pid), my_pgid,
                leader_pid=pid, session_id=sessions.get(pid),
            )
            for pid in pids
        }
        survivors = {member_pid: proc for members in owned.values() for member_pid, proc in members.items()}
        if not survivors:  # empty, dead, reused or unverified ledgers pay no grace
            return
        for pid, members in owned.items():
            _signal_mcp_processes(members, _signal.SIGTERM, pgids.get(pid), my_pgid)
        # One shared grace, not one per child. psutil waits on cached incarnation
        # handles and returns as soon as all owned processes exit.
        deadline = time.monotonic() + 2.0
        psutil.wait_procs(list(survivors.values()), timeout=max(0.0, deadline - time.monotonic()))
        sigkill = getattr(_signal, "SIGKILL", _signal.SIGTERM)
        for pid, members in owned.items():
            alive = {member_pid: proc for member_pid, proc in members.items() if _mcp_process_alive(proc)}
            if alive:
                _signal_mcp_processes(alive, sigkill, pgids.get(pid), my_pgid)
                logger.warning("Force-killed MCP process group for %d (%s) after SIGTERM timeout", pid, pids[pid])
    finally:
        # Release groups whose current membership is verified empty or positively
        # identified as foreign. An unreadable/live group stays registered with
        # the death supervisor.
        empty = []
        for pid, pgid in pgids.items():
            scan = _enumerate_mcp_group(pgid, sessions.get(pid))
            if scan == {} or getattr(scan, "foreign", False):
                empty.append(pgid)
        _core._update_death_supervisor("unregister", empty)


def _stop_mcp_loop_if_idle() -> bool:
    """Stop the MCP loop only when no registered server still owns it. Probe paths create
    temporary MCPServerTasks not placed in ``_servers``; they may clean up an idle loop but
    must not tear down the process-global loop under live agent tools."""
    return _loop._stop_mcp_loop(only_if_idle=True)


async def _drain_mcp_loop_tasks(*, timeout: Optional[float] = None) -> None:
    """Cancel every task still pending on the MCP loop and reap it. ``Task.cancel()`` only
    schedules the throw, so tasks need a cancellation cycle before the loop goes away; wait
    for them here, on their owning loop, bounded so a task that suppresses cancellation
    cannot hang process exit."""
    if timeout is None:
        timeout = _core._MCP_LOOP_DRAIN_TIMEOUT
    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    if not pending:
        return
    logger.debug("Draining %d pending task(s) from the MCP loop", len(pending))
    for task in pending:
        task.cancel()
    done, still_pending = await asyncio.wait(pending, timeout=timeout)
    for task in done:
        if not task.cancelled():
            task.exception()  # mark retrieved so asyncio doesn't warn "exception was never retrieved"
    if still_pending:
        logger.warning("%d MCP loop task(s) still pending after %.1fs drain", len(still_pending), timeout)


async def _drain_and_stop_mcp_loop() -> None:
    """Drain pending tasks, then stop the loop from its owning thread. Both must run as one
    loop-owned sequence: a ``loop.stop`` queued separately by a timed-out caller can overtake
    the scheduled drain, leaving the drain coroutine itself pending when the loop is closed."""
    loop = asyncio.get_running_loop()
    try:
        await _drain_mcp_loop_tasks(timeout=_core._MCP_LOOP_DRAIN_TIMEOUT)
    finally:
        loop.call_soon(loop.stop)
