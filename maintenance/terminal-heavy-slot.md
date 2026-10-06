# Terminal heavy-slot

Load when changing `tools/terminal_tool_heavy_slot.py`, how `terminal_tool` hands the execution command to `_run_foreground` or `spawn_background_process`, or local spawn and kill paths that a wrapped command passes through.

## Required behavior and provenance

Fork patch identity: `terminal-heavy-slot`.

On the Mac Studio, agents started test suites and gates from the Hermes terminal tool directly. A 10-minute load probe on 2026-10-05 found an average of 9.5 suites or gates running outside the host's `heavy-slot` semaphore at once (12 at peak), on 28 cores at load 60-120. The shell-function wrappers in `~/.config/shell/heavy-slot.sh` only cover `./bin/ci` and `scripts/run_tests.sh` typed literally, and miss `pytest`, `uv run pytest`, `pnpm test` and `vitest`.

For the local backend only, `wrap_heavy_command` parses the command with the existing quote-aware shell scanner (`_scan_shell`), with heredoc bodies masked by `_mask_heredocs`. It puts `heavy-slot --label ... --` in front of each simple command in command position that runs a broad pytest, vitest or jest run, `scripts/run_tests.sh`, tox or nox, a package `test`/`test:*` script in any spelling (`pnpm test`, `npm run test`, `yarn workspace app test`, `pnpm -r test`) except package management (`add`, `install` and similar), `turbo`/`nx`/`lerna` test runs, or a `./bin/ci` profile other than `preflight`, `list` or `install-hooks`. The rest of the command stays in the session shell, so `cd`, exports, functions, `&` and the cwd marker behave as before. Approval, guards, metrics, exit-code notes, verification evidence and redaction all see the user's original command.

Never wrapped: non-local backends (Docker, SSH, Modal, Singularity, Daytona, Vercel), commands whose environment already has `HEAVY_SLOT_HELD` (nested runs share the outer slot), the opt-out `HERMES_HEAVY_SLOT=off` (also `0`, `false`, `no`) in the process or terminal environment, hosts without the helper, mentions of a runner in arguments, quotes, comments or heredoc bodies (`grep pytest`, `git commit -m 'pytest'`), targeted runs of 1-10 explicit test files whose options are all on a small single-run allowlist (`-q`, `-x`, `-k`, `--tb` and similar; any other option, such as `-n`, `--maxWorkers` or `--pool`, keeps the slot), and git-guard's `ci-gate`, which already takes its own slot around the repository gate. Any classifier error runs the command unwrapped.

The helper runs the heavy command in its own process group under the shell's group and stops its whole tree when signalled. Foreground timeouts, background kills and PTY kills therefore still tear everything down and release the slot. A wait for a slot counts against the command's foreground timeout. Long gates should run in the background, as they already must.

Upstream: `heavy-slot` is a host-local tool, so this has no upstream equivalent and is not proposed upstream.

## Verification and retirement

Run `scripts/run_tests.sh tests/tools/test_terminal_tool_heavy_slot.py`. It covers classification in both directions, the exact prefix rewrite, non-local backends, the nested-slot marker and opt-out, fail-open on classifier errors, a real `LocalEnvironment` command whose `cd` and export survive the wrapped call, and (when the helper is installed) a background run through the real helper with private slots, killed through the process registry, which must leave no process and no held slot.

Retire when the host stops using `heavy-slot` or when Hermes gains a native concurrency limit for heavy terminal work. No stored-state migration is involved. Rollback reverts the commits carrying this identity.
