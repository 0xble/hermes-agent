# Fork-sync code-health refactors

Fork patch identity: `fork-sync-code-health`.

## Contract

The frozen upstream sync must satisfy the unmodified main-side code-health
ratchet without changing runtime behavior, suppressing findings, or deleting tests.

Existing helpers moved into topical siblings retain original import/patch bindings:
`agent/turn_recovery_entitlement.py`, `cron/scheduler_delivery_results.py`,
`gateway/run_adapters_scope.py`, `gateway/run_notifications_process.py`,
`gateway/run_startup_secrets.py`, `gateway/run_turn_pending.py`,
`gateway/run_turn_runner_receipts.py`, `hermes_cli/update_cmd_fleet_receipts.py`,
and `hermes_state_sessions_workspace.py`. Phase extractions preserve ordering,
exception boundaries, and facade dependencies. Existing mixin methods remain
inherited attributes of their original classes.

The synchronous foreign-node predicate in `gateway/control_socket.py` runs through
`asyncio.to_thread`, preserving its short-circuit evaluation and error propagation.
Duplicate terminal lifecycle imports are consolidated without changing bindings.

The F811 in `test_profiles.py` was a local `import socket` shadowing its module
import, not a duplicate test definition. The pinned source and both merge parents
contain exactly one symlink-provider test; it remains collected and executed.
Profile skill-cache and finite-CLI metrics tests move unchanged into topical files.
The former in-process one-shot cleanup probe retired with upstream commit
`936f3465f8283645315c24c04016a50af61b2a2c`, which removed that runtime.
Gateway-backed one-shot dispatch and session finalization own the current lifecycle.

Contract tests: `tests/hermes_cli/test_profiles.py::TestCreateProfile::test_clone_config_never_follows_symlinks_out_of_the_provider_dir`
  `tests/hermes_cli/test_profiles_skill_counts.py::test_count_skills_publishes_timestamp_after_the_walk`
  `tests/hermes_cli/test_relay_shared_metrics_runtime_cli.py::test_finite_cli_runs_report_their_own_entrypoint`
  `tests/gateway/test_runner_startup_failures.py`
  `tests/gateway/test_pending_steer_drain.py`
  `tests/gateway/test_background_process_notifications.py`
  `tests/tools/test_browser_vault.py`
  `tests/cron/test_cron_live_bot_delivery.py`

## Semantic re-resolution follow-through

The release-sync re-resolution also splits adapter Markdown split guards, busy restart
redelivery, interrupt-time plugin dispatch, queued copy delivery, shutdown-note target
reservation, ephemeral reply coercion, progress-lane admission, turn notice callbacks,
stream tick updates, browser result projection, environment process readers, and sandbox
image defaults into topical siblings. Moved methods keep their original qualified
identities and remain inherited by the original classes; late-bound facade imports
preserve existing monkeypatch seams. The split adds no cap configuration or waivers.

Standalone and environment delivery boundaries log tracebacks at the catch site.
Profile config reads use coherent runtime-scope bindings, including the single-profile
launch lane's deliberate unbound home/terminal state. Duplicate Telegram rich-message
tests retain the newer superset (including inline-fence and ordered-start assertions),
not the stale second definitions that shadowed it.

## Retirement and rollback

Retire when a later frozen upstream baseline includes equivalent decomposition.
Rollback the commits carrying this identity together; reverting only a sibling
would break imports. No ratchet configuration or exemptions belong to this unit.
