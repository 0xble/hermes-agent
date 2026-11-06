# Snapshot keeps shell functions

Load when changing the terminal session snapshot: `_snapshot_bootstrap_script`, `_wrap_command_script`, `_shell_state_dump` or `_export_dump_excluding_session_vars` in `tools/environments/base_session_env.py`.

## Required behavior and provenance

Fork patch identity: `snapshot-keep-functions`.

The login-shell bootstrap writes exports, functions and aliases into the session snapshot, which every later command sources. The per-command re-dump wrote exports only and then replaced the snapshot, so every function and alias from the user's profile, `terminal.shell_init_files`, or an earlier command disappeared from the second command on. Exported variables survived, which hid the loss. Found when the `heavy-slot` validation limiter (dotfiles #689), sourced through `terminal.shell_init_files`, wrapped `./bin/ci` only on a session's first command.

Both writers now emit one shared `_shell_state_dump`: exports with the per-session exclusions, then name-filtered functions, aliases, and the `shopt -s expand_aliases`, `set +e`, `set +u` trailer. The same mktemp plus atomic `mv` publish applies, and a failed dump still never replaces a good snapshot.

Upstream checked at `NousResearch/hermes-agent` main on 2026-10-01: `_wrap_command_script` still re-dumps `export -p` only. Issue and pull request searches for lost snapshot functions and aliases found no report or fix. The #38249 atomic-write and #71296 session-variable fixes are compatible and kept.

## Bash Parser Recovery

Fork patch identity: `terminal-bash52-parser-recovery`.

Bash 5.2 before bash52-018 can corrupt parser recovery after an unclosed command
substitution in `eval`. Parse the eval and its complete epilogue as 1 brace group,
after sourcing the snapshot so aliases and shell options still apply. Keep
PIPESTATUS capture inside the existing eval. Emit `pwd -P` between marker
delimiters without command substitution, suppress its diagnostics, and reject
non-absolute or multiline cwd marker bodies without replacing the previous cwd.
Apply path validation only to cwd: PIPESTATUS keeps its generic numeric body,
so piped verifier results retain per-segment attribution.

Contract tests: `tests/tools/test_terminal_bash_parser_recovery.py`,
`tests/tools/test_terminal_pipeline_status.py`, `tests/tools/test_base_environment.py`,
`tests/tools/test_terminal_marker_extraction.py`.
The real-shell regressions preserve Bash 3.2's exit 1 and modern Bash's exit 2
without accepting a signal, retain snapshots and aliases, and recover after a
command removes its own working directory. Retire only when released upstream
preserves these behaviors without the wrapper adaptation.

## Verification and retirement

Run `scripts/run_tests.sh tests/tools/test_snapshot_functions_persist.py`. It defines a function, a slash-named function, an alias and an export in one command and requires all four on three later commands, requires an init-file function on every command, and keeps `_`-prefixed helpers out. The first two fail on the unpatched base.

Retire when upstream's per-command re-dump carries functions and aliases and the regression passes without `_shell_state_dump`. No stored-state migration is involved. Rollback reverts this commit only.
