# Cron prompt-update guard

**Patch identity:** `cron-prompt-update-guard`.

A cron prompt update replaces the job's whole instruction. On 2026-10-12 an agent
that meant to load a prompt from a file called `cronjob(action="update",
prompt="__FILE__")` and persisted the literal 8-char token over an 11,137-char
prompt, the second such wipe in two days. Prose warnings in the cron guide did not
prevent it.

Preserve: every prompt *update* through `cronjob_manage`, `hermes cron edit`, and
chat `/cron edit` goes through `cron/prompt_guard.py::prompt_update_error`, which
refuses (a) a stripped prompt that fully matches a placeholder shape (`__WORD__`,
a single `<...>` token, `...`/`…`, `TODO`/`TBD`/`FIXME`/`PLACEHOLDER`,
`$(cat …)`, `` `cat …` ``, `@path`, `$VAR`/`${VAR}`), and (b) a prompt under 10%
of an existing prompt of 500+ characters. The refusal names the reason and both
lengths, and the update persists nothing (all fields in that call are dropped).
`allow_shrink=true` (tool) / `--allow-shrink` (both CLIs) overrides both checks.
Creation is unguarded: there is no prior prompt to lose.

Source: `cron/prompt_guard.py`, `tools/cronjob_tools.py` (`_update_core_fields`,
schema `allow_shrink`, `_HANDLER_FORWARDED_ARGS`), `hermes_cli/cron.py`
(`cron_edit`), `hermes_cli/subcommands/cron.py`, `hermes_cli/cli_commands_mixin.py`
(`/cron edit`), `website/docs/user-guide/features/cron.md`.

Proof: `scripts/run_tests.sh tests/tools/test_cronjob_tools.py` and
`scripts/run_tests.sh tests/hermes_cli/test_cron.py` (classes
`TestPromptUpdateGuard`, `TestCronEditPromptGuard`).

The guard sits in the tool's update step, not `cron.jobs.update_job`: internal
scheduler and dashboard writes go through `update_job` without a prompt change, and
the REST `PATCH /api/jobs/{id}` path is out of scope for this incident.

Upstream: no upstream issue or PR found (searched 2026-10-12 for cron prompt
placeholder/overwrite/shrink/validation). Contribute upstream. Retire when upstream
refuses placeholder and drastic-shrink prompt updates on both the tool and CLI
paths with an explicit override, and these regressions pass there.

Rollback: revert the commits carrying `Fork-Patch: cron-prompt-update-guard`.
