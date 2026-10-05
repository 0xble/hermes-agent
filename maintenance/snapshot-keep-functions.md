# Snapshot keeps shell functions

Load when changing the terminal session snapshot: `_snapshot_bootstrap_script`, `_wrap_command_script`, `_shell_state_dump` or `_export_dump_excluding_session_vars` in `tools/environments/base_session_env.py`.

## Required behavior and provenance

Fork patch identity: `snapshot-keep-functions`.

The login-shell bootstrap writes exports, functions and aliases into the session snapshot, which every later command sources. The per-command re-dump wrote exports only and then replaced the snapshot, so every function and alias from the user's profile, `terminal.shell_init_files`, or an earlier command disappeared from the second command on. Exported variables survived, which hid the loss. Found when the `heavy-slot` validation limiter (dotfiles #689), sourced through `terminal.shell_init_files`, wrapped `./bin/ci` only on a session's first command.

Both writers now emit one shared `_shell_state_dump`: exports with the per-session exclusions, then name-filtered functions, aliases, and the `shopt -s expand_aliases`, `set +e`, `set +u` trailer. The same mktemp plus atomic `mv` publish applies, and a failed dump still never replaces a good snapshot.

Upstream checked at `NousResearch/hermes-agent` main on 2026-10-01: `_wrap_command_script` still re-dumps `export -p` only. Issue and pull request searches for lost snapshot functions and aliases found no report or fix. The #38249 atomic-write and #71296 session-variable fixes are compatible and kept.

## Verification and retirement

Run `scripts/run_tests.sh tests/tools/test_snapshot_functions_persist.py`. It defines a function, a slash-named function, an alias and an export in one command and requires all four on three later commands, requires an init-file function on every command, and keeps `_`-prefixed helpers out. The first two fail on the unpatched base.

Retire when upstream's per-command re-dump carries functions and aliases and the regression passes without `_shell_state_dump`. No stored-state migration is involved. Rollback reverts this commit only.
