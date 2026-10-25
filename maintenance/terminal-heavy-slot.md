# Terminal heavy-slot

Load when changing `tools/terminal_tool_heavy_slot.py`, how `terminal_tool` hands the execution command to `_run_foreground` or `spawn_background_process`, local spawn and kill paths that a wrapped command passes through, or the narrow installed-helper peel in `cron/lifecycle_guard.py`.

## Required behavior and provenance

Fork patch identities: `terminal-heavy-slot`; lifecycle-guard addition: `lifecycle-guard-heavy-slot-walk`.

On the Mac Studio, agents started test suites and gates from the Hermes terminal tool directly. A 10-minute load probe on 2026-10-05 found an average of 9.5 suites or gates running outside the host's `heavy-slot` semaphore at once (12 at peak), on 28 cores at load 60-120. The shell-function wrappers in `~/.config/shell/heavy-slot.sh` only cover `./bin/ci` and `scripts/run_tests.sh` typed literally, and miss `pytest`, `uv run pytest`, `pnpm test` and `vitest`.

For the local backend only, `wrap_heavy_command` parses the command with the existing quote-aware shell scanner (`_scan_shell`), with heredoc bodies masked by `_mask_heredocs`. It routes each simple command in command position that runs a broad pytest, vitest or jest run, `scripts/run_tests.sh`, tox or nox, a package `test`/`test:*` script in any spelling (`pnpm test`, `npm run test`, `yarn workspace app test`, `pnpm -r test`) including heavy build/check/lint/typecheck scripts and dependency installation (`add`, `install`, `ci` and similar), `turbo`/`nx`/`lerna` test runs, or a `./bin/ci` profile other than `preflight`, `list` or `install-hooks` through `_hermes_heavy_slot LABEL CMD ...`, a shell function the rewrite defines on its first line. An external program goes through `heavy-slot --label` with the cwd it runs in. A name the session shell resolves itself (alias, function, builtin, in bash or zsh) runs unchanged, so a user's own wrapper such as the `./bin/ci` function from `~/.config/shell/heavy-slot.sh` keeps deciding. The `_` prefix keeps the snapshot re-dump from persisting the function. Exec-style prefix utilities (`env`, `nice`, `timeout`, `nohup`) cannot run a shell function, so the dispatcher goes in front of them. `command NAME` and `exec NAME` are left unwrapped because they deliberately bypass functions or replace the session shell. The rest of the command stays in the session shell, so `cd`, exports, functions, `&` and the cwd marker behave as before. Approval, guards, metrics, exit-code notes, verification evidence and redaction all see the user's original command.

Never wrapped: non-local backends (Docker, SSH, Modal, Singularity, Daytona, Vercel), commands whose environment already has `HEAVY_SLOT_HELD` (nested runs share the outer slot), the opt-out `HERMES_HEAVY_SLOT=off` (also `0`, `false`, `no`) in the process or terminal environment, hosts without the helper, mentions of a runner in arguments, quotes, comments or heredoc bodies (`grep pytest`, `git commit -m 'pytest'`), targeted direct pytest/Jest/Vitest runs of 1-10 explicit test files whose options are all on a small single-run allowlist (`-q`, `-x`, `-k`, `--tb` and similar; any other option, such as `-n`, `--maxWorkers` or `--pool`, keeps the slot), and git-guard's `ci-gate`, which already takes its own slot around the repository gate. Any classifier error runs the command unwrapped.

The helper runs the heavy command in its own process group under the shell's group and stops its whole tree when signalled. Foreground timeouts, background kills and PTY kills therefore still tear everything down and release the slot. A wait for a slot counts against the command's foreground timeout. Long gates should run in the background, as they already must.

## Expanded launchers and per-job budget

The classifier and cheap prefilter also cover Node workspace/Playwright checks,
ESLint/typechecks, `uv sync` and package installation, Python unittest/compileall,
Go tests/builds, PM installs, and `agentkit evals run`/`admit`. `run_tests.sh` and
`run_tests_parallel.py` always take a slot even for one file: targeted shell runs
still compile the whole checkout or realize their test environment. Direct
single-runner targeted pytest/Jest/Vitest commands retain their light exemption.
The existing helper's ancestor reentrancy and the `HEAVY_SLOT_HELD` fast path
continue to prevent nested admission deadlocks; no second semaphore is added.

`scripts/run_tests.sh` defaults and forwards `HERMES_TEST_WORKERS=4` into the
clean test environment and uses it for `compileall -j`, not `-j 0`. Workspace
checks use the same budget (fallback 4), preserving explicit `--concurrency`.
PM binds `UV_CONCURRENT_BUILDS` and `UV_CONCURRENT_INSTALLS` to this budget after
stripping ambient UV settings; uv sizes its bytecode compiler from install
concurrency. Positive explicit worker budgets remain opt-ins. The host's
chezmoi-managed shell sets the same four-worker default while retaining two
machine-wide slots. No daemon, process kills, or queue-policy change is needed.

## Lifecycle guard contract

The guard omits `heavy-slot`'s own source only when an explicit absolute command path resolves by realpath to the installed helper at `~/.local/bin/heavy-slot` and the argv is exactly `heavy-slot [--label VALUE | --label=VALUE | --gate-fd N | --gate-fd=N]* -- CMD...`. An absolute symlink alias is trusted by identity, not basename. It strictly scans `CMD...` and its executed scripts. Missing `--`, abbreviated or unknown options, malformed gate descriptors, relative and bare names, and every other file named `heavy-slot` retain the helper's strict source scan. Bare names are resolved through the existing passive resolver for that scan; they never earn the trusted-source omission, even on an unchanged PATH. Remote text callbacks cannot attest the local helper's identity and never earn it either.

The non-trusted path treats `heavy-slot` as an opaque prefix **in addition to** scanning its source. Before the first `--`, only the exact argparse options `--label VALUE`, `--gate-fd N`, `-h`, and `--help` are recognized; argparse-style abbreviations are refused by name rather than emulated. It follows the command after the first `--`, or after the known value-taking options when `--` is missing, using the existing prefix and recursive script/payload walks. This includes directly executed, shell-executed, sourced, and option-carried subprocesses (`env -S`, clustered `env -iS...`, attached `su -c...`, `runuser -c`). Short-option clusters follow getopt semantics: once a value-taking option appears, the rest of that token is its value. Option scanning stops at the wrapped command operand only when that boundary is proven: reached via `--` or with every earlier wrapper option recognized in the value/flag tables (including BSD/GNU `env -P`/`-a`/`--argv0` and util-linux `su`/`runuser` `-w`/`--whitelist-environment`). After any unrecognized wrapper option the boundary is ambiguous, so the string-option scan runs to the end of the segment and every later token is also treated as a possible command start; this may over-flag but never drops a payload. `su` and `runuser` are never bounded: util-linux parses them with a permuting `getopt_long` (`-c` may follow the user operand) and `run_shell` passes every argument left after the user, including everything after `--`, to the target shell. Their `-c`/`--command`/`--session-command` scan (separate, attached, clustered) runs to the segment end, and every trailing token and attached option value is also scanned as shell source, except after a recognized `runuser -u`/`--user`, which execs the command directly. `sudo -s`/`-i` and `doas -s` operands are likewise scanned as shell source. A proven `--` boundary bounds the scan only when the next token is a real command name, not an option-shaped token such as `env -i -- -S ...`. An unknown option without a delimiter, a missing option value, an empty delimited command, or an exhausted prefix chain refuses with a named wrapper/budget reason; no remaining command may be silently dropped. `heavy-slot` `-h`/`--help` and benign wrapped test scripts are allowed, but the helper's source still scans strictly on the non-trusted path.

Only `/dev/null` and `/dev/zero` are ignored by the referenced-script candidate walk because they produce no script bytes. Standard streams, `/dev/fd`, and exactly `/dev/fd/<digits>` fail closed even when a host exposes one as a regular descriptor; the match is lexical and exact, so `/dev/fd/../root/...` and `/dev/fd/../cwd/...` remain script candidates, never device exemptions.

Generic interpreter/shebang leniency is retired. Every other directly executed script, including Python and `env` shebang forms, remains on the base strict walk; a shebang is not an attestation that its source is safe to tokenize as shell. The lifecycle guard does not model arbitrary interpreter dispatch, shell fallback, or command-specific PATH changes.

Upstream: `heavy-slot` is a host-local tool, so this has no upstream equivalent and is not proposed upstream.

## Verification and retirement

Run `scripts/run_tests.sh tests/tools/test_terminal_tool_heavy_slot.py`. It covers classification in both directions, the exact rewrite, functions and aliases passing through the dispatcher in real bash and zsh, non-local backends, the nested-slot marker and opt-out, fail-open on classifier errors, a real `LocalEnvironment` command whose `cd` and export survive the wrapped call, and (when the helper is installed) a background run through the real helper with private slots, killed through the process registry, which must leave no process and no held slot.

Also run `HERMES_TEST_WORKERS=4 scripts/run_tests.sh tests/pm/test_environment_parallelism.py tests/scripts/test_runner_parallelism.py` and
`HERMES_TEST_WORKERS=4 node --test scripts/ci/tests/workspace-checks.test.mjs`.
These exercise the actual PM/shell child-process boundary and Node runner
budget; the classifier table exercises classification **and wrapping** for
heavy commands plus light read-only negatives.

Retire when the host stops using `heavy-slot` or when Hermes gains a native concurrency limit for heavy terminal work. No stored-state migration is involved. Rollback reverts the commits carrying this identity.
