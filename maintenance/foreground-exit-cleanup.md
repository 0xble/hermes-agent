# Foreground command exit cleanup

Load when changing `kill_live_foreground_processes`, the foreground spawn publication fence, or process-exit cleanup of terminal environments.

## Required behavior and provenance

Fork patch identity: `foreground-exit-cleanup`.

Nightly [run 36492811025](https://github.com/0xble/hermes-agent/actions/runs/36492811025), Python and e2e (7/10), found a live bash and sleep process group after `cleanup_all_environments()`. Graceful exit took a snapshot of published commands without fencing new spawns or waiting for a child already created but not published. The hard-exit path retains its one-way fence; graceful cleanup instead raises a temporary, counted fence across its bounded wait, snapshot and kill, then releases only its own fence in `finally`. An in-process caller can subsequently execute foreground work. Both wait paths use a 0.5s spawn-publication budget: a remote SDK can stall during spawn, and a signal handler can reenter cleanup on the same spawning thread. The group-wide cleanup still kills only tracked foreground commands, not background jobs intended to outlive the host.

Upstream design checked at `NousResearch/hermes-agent` main `7154128fe19f393267b1a2e4ca8176ca53bfbe24` (root and tools AGENTS.md, CONTRIBUTING.md). Related [PR #124609](https://github.com/NousResearch/hermes-agent/pull/124609) covers a distinct slash-worker hard-exit path and mentions this pre-existing flaky test; it does not fence graceful spawns. No equivalent upstream merged fix was identified in that review.

## Shutdown process-table cost

The October 13 watchdog dump has two tool-kill threads inside macOS psutil's
`_ppid_map`, called by `local.py`'s pre-signal recursive descendant snapshot.
Each scan reads every host PID's parent via sysctl; concurrent kills repeat that
work before sending a signal. The other nine requested dumps are truncated at
100 threads, so this evidence does not establish one cause for every watchdog.
Two show a main-thread session-store lock wait; current fork goal lookups already
use the async session-store boundary.

For the shared `tools.interrupt.INTERRUPT_REASON_GATEWAY_SHUTDOWN` cause or an
active foreground cleanup fence, local POSIX teardown omits descendant discovery.
`execute()` binds its deadline wait worker's `acting_for_tid` to the submitting
tool thread and resets it after the wait, so the kill decision sees the same
shutdown cause as `_wait_for_process`'s parent-thread interrupt check. An
inherited binding wins: the pre-exec guard's nested execute already acts for
the tool thread, and rebinding it to the guard worker would hide `/stop`. It retains
the existing bounded group TERM→KILL/reap sequence and both PGID fingerprint
checks. Windows is unchanged.
Normal timeout and `/stop` paths still snapshot before signalling and sweep
identity-checked setsid escapees. Shutdown deliberately gives up that best-effort
escapee sweep rather than spending its budget enumerating the entire host.
An ordinary descendant scan already in progress before shutdown is not cancelled.

## Verification and retirement

Run `scripts/run_tests.sh tests/tools/test_local_interrupt_cleanup.py` on Linux with a reaping init process, plus the local process-tree tests. The spawn-before-publication regression holds a real child behind a gate while exit cleanup starts, then releases it and requires its process group to disappear. The preexisting exit-cleanup assertion remains strict. In a minimal container without `ps`, diagnostics use psutil; without an init reaper, a killed sleep may remain a zombie indefinitely and make the strict group-existence probe fail for a test-environment reason.

The shutdown-specific checks are in `tests/tools/test_local_shutdown_kill.py`: gated
slow scans cannot delay a gateway interrupt or foreground cleanup. The real
`execute()` → `run_bounded_sync` → `_wait_for_process` regression spawns a child,
interrupts only the submitting tool thread, and requires returncode 130 without
any `children()` call; it does not set worker interrupt context itself.
A nested execute under an outer acting-for binding must still return 130 on
`/stop` aimed at the tool thread. Recycled group leaders must not be signalled before TERM or during
escalation; the caller's group is never signalled; normal cancellation retains
identity-checked escapee sweeping. Run these with `test_pgid_starttime_drift.py`,
`test_interrupt.py`, `test_local_interrupt_cleanup.py`, and
`test_local_setsid_descendant_sweep.py`. The live escapee integration module is
Linux-only; the mocked shutdown/ownership contracts also run on macOS.

Retire only when the accepted upstream release fences graceful spawns, avoids
host-wide descendant scans on shutdown, and these regressions pass without the
fork implementation. No stored-state migration is involved.
