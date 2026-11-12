# Local delegated shell isolation

Patch identity: `local-child-shell-isolation`.

## Contract

A delegated child on the local terminal backend owns its persistent bash export snapshot. Its exports (including HOME, HERMES_HOME, PATH and sandbox settings) must not enter the parent, sibling or nested child's shell; parent exports must not initialize child shells either. Repeated commands within one child retain its own exports. The filesystem remains shared and cwd seeding remains unchanged. This is shell-state ownership, not an OS security boundary.

`_ChildRun.seed_workspace` registers every child task ID, including lifecycle/review children, in the container-alias registry. The local environment key resolver checks that declared identity before the inherited gateway session key or cron run alias. It never recognizes children by an ID spelling. Routed profile qualification and explicit backend/image overrides remain authoritative. Docker, SSH and other sandbox backends retain their existing container-sharing contracts.

The existing idle environment reaper owns child snapshots and preserves environments with active background processes. Do not add eager child-close deletion: timeout-deferred workers and handed-off adopted shells can still write their snapshot. Idle cleanup uses the exact cached key (including routed namespace), not the profile context of a Future callback. No new teardown lifecycle is introduced.

Contract tests: `tests/tools/test_local_delegated_shell_isolation.py`, `tests/tools/test_shared_container_task_id.py`.

## Evidence and upstream disposition

Fork base: `9add22c5a5fb54605cc374841721ca9f5c2cc535`. Upstream main inspected: `a212421dbd5070ce70204a88af4b3c3c2aaa2b18`. Upstream root and subsystem guidance require a real reproduction and preserving intentional container sharing. Related open upstream PRs #140434 (cwd-record ownership) and #83430 (opt-in container sandboxing, explicitly not local shells) do not fix default local export snapshot sharing. No competing fork PR was found in the initial shell/child-terminal/environment searches. No upstream submission is part of this change.

Root cause: local terminal cache keys collapsed registered children onto the parent's inherited session key, default key, or cron run owner. `_acquire_env` therefore returned the same LocalEnvironment object, whose BaseEnvironment snapshot is re-sourced and overwritten on every command. A child's export was an update to the parent's snapshot, not Python os.environ propagation. RED on the base: the real local terminal returned reviewer|review-home for the parent after a child export on gateway, CLI and cron paths. The regression now seeds real child runs and verifies separate snapshot paths, within-child persistence, both isolation directions, siblings/nesting and idle cleanup.

Source surfaces: `tools/terminal_tool.py`, `tools/terminal_tool_lifecycle.py`. Verification uses `scripts/run_tests.sh` under heavy-slot admission with disposable HOME/HERMES_HOME/HERMES_RUNTIME_DIR, the affected terminal/delegation tests, source-only Fork-Patch ownership, whitespace and Windows-footgun checks. Source publication does not imply runtime adoption; no live profile, update, promotion or restart is authorized.

Retire when an adopted upstream release gives every local delegated child independent persistent shell state and equivalent teardown proof without changing required non-local sharing. Reverting restores the environment-leak risk; no persisted-state migration is introduced.
