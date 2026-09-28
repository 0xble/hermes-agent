# Foreground command exit cleanup

Load when changing `kill_live_foreground_processes`, the foreground spawn publication fence, or process-exit cleanup of terminal environments.

## Required behavior and provenance

Fork patch identity: `foreground-exit-cleanup`.

Nightly [run 36492811025](https://github.com/0xble/hermes-agent/actions/runs/36492811025), Python and e2e (7/10), found a live bash and sleep process group after `cleanup_all_environments()`. Graceful exit took a snapshot of published commands without fencing new spawns or waiting for a child already created but not published. The hard-exit path had the fence and wait; graceful exit now does too. The group-wide cleanup still kills only tracked foreground commands, not background jobs intended to outlive the host.

Upstream design checked at `NousResearch/hermes-agent` main `7154128fe19f393267b1a2e4ca8176ca53bfbe24` (root and tools AGENTS.md, CONTRIBUTING.md). Related [PR #124609](https://github.com/NousResearch/hermes-agent/pull/124609) covers a distinct slash-worker hard-exit path and mentions this pre-existing flaky test; it does not fence graceful spawns. No equivalent upstream merged fix was identified in that review.

## Verification and retirement

Run `scripts/run_tests.sh tests/tools/test_local_interrupt_cleanup.py` on Linux with a reaping init process, plus the local process-tree tests. The spawn-before-publication regression holds a real child behind a gate while exit cleanup starts, then releases it and requires its process group to disappear. The preexisting exit-cleanup assertion remains strict. In a minimal container without `ps`, diagnostics use psutil; without an init reaper, a killed sleep may remain a zombie indefinitely and make the strict group-existence probe fail for a test-environment reason.

Retire only when the accepted upstream release fences graceful spawns and the regression passes without the fork implementation. No stored-state migration is involved.
