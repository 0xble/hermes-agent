# Upstream release defects

Load this unit before changing any file named in a section's guard or description
below, when a sync review finds a defect in upstream or fork code, and when deciding
whether a fix here can retire because upstream now carries an equivalent. Current
scope: parked-profile gateway status and restart, Bot Desktop teardown, the workspace
snapshot pin, memory-provider config cloning, `/update` reporting, deferred slash
commands, portable CI and its source guards, launchd test scoping, desktop E2E
wiring, Telegram media and album flood control, the delivery ledger, the Hindsight
session lifecycle, the alias-cache isolation guard, gateway orphan-reaper
home scoping, per-run cron terminal isolation, gateway subcommand exit codes, and
the `v0.21.6` merge integration.

Each section is a narrow fix for a defect found while syncing to upstream
`v2026.9.24`, either shipped by upstream or exposed in fork code by that sync, and
records its patch identity and guard test. Offer upstream-origin fixes upstream and
drop each once upstream carries an equivalent fix. Append new defects as sections
here; move a section into a behavior-specific unit when that unit starts owning it.

`production-unused-imports` is a mechanical fork-only hygiene patch. Ruff's `F401` check is the guard for the four removed imports in `agent/agent_init.py`, `agent/codex_runtime.py`, and `agent/memory_manager.py`; retire this record if the imports return or upstream carries the equivalent cleanup.

## Fail-Closed MCP Config Reconciliation

- Fork patch identity: `mcp-config-fail-closed`.
- A config read/parse/preset validation failure is not an empty `mcp_servers`
  document. Strict reconciliation retains live, lazy and connecting servers,
  logs once per config revision, and resumes normal removal after repair.
- Owner: `tools/mcp_tool_config.py` and `tools/mcp_tool_discovery.py`.
- Guard: `tests/tools/test_mcp_invalid_config_reconcile.py` runs the real gateway
  housekeeping callback against invalid on-disk documents and a repaired file.
- Retire when upstream distinguishes unreadable config from an intentional empty
  desired set and the same regression passes.

## Distinct Config Error Notice

- Fork patch identity: `config-error-notice`.
- Typed config/preset validation failures produce `Hermes config is invalid;
  fix config.yaml`, not a provider sign-in notice. Arbitrary validation details
  stay in the operator log, never in the transport reply. Pre-turn and in-turn
  handlers log once per profile config revision. Unrelated failures retain
  existing classification, failed-turn bookkeeping and sanitized messages.
- Owner: `hermes_cli/config_errors.py`, `gateway/run_turn_runner.py`,
  `gateway/run_turn.py` and `gateway/run_turn_persistence.py`.
- Config load failures use generic `ConfigIssue.message` text and keep operator detail in
  `local_detail` for config show, doctor and stderr. Unknown-provider replies in model switching
  and auth do not copy structural issue messages or hints either, since those can quote authored
  values. The generic repair hint is enough for transport callers.
- Guard: `tests/gateway/test_config_error_notice.py` covers chained pre-turn
  errors, both in-turn handlers, repeated turns and a changed config mtime.
  `tests/gateway/test_model_config_diagnostics.py` crosses the real model-switch reply boundary
  with private preset and authored-value markers, and checks both unknown-provider hint builders.
- Retire when upstream provides the same typed classification and log dedupe.

## Config Changes During Failed Cron Runs

- Fork patch identity: `cron-config-change-audit`.
- The actual cron execution owner snapshots config mtime and SHA-256 around
  agent and no-agent runs. A failed run with a changed document emits 1 warning
  naming the job and its ID. A successful run observing an invalid changed config
  also warns, without replacing its real outcome or final response. Warnings stay
  in operator logs and saved output. Failed runs also use the existing incident lane.
- This is an observation of the run interval, not attribution of a concurrent
  edit to that job. Successful valid edits and unchanged failures stay quiet.
- Owner: `cron/config_audit.py` and `cron/scheduler.py`.
- Guard: `tests/cron/test_config_change_failure.py` runs real config-mutating
  scripts through canonical bookkeeping and checks actual exit status, the
  warning text and its once-only log, plus a successful valid edit.
  `tests/cron/test_config_audit_result_preservation.py` retains real output and
  final responses in both agent and no-agent runs. Document-shape regressions
  reject non-mapping YAML roots rather than silently auditing an empty mapping.
- Retire when upstream observes config mutations at the same execution boundary
  with equivalent status and operator visibility.

## Post-Install Admission Canary

- Fork patch identity: `post-install-admission-canary`.
- A healthy process identity does not prove native ingress works. After release
  acknowledgement, update and guardian share a bounded refusal canary and the
  existing guardian rollback, reload and previous-release health proof. Failure
  persists counts, the dominant refusal clause and rollback health in a guardian
  receipt, and fails the updater without sending chat notifications.
- `gateway.guardian.admission_canary` defaults to `enabled: true`,
  `window_seconds: 180` (10–300) and `refusal_threshold: 10` (5–1000).
  10 refusals in 3 minutes catches broken ingress without treating isolated
  invalid input or a quiet gateway as failure. `enabled: false` disables it.
- `gateway.admission_canary.record_refusal` owns bounded, timestamped, content-free
  runtime-status evidence. The authority's `admit_native` and `admit_automation`
  boundaries count each refused attempt once with its source, reason and clause,
  including internal wakes and suppressed user notices. An automation refusal
  carries its producer identity, and retries of one identity (a watch notice
  requeued on backoff after a non-permanent refusal) count once per 600-second
  retention window: on 2026-10-29 one stuck pair of delegation-completion batches
  retried after a restart reset their backoff and tripped bff96cdf's canary.
  Regression: `tests/gateway/test_admission_canary.py::test_retries_of_one_refused_producer_identity_count_once_per_window`. Receipt-only producer
  scope also covers pre-admission authorization, codec preparation and retry
  conflicts. Nested scopes count once per task without suppressing later tasks.
  Codec, gate and ledger refusals share this hook, not the deduplicated
  presentation/logging paths.
  Only events from the acknowledged PID/code identity within the post-ack window
  count. `release-admission-canary.json` retains the terminal result across the
  rollback, and the shared guardian lock prevents duplicate rollback attempts.
- Proven absent/ended targets in the source's runtime home, explicitly suspended
  routes and pre-ledger routes with no native delegation are excluded from
  automation outage evidence. The latter use the permanent `not_found` refusal
  with field `automation_legacy_delegation`, before connector-identity checks so
  a real relay registered under `Platform.RELAY` takes the same settled fence.
  They reach the existing 8-attempt producer drop cap, never acquire
  delegated trust and need a fresh human admission before automation can succeed.
  Standalone restore proves its home with a wire-invisible stamp, not a
  RoutingIdentity. A wire-dict source or one proven to another home stays counted.
  Missing routes with a live durable target, native refusals, connector/auth
  failures and invalid producer payloads still count. A committed envelope waits
  for its relay adapter. Threshold, observation window and PID fence are unchanged.
- Rollback distinguishes launchctl probe failures from an absent supervised PID
  and a proven foreign owner. All fail closed before launchd mutation. Drain
  escalation reserves startup time using the smaller of 15 seconds or a quarter
  of the remaining rollback deadline, and logs a WARNING with the killed PID and
  its last-reported in-flight agent count (unknown for a nonmatching status PID).
- Guards: `tests/hermes_cli/test_admission_canary.py`,
  `tests/gateway/test_admission_canary.py`,
  `tests/gateway/test_automation_upgrade_admission.py`,
  `tests/gateway/test_legacy_delegation_refusal.py`,
  `tests/hermes_cli/test_rollback_diagnostics.py`, and existing guardian/release tests.

## Automation Wakes After a Route Rebind

- Fork patch identity: `automation-rebound-route`.
- Producer wakes (`/loop` ticks, delegation completions, process notices) borrow the
  route's last committed envelope. A session rebound to another route after its last
  human turn (`switch_session`, as the 2026-10-30 Slack cutover moves 142 Telegram topic
  owners to Slack threads) kept a committed envelope from the old route. Its provenance
  names a connector that no longer serves the session, so every wake was refused
  (`not_found` from provenance restore). A refused completion is permanent and reaches the
  drop cap. A refused `/loop` tick is skipped.
- `route_rebound(entry, envelope)` is true when the committed envelope's `route` is not
  the entry's current `session_key`. Then `snapshot_automation` and `admit_route_wake`
  use the current origin, revalidated by `revalidate_legacy_route` against the live
  adapter like native ingress. That path mints no proof, refuses relay, role and
  upstream-authorized origins, and keeps the sender and route comparison.
- Guard: `tests/gateway/test_automation_rebound_route.py`.

## Billing outage persistence and provider-wide classification

- Fork patch identity: `billing-outage-recovery`.
- A provider/account billing or rate-limit failure must retain the failed user turn, avoid the context-overflow auto-reset path, and classify provider-wide proxy `auth_unavailable` envelopes into the existing shared cooldown/fallback route. Explicit 5xx overload/529 signals arm the same breaker before local backoff. Generic server faults and request-specific validation/context failures do not create shared outages.
- Both persistence layers share `is_context_overflow_reason`: known classifications are authoritative; only missing/unknown classifications may use the legacy large-400 growth guard. Proxy upstream text is considered only within a 5xx `auth_unavailable` envelope's `last upstream error:` suffix, before generic provider-profile status verdicts.
- Guard: run `scripts/run_tests.sh` one file at a time for `tests/gateway/test_billing_failure_transcript.py`, `tests/agent/test_billing_proxy_error_classification.py`, and `tests/agent/test_billing_outage_shared_cooldown.py`. The latter drives two separate `AIAgent.run_conversation` processes against a loopback provider with the same `HERMES_HOME`; agent B makes no primary call after A records billing 400, proxy billing/rate-limit 503, or explicit overload. Also run the existing classifier, shared-primary-cooldown, fallback-exhaustion, and transient-transcript tests.
- Retire only when the released upstream passes these regressions without this patch. Roll back the code and focused tests together; no state migration or alternate cooldown store is introduced.

## Production-only unused imports

- Fork patch identity: `production-unused-imports`.
- The maintained fork had four imports that Ruff proved unused on the fork's current release baseline. Removing them changes no runtime behavior or public interface.
- Guard: `ruff check --select F401 agent/agent_init.py agent/codex_runtime.py agent/memory_manager.py` and `python3 -m py_compile` on the three files.

## Relay close-failure abort ordering

- Fork patch identity: `relay-close-failure-test-race`.
- Relay's managed stream can observe either deterministic abort ordering. In the
  worker-first ordering, the worker closes the stream, records
  `interrupt_stream_close_failed`, and poisons the request-client slot before the
  monitor enters its interrupt-abort path. In the monitor-first ordering, the
  monitor records `stream_interrupt_abort` and poisons the slot before Relay
  closes the stream; the resulting close failure can surface as
  `RuntimeError: internal error: RuntimeError: close failed` while the worker is
  advancing the managed iterator, skipping the worker's body-level branch. Both
  orderings are safe for request reuse because the first abort poisons the real
  slot, and any later abort targets the same client. The regression tests force
  each ordering with event synchronization and assert every wait succeeds.
- Guard: `tests/agent/test_request_client_reuse_abort_races.py`
  (`test_relay_managed_close_failure_poisons_request_client`,
  `test_relay_managed_close_failure_preserves_poison_when_monitor_wins`).

## Parked status hides a live gateway

- Fork patch identity: `parked-status-live-gateway`.
- Upstream tracking: [issue #124553](https://github.com/NousResearch/hermes-agent/issues/124553),
  filed with both current-main lifecycle regressions on 2026-09-26.
- `hermes gateway status` returned "parked" before inspecting processes. A
  gateway started with `--force` bypasses parking and leaves the marker, so it
  was hidden, including from `--deep` and `--full`. Status now reports the
  parked marker and still shows a running gateway.
- Guard: `tests/hermes_cli/test_gateway_multiplex_lifecycle.py`
  (`test_parked_status_still_reports_a_forced_gateway`).
- **Status:** Upstream-main comparison at
  `f974222e69278b856b1b75053774e10014c4797b` is source-equivalent and contains
  the same runtime-snapshot guard and forced-gateway regression. This is not a
  released-upstream retirement proof, so the fork patch remains active.
- **Retirement:** Retire only after the selected released upstream contains the
  equivalent behavior and the regression passes there without the local implementation.

## Stale Bot Desktop lease survives a rename

- Fork patch identity: `rename-stale-lease`.
- Profile teardown released the human Bot Desktop lease only when
  `runtime.stop()` signalled a live screen. An already-dead screen returns
  False, so a rename moved the stale human lease into the new profile, where it
  fenced the agent out. The lease is now released whenever one exists after the
  screen is stopped.
- Guard: `tests/hermes_cli/test_profiles.py`
  (`test_profile_rename_clears_a_stale_human_lease_when_the_screen_was_already_stopped`).

## Workspace pin misses a symlinked cwd

- Fork patch identity: `workspace-pin-canonical-cwd`.
- The workspace snapshot pin compared cwd spellings. The launch dir comes from
  `os.getcwd()`, which resolves symlinks (`/private/var` on macOS), while a
  bound cwd keeps its configured spelling (`/var`). One directory produced two
  keys, so a compaction rebuild re-probed git and changed the prompt bytes.
  Both the pin key and the persisted `Current working directory` are now
  compared as canonical paths.
- Guard: `tests/agent/test_compaction_prompt_rebuild.py`
  (`test_symlinked_spelling_of_the_launch_dir_replays_the_pin`).

## Provider config clone follows symlinks

- Fork patch identity: `clone-memory-config-no-symlinks`.
- `--clone` copied the active memory provider's `<provider>/` directory with
  `shutil.copytree`, which follows symlinks. A link inside it pulled the target's
  files into the clone, and a socket aborted the clone. Only real files and
  directories inside the source profile are copied now. Links and special files
  are skipped.
- Guard: `tests/hermes_cli/test_profiles.py`
  (`test_clone_config_never_follows_symlinks_out_of_the_provider_dir`).

## Restart leaves a parked profile stopped

- Fork patch identity: `parked-profile-restart`.
- Upstream tracking: [issue #124553](https://github.com/NousResearch/hermes-agent/issues/124553),
  filed with both current-main lifecycle regressions on 2026-09-26.
- `hermes -p <name> gateway stop` parks the profile and the host stops serving
  it. A later `gateway restart` then found no host serving the profile and fell
  through to the standalone restart path, so the profile stayed parked and
  stopped. Restarting a parked profile now unparks it and asks the host to serve
  it, the same as `gateway start`. A separate `--force` gateway keeps its own
  restart path.
- Guard: `tests/hermes_cli/test_gateway_multiplex_lifecycle.py`
  (`test_restart_after_stop_unparks_and_serves_the_profile`).
- **Status:** Upstream-main comparison at
  `f974222e69278b856b1b75053774e10014c4797b` is source-equivalent and contains
  the same parked-profile restart path and regression. This is not a
  released-upstream retirement proof, so the fork patch remains active.
- **Retirement:** Retire only after the selected released upstream contains the
  equivalent behavior and the regression passes there without the local implementation.

## Launchd restart tests depend on the host gateway

- Fork patch identity: `launchd-restart-test-isolation`.
- The invoking-profile restart tests pinned the restart and the verifier but
  left the real LaunchAgent pid and process-ancestry probes live. When the
  suite runs under a Hermes gateway, pytest descends from the supervised pid,
  so the updater correctly took the in-gateway self-restart branch and three
  tests failed. The fixture now pins both probes, and the enclosing-gateway
  branch has its own test.
- Guard: `tests/hermes_cli/test_update_launchd_restart_verification.py`
  (`test_restart_handed_to_the_enclosing_gateway_skips_verification`).

## /update never reports its result

- Fork patch identity: `slash-update-v2-lifecycle`.
- The detached update wrapper records completion only in
  `.update_process_exit_code` and deletes `.update_exit_code`, but `/update`
  still wrote a legacy pending record that waits on `.update_exit_code`. Every
  chat-started update therefore stayed pending until the 30-minute deadline and
  was then reported as unverified, even when it failed at once. `/update` now
  launches through the same v2 lifecycle as agent-requested updates, which also
  clears a stale completion marker and refuses a second concurrent request.
- Guard: `tests/gateway/test_update_lifecycle_notifications.py`
  (`test_slash_update_final_outcome_follows_the_detached_wrapper`).

## Deferred command races a queued follow-up

- Fork patch identity: `deferred-command-drain-order`.
- When both a deferred control command (`/compress`, `/undo`) and ordinary
  follow-up text were queued behind a turn, the in-band handoff started the
  follow-up first, and turn cleanup then started the command as a second task
  on the same session. The command ran after the prompt it was meant to precede,
  concurrently with it. The handoff now takes deferred commands first, and
  cleanup never spawns while a live successor owns the session.
- Guard: `tests/gateway/test_cancel_background_drain.py`
  (`test_deferred_command_runs_before_queued_prompt_with_one_session_owner`).

## CI baseline and source guard read the wrong state

- Fork patch identity: `ci-exact-head-baseline-and-new-source`.
- The release-baseline exemption for exact-head plugin admission read
  MAINTENANCE.md from the working tree and checked ancestry against the current
  HEAD, so uncommitted metadata could exempt a committed head's catalog entries.
  An explicit revision now reads its own committed MAINTENANCE.md and ancestry.
- The CI source mutation guard re-fingerprinted only the initial file list, so
  source created during setup or checks went unnoticed. It now re-enumerates.
- Guards: `scripts/ci/tests/test_plugin_admission.py`
  (`test_exact_head_baseline_comes_from_the_checked_revision_not_the_working_tree`),
  `scripts/ci/tests/test_portable.py` (`test_source_guard_detects_source_created_during_ci`).

## Fork-patch check fails on a separate-checkout gateway

- Fork patch identity: `fork-patch-check-external-fleet`.
- `scripts/check_fork_patches.py` compared every update-receipt fleet row's
  `code_sha` with the checkout, including rows the updater marks `external`
  (gateways serving a separate checkout it did not touch). The live fleet probe
  excludes those rows, so a successful update beside a legitimate second
  checkout always failed. External rows are now skipped.
- Guard: `tests/scripts/test_candidate_scripts.py`
  (`test_check_receipt_ignores_gateways_on_a_separate_checkout`).

## Portable CI lost the WAL-capable SQLite guard

- Fork patch identity: `ci-wal-capable-sqlite`.
- Retiring `tests.yml` dropped its "Check SQLite runs WAL" step while the
  replacement gate and nightly pinned uv 0.9.28 and CPython 3.11.14, whose
  every published build links WAL-reset-vulnerable SQLite 3.50.4. Hermes then
  runs DELETE mode and the WAL test arms skip, so CI could pass without
  exercising WAL. The pins are now uv 0.12.13 and CPython 3.11.16 (SQLite
  3.53.1), the pair upstream's retired workflow used; uv 0.9.28 cannot download
  any newer 3.11 patch. The Linux uv artifact checksums match the published
  `.sha256` files. Every portable Python lane fails closed on a vulnerable
  interpreter before running tests.
- Guards: `scripts/ci/tests/test_portable.py`
  (`test_python_lanes_require_a_wal_capable_sqlite`,
  `test_workflows_install_the_pinned_python`,
  `test_uv_pin_is_consistent_across_installers`).

## Launchd wrapped-child test predates update home scoping

- Fork patch identity: `launchd-child-test-home-scope`.
- `test_launchd_exclusion_protects_real_wrapped_process` (fork #52) predates
  upstream #93349, which stops only manual gateways whose live home the update
  owns. Its synthetic manual gateway had no readable home, so it was correctly
  left running and the test failed on macOS. The test now binds both candidates
  to the updating home, so the wrapped child survives only through the
  service-ancestry exclusion (verified: removing that exclusion fails the test).
- Guard: `tests/hermes_cli/test_gateway_launchd_supervised_child.py`.

## Desktop core E2E lost its only caller

- Fork patch identity: `ci-desktop-core-nightly`.
- Retiring `ci.yaml` removed the only caller of `e2e-desktop-core.yml`, a
  `workflow_call`-only workflow, so the deterministic Desktop core suite
  (transcript integrity, backend lifecycle and orphans, clarify/approval) no
  longer ran anywhere. Nightly now calls it and its `qualification` requires it.
  It also runs on demand, on `ubuntu-latest`, because this repository has no
  larger hosted runners.
- The same retirement (#62) deleted `.github/actions/retry`, which this suite
  and `live-providers.yml` still use, so both failed at their first install
  step. The upstream composite action is restored unchanged.
- Guards: `scripts/ci/tests/test_portable.py`
  (`test_every_reusable_only_workflow_has_a_caller`,
  `test_every_local_action_reference_resolves`).

## DMG install proof accepted an unrelated app

- Fork patch identity: `dmg-install-proof-provenance`.
- The macOS DMG driver treated any `/Applications/Hermes.app` as proof that
  the selected checkout's bootstrap had installed its desktop app. The proof
  now stays under `INSTALL_DIR` and requires the minimum app-bundle shape,
  while the desktop E2E caller uses the same scoped admission.
- Guard: `tests/scripts/install/test_dmg_install_driver.py` and `bash -n` on
  both macOS install drivers.
- Upstream status: the scoped test-driver fix is fork-local. Upstream has an
  open macOS installer/product-app ownership issue tracked in #125245; that
  runtime packaging change is separate from this proof-boundary repair.

## Media send checks flood cooldown after chat lock acquisition

- Fork patch identity: `telegram-media-flood-under-lock`.
- A media send checked the shared flood cooldown before entering the per-chat send lock. When it queued behind a text send that armed a 120-second window, it later acquired the lock and uploaded anyway. Media sends now recheck the cooldown inside the serialization boundary before the API call and before the topic-anchor retry.
- Guard: `tests/gateway/test_telegram_flood_coherence.py`
  (`test_media_queued_behind_send_lock_rechecks_flood_cooldown`).

## Hindsight session lifecycle loses bank isolation, buffered turns, and recall scope

- Fork patch identity: `hindsight-session-lifecycle`.
- A session switch kept the prior template-derived bank, shutdown discarded
  turns below the retain batch boundary, and a prefetch worker outliving the
  switch could inject the prior session's recall. Switch now rotates the bank
  after queuing old-bank writes and invalidates old prefetch workers; shutdown
  enqueues the unretained tail before stopping the writer.
- Guards: `tests/plugins/memory/test_hindsight_provider.py`
  (`test_session_template_rotates_bank_without_redirecting_queued_writes`,
  `test_shutdown_flushes_buffered_tail`,
  `test_slow_old_prefetch_cannot_repopulate_new_session`).

## Alias-cache isolation guard scanned nothing from a worktree

- Fork patch identity: `alias-cache-guard-discovery`.
- The DIRECT_ALIASES in-place-write guard skipped any path whose absolute parts
  contained `.worktrees`, so from a linked worktree (the fork's standard review
  and sync checkout) it scanned zero production files and passed vacuously.
  Discovery now filters repository-relative parts, and a coverage assertion
  requires the scan to reach the alias-cache owner.
- Guard: `tests/hermes_cli/test_model_alias_credentials.py`
  (`test_the_scan_covers_the_alias_cache_owner`).

## Absurd flood penalty stranded a failed delivery row

- Fork patch identity: `delivery-ledger-absurd-flood-at-failure`.
- A multi-hour flood refusal has no retry deadline, so `pending_retries()` skipped the row, the
  redelivery timer exited, and `sweep_failed_for_runtime()` never ran to abandon it: the row stayed
  `failed` with no warning. `mark_failed()` now abandons such a row and logs the bounded failure at
  the moment the refusal is recorded.
- Guard: `tests/gateway/test_delivery_ledger.py`
  (`test_absurd_penalty_is_abandoned_when_the_failure_is_recorded`).

## Hindsight capability cache leaked between tests

- Fork patch identity: `hindsight-session-lifecycle`.
- The `update_mode='append'` capability is cached process-wide per (API URL, key), and every
  Hindsight test fixture shares one URL and key. A capability test's mocked "modern API" answer
  therefore decided later tests' document IDs, making `TestSyncTurn` order-dependent, and tests
  that never patched the probe contacted whatever listened on the fixture URL. The autouse
  fixture now gives each test a fresh cache and a legacy-API default probe. Providers were also never
  shut down, so their retain writer threads (23 after a full module run) outlived the test and, with
  the retry backlog, kept retrying queued jobs under restored globals; the autouse
  `_stop_retain_writers` fixture joins every provider's writer while the test's patches still apply.
- Guard: `tests/plugins/memory/test_hindsight_provider.py`
  (`test_capability_cache_does_not_leak_between_tests_first`/`_second`; `_stop_retain_writers` fails
  a test whose writer does not stop).

## Telegram album flood refusal read as a permanent failure

- Fork patch identity: `telegram-album-flood-contract`.
- `send_multiple_images()` caught a media-group flood refusal (local or platform) in its generic
  handler, fell back to per-image sends that were refused again, and returned
  `all images failed to send` with no `retry_after`, so the caller saw a permanent failure. The album
  path now arms the per-chat window, skips the futile fallback, and answers a wholly undelivered album
  with `flood_control:<s>`, using the longer of the window and the platform's own remaining deadline
  (the window caps at 300s, so a multi-hour penalty still reaches the ledger intact). Every refusal
  records that deadline per chat, so per-image fallback and animation refusals report it too. `_telegram_retry_after()` also honours a
  `timedelta` `retry_after` (PTB_TIMEDELTA) instead of shrinking it to one second.
- Guard: `tests/gateway/test_telegram_flood_coherence.py`
  (`test_album_inside_an_armed_window_returns_the_flood_contract`,
  `test_album_refused_by_the_platform_returns_the_flood_contract`,
  `test_timedelta_retry_after_keeps_the_full_penalty`,
  `test_album_reports_the_platform_penalty_beyond_the_local_window_cap`,
  `test_album_fallback_route_reports_the_platform_penalty`,
  `test_animation_only_album_reports_the_platform_penalty`).

## Update notice held or mislabelled at completion

- Fork patch identity: `update-lifecycle`.
- `_watch_update_progress()` left a whitespace-only unread output suffix in its buffer (the flush
  sends only non-blank text) while the completion branch waited for an empty buffer, so a trailing
  newline after the last flush held the final notice until the 30-minute deadline. Blank output is
  now consumed and checkpointed without sending. `_update_result_heading()` also labelled every
  same-SHA run "Already Latest … the gateway was not restarted", including checkout repair or fleet
  catch-up runs that did restart and verify the gateway; it now uses `final_outcome()`'s no-op rule
  (same revision, no restart, no fleet) and reports a verified same-revision restart as complete.
- Guard: `tests/gateway/test_update_lifecycle_notifications.py`
  (`test_whitespace_only_trailing_output_does_not_hold_the_final_notice`) and
  `tests/gateway/test_update_result_heading.py`
  (`test_same_revision_with_verified_restart_is_not_reported_as_noop`).

## Boot recovery notice suppressed while adapter disconnected

- Fork patch identity: `delegation-explicit-resume`.
- Auto-resume authorization consulted adapter-owned policy before the transport connected;
  an apparent refusal then consumed the one-shot trigger permanently. A closed parent remains
  terminal, but an unavailable adapter now defers without claiming, and authorization is checked
  only after the delivery route is live.
- Guard: `tests/gateway/test_delegation_auto_resume.py`
  (`test_boot_notice_defers_disconnected_owner_then_delivers_after_reconnect`).

## Update output repeated after partial chunk delivery

- Fork patch identity: `update-lifecycle`.
- A failed later progress chunk retried the entire buffered output; final-send also retained its
  original byte offset. Both paths now checkpoint exact raw-byte progress per successful chunk,
  preserving invalid UTF-8 and stripping ANSI without splitting an escape at a chunk boundary.
  The final notice waits for all output to be delivered.
- Guard: `tests/gateway/test_update_lifecycle_notifications.py`
  (`test_stream_retry_only_sends_unsent_chunk`,
  `test_final_retry_only_sends_unsent_chunk_and_then_final`).

## Hindsight config parsing and failed append retains

- Fork patch identity: `hindsight-session-lifecycle`.
- Three defects in the Hindsight provider. `recall_tags` stayed a string although the schema documents
  comma-separated tags, so the SDK's `RecallRequest` rejected every recall (auto and tool); it is now
  normalized like `retain_tags`. `_resolve_bank_id_template()` caught only `KeyError`/`IndexError`, so a
  template with unmatched braces raised `ValueError` and stopped initialization instead of using the
  fallback bank. The writer discarded a failed retain job although append mode had already dropped
  those turns from `_session_turns`, so a transient outage lost conversation memory permanently. Failed
  jobs now stay in an ordered, bounded backlog (5 attempts with exponential backoff, at most 50 jobs)
  that retries oldest-first once its backoff expires (newer jobs queue behind it, never forcing an
  early retry), with one last attempt at shutdown. The prefetch drain barrier counts that backlog, so
  recall does not read before a pending retain lands.
- Guard: `tests/plugins/memory/test_hindsight_provider.py`
  (`test_malformed_bank_id_template_falls_back`, `test_csv_recall_tags_reach_the_sdk_as_a_list`,
  `TestRetainRetry`, including `test_queued_jobs_do_not_bypass_the_retry_delay` and
  `test_prefetch_barrier_waits_for_a_pending_retry`).

## Telegram legacy links lost titled or angle-bracket destinations

- Fork patch identity: `telegram-link-targets`.
- The unsupported-link scrubber parses CommonMark destinations (`[t](url "Title")`, `[t](<url>)`) with
  `_markdown_link_target()` and keeps such links, but the legacy MarkdownV2 converters in
  `format_message()` re-validated the raw group: an ordinary link with a title or angle brackets lost
  its URL and became plain text, and an explicit numeric citation shipped the title inside the Telegram
  URL. Both converters now validate and emit the parsed destination, and a citation with an unsupported
  target degrades to its number.
- Guard: `tests/gateway/test_telegram_unsupported_link_targets.py`
  (`test_link_with_title_keeps_its_url`, `test_angle_bracket_destination_keeps_its_url`,
  `test_citation_with_title_does_not_put_the_title_in_the_url`,
  `test_citation_with_unsupported_target_degrades_to_its_number`).

## Hindsight timed-out retains resent and empty recall_types overridden

- Fork patch identity: `hindsight-session-lifecycle`.
- `_run_sync()` stops waiting at the provider timeout but leaves the coroutine running on the shared
  loop, so the retain backlog could resend an append that later landed, writing the same turns twice.
  A timed-out send is now always kept with its job (even if it completed as the wait timed out): the
  retry first lets it settle (one more timeout) and judges it by its own outcome, skipping the resend if
  it landed, resending only if it failed, and otherwise failing the attempt and backing off. An
  explicit `recall_types: []` was also replaced with `["observation"]`, unlike the equivalent empty
  string; only an unset key now gets the default.
- Guard: `tests/plugins/memory/test_hindsight_provider.py`
  (`test_timed_out_write_that_lands_is_not_sent_again`, `test_timed_out_write_that_failed_is_sent_again`,
  `test_wait_timeout_racing_completion_still_hands_over_the_future`,
  `test_retry_judges_the_earlier_send_by_its_outcome_not_the_wait`,
  `test_explicit_empty_recall_types_disables_the_filter`).

## Hindsight stale prefetch overwrite and cross-bank retain-op status

- Fork patch identity: `hindsight-session-lifecycle`.
- `queue_prefetch()` captured the prefetch generation without advancing it, so within one session a
  recall worker that outlived `prefetch()`'s 3s join could publish its older result over a newer
  request's completed recall. Each queued request now starts its own generation.
- Pending server-side retain operations shared one bank ID, overwritten by every retain. With a
  session-scoped `bank_id_template`, old-session ops were polled against the new bank, whose 404 reads
  as completion, so the recall-visibility barrier passed early. Each op now keeps the bank it was
  retained to.
- Regression coverage: `test_hindsight_provider.py` (`test_pending_ops_are_polled_against_their_own_bank`,
  `TestPrefetchSupersession::test_query_queued_during_slow_recall_wins_over_it`).

## Hindsight self-parent lineage tag re-consolidated whole sessions

- Fork patch identity: `hindsight-session-lifecycle`.
- In-place compaction calls `on_session_switch` with the session id as its own parent, so retains
  gained a `parent:<self>` tag that a later switch or gateway restart dropped again. Hindsight treats
  any change to a document's tag set as a rescope: it deletes the document's observations and
  requeues every fact without logging it. On 2026-09-27 this requeued about 6,700 already
  consolidated facts from a few long Telegram sessions and turned the backlog drain into a rise. A
  parent equal to the session itself is now treated as no parent, in `initialize()` and on switch.
- Regression coverage: `test_hindsight_provider.py`
  (`test_in_place_compaction_keeps_lineage_tags_stable`).

## Telegram short media flood retry let other traffic into the penalty

- Fork patch identity: `telegram-media-flood-under-lock`.
- A media upload refused with a short `retry_after` (within the 5s inline cap) slept and retried in place,
  but the sleep ran after the per-chat send lock was released and armed no shared window, so a concurrent
  text send, edit or typing request could reach Telegram inside the penalty and lengthen it. The retry now
  holds the chat's send lock across its wait and arms the shared window for it, releasing only its own
  window before the retry.
- Regression coverage: `test_telegram_flood_coherence.py::test_short_media_flood_wait_holds_other_outbound_traffic`.

## Desktop orphan reap crosses isolated gateway homes

- Fork patch identity: `gateway-orphan-reaper-home-scope`.
- A Desktop backend starts its unsupervised-orphan sweep with no profile PID record.
  The fallback command-line scan matches every default-profile `gateway run` process,
  even when that process was launched with another `HERMES_HOME` in its environment.
  Starting an independent Desktop backend therefore SIGTERMed a healthy gateway
  during the two-tenant E2E run, which entered drain and returned HTTP 503.
  Before signalling, the reaper now compares an explicitly advertised process
  home with its own; unknown/inaccessible process environments preserve the
  prior best-effort behavior. The unrelated AF_UNIX path-length warning did not
  activate drain.
- Guards: `tests/hermes_cli/test_gateway.py`
  (`test_desktop_reaper_does_not_signal_another_home`), plus concurrent
  `tests/e2e/core/tenancy/test_two_tenant_gateway.py` and
  `tests/e2e/core/tenancy/test_two_tenant_desktop_backend.py` under `-j 2`.
- Upstream `main` at `7b761da2de49` still has the same sweep; retire this patch
  once an upstream release protects the same cross-home boundary. No upstream
  issue or PR matched a targeted `gateway reaper HERMES_HOME orphan` search.

## Hindsight empty prefetch, unbounded status poll and unapplied bank missions

- Fork patch identity: `hindsight-session-lifecycle`.
- A newer prefetch whose recall came back empty did not replace an older worker's buffered result, so
  the next turn injected memories from the superseded query. Advancing the generation now clears the
  buffer, and the current generation publishes its result even when empty.
- The retain-drain deadline was checked only between retain-op status requests; each request ran with
  the full provider timeout (120s default), so a hung status endpoint overran the 10s prefetch drain.
  The remaining budget now bounds each status request and poll sleep.
- `bank_mission`/`bank_retain_mission` were stored but never sent (inherited from upstream, where the
  README says "Applied via Banks API"). They are now applied once per resolved bank through the Banks
  API before its first retain or reflect, best effort; concurrent callers for that bank wait until the
  attempt in flight has actually finished (one bounded budget, including a reconnect retry and a late
  landing), so none reaches the bank ahead of its missions.
- The local_embedded reconnect retry reused the caller's full timeout; one budget now covers the
  operation, so the retry gets only what the first attempt left and is skipped when it is spent.
- Regression coverage: `test_hindsight_provider.py`
  (`test_empty_newer_recall_does_not_inject_older_query_memories`,
  `test_drain_budget_bounds_the_status_request_itself`, `test_embedded_reconnect_*`, `TestMissionConfig`).

## Hindsight atexit pin and disabled mode reaching the network

- Fork patch identity: `hindsight-session-lifecycle`.
- `shutdown()` left the bound `_atexit_shutdown` registered, so every evicted gateway session's
  provider (transcript buffers, callbacks) stayed reachable until process exit. Shutdown now
  unregisters it; a later retain re-registers.
- `_mode = "disabled"` (local runtime unavailable, or root) gated nothing: recall, retain and tools
  still ran, and `_get_client()` fell through to the cloud client against the configured endpoint.
  Disabled now short-circuits recall, auto-retain, tool calls, tool schemas and the system prompt
  block, and `_get_client()` refuses to build a client.
- The test module's autouse fixture stubbed `lazy_deps.ensure` but not `install_specs`, so with an
  outdated installed SDK, mocked tests ran `initialize()`'s auto-upgrade for real (download, env
  mutation). The fixture now stubs `install_specs`; upgrade tests still override it. The eviction test's
  250ms wall-clock bound is replaced by a poll count.
- Regression coverage: `test_hindsight_provider.py`
  (`test_disabled_provider_makes_no_network_calls`, `test_shutdown_unregisters_the_atexit_callback`,
  `test_default_fixture_never_installs_for_an_outdated_sdk`).

## Hindsight shared-loop startup race and stale parent on session switch

- Fork patch identity: `hindsight-session-lifecycle`.
- `_get_loop()` released its lock as soon as the loop thread was started, before the loop ran. A
  caller in that window saw `is_running()` False and replaced the loop, so one cached async client
  could span two event loops and an untracked loop thread stayed behind. Initialization now waits for
  the loop to signal it is running before releasing ownership.
- `on_session_switch()` only updated the parent when one was supplied, so switching from a branch to
  an unrelated session kept the old parent and later retains tagged it with the wrong lineage. An
  explicit empty parent on a switch to a different session now clears it; a rewind of the same
  session keeps it.
- Regression coverage: `test_hindsight_provider.py` (`test_shared_loop_is_not_replaced_during_startup`,
  `test_switch_to_unrelated_session_clears_the_old_parent`, `test_rewind_of_the_same_session_keeps_its_parent`).

## Fork test whitespace flagged by `git diff --check`

- Fork patch identity: `fork-ci-reliability`.
- Five fork-touched test files carried trailing blank lines at EOF or whitespace-only lines, so
  `git diff --check <release> <main>` over the retained fork delta was not clean. Whitespace only;
  no test logic changed.

## Scratch-dir setgid expectation depended on the temp directory's group

- Fork patch identity: `fork-ci-reliability`.
- The macOS portability shim assumed macOS always drops `S_ISGID`. The kernel only drops it on
  `chmod` when an unprivileged caller is not in the directory's group: under the system temp root
  (group `wheel`) the bit is dropped, under the per-user `/var/folders` temp dir (group `staff`) it is
  kept, so seven `tests/test_scratch_dir.py` tests failed wherever pytest's temp root was the per-user
  one. Sandboxed runs drop it even for the caller's own group. The expectation is now measured: the
  same mode is applied to a throwaway sibling directory on the same filesystem, and the bit is
  expected only if it survives.

## In-gateway launchd update recorded the wrapper, not the gateway, as restart-pending

- Fork patch identity: `update-lifecycle`.
- A `hermes update` running inside the gateway's process tree hands the restart to that gateway and
  marks it restart-pending so the fleet matrix does not call it stale. On macOS launchd supervises the
  `osascript` wrapper (`osascript` → `stderr_timestamp` → gateway), so only the wrapper pid was
  recorded while the matrix row carries the gateway pid. Every in-gateway update therefore printed
  "STALE (pre-update code)", exited 1 and wrote a `partial` receipt although the restart completed.
  Every process between the supervised pid and the updater is now recorded as pending.
- Regression coverage: `test_fleet_matrix_self_restart_pending.py`
  (`test_launchd_wrapper_pid_still_marks_the_gateway_below_it_pending`).

## Update notice treated a restart-pending receipt row as a failed update

- Fork patch identity: `update-lifecycle`.
- Once the updater recorded the enclosing gateway as `restart_pending`, its exit code and receipt
  became `success`. The gateway's final notice still required every fleet row to be `current`, so
  each `request_update` promotion sent "❌ Update Failed … did not confirm the updated revision". The
  receipt cannot prove that row: the gateway restarts only after the updater exits. The notice now
  judges that row by the replacement gateway for the same home. Success needs a live, identity-verified
  replacement reporting the expected revision. Old code is still a failure. While the recorded
  process still serves, or no gateway is up, the notice stays pending until its existing deadline.
  Every other row keeps the strict `current` check.
- Regression coverage: `test_update_lifecycle_notifications.py`
  (`test_self_restart_pending_is_judged_by_the_replacement_gateway`), plus a replay of the
  2026-09-25 10:09 receipt: the old code reports failure, the patched code reports success
  (`b6fb36d94a21`).

## Finished update notice retained past the watcher deadline

- Fork patch identity: `update-lifecycle`.
- A finished v2 marker whose final send was flood-refused once blocked new requests forever:
  the watcher expired, boot was the only retry, and `O_EXCL` refused admission.
  `launch_native_update` now atomically replaces only an unclaimed marker with a matching
  finalized outcome and real process-exit sentinel. The old result and reason ride in
  `previous_outcome` and appear in the next request's final notice; claimed or unfinished
  updaters still block. Marker writes and clears compare identity under the admission lock.
  Phases, output checkpoints, and final sends retain the starting identity across awaited
  adapter sends, so an in-flight old notice cannot adopt, announce, or alter the new request.
  Housekeeping retries post-deadline notices with persisted exponential backoff honoring
  the platform's `retry_after`; raised delivery errors in phase acknowledgements,
  output chunks and final notices use the same backoff as failed send results.
  A never-connected adapter retains its existing expiry. No profile state or
  updater process is restarted by these retries.
- Guards: `tests/gateway/test_agent_update_launcher.py` (finished, unfinished, claimed,
  concurrent and failed-spawn admission) and
  `tests/gateway/test_update_lifecycle_notifications.py` (post-deadline retry, flood
  delay, stream/final retry, adapter expiry,
  `test_post_deadline_delivery_exception_persists_retry_backoff` for output,
  final notice and phase acknowledgement, and
  `test_inflight_old_notice_cannot_mutate_superseding_request` for final notice,
  final output, phase acknowledgement and watcher checkpoint).
- Related upstream [#42191](https://github.com/NousResearch/hermes-agent/pull/42191) preserves state after a soft send failure but does not admit a subsequent request or schedule post-deadline retries; [#111307](https://github.com/NousResearch/hermes-agent/pull/111307) addresses a distinct `fleet_restart_pending` warning. Both were open at qualification, neither is an equivalent released replacement.
- Retire only when an upstream *released tag* has equivalent finished-marker admission with old-outcome delivery, race-safe claim protection and periodic flood-aware retry after watcher expiry. Roll back this patch as a unit; do not delete an existing pending notice to work around admission.

## Restart notices hid the reason from other interrupted chats

- Fork patch identity: `update-lifecycle`.
- An agent-requested update stored a reason, but only the originating conversation saw it. Every
  other chat interrupted by the same restart received a generic notice, and a direct agent restart
  (`request_restart`) had no way to carry a reason at all. By owner decision, the reason is not
  private: the shutdown notice now leads with `🔄 Restarting` and the reason in every interrupted
  chat. `request_restart()` accepts an optional `reason` for direct restarts. Without a reason
  the notice is unchanged.
- Regression coverage: `test_update_lifecycle_notifications.py`
  (`test_update_restart_reason_reaches_every_interrupted_conversation`,
  `test_direct_restart_reason_reaches_interrupted_conversations`). Both fail on the base.

## Concurrent cron runs share one local shell

- Fork patch identity: `cron-run-terminal-isolation`.
- Cron runs carry no session key, so every session-less run collapsed onto the shared `"default"`
  local terminal environment, whose shell snapshot keeps exports between commands. On 2026-09-24 a
  fork-sync cron job exported `PATH=~/Repos/hermes-agent/.venv/bin:$PATH`; the concurrent
  `curate-skills` run's bare `hermes` then resolved to that stale checkout and rejected
  `hermes observations` as an unknown command. `_CronRunScope` now registers its run task id, and on
  the local backend a registered session-less run keys its own environment; its `delegate_task`
  children still share it through the container alias. Docker, SSH and other sandboxed backends keep
  their shared or profile container contract. Upstream `main` still collapses these runs; retire
  this when upstream keys session-less scheduled runs separately.
- Regression coverage: `tests/tools/test_shared_container_task_id.py`
  (`test_cron_run_does_not_see_another_runs_exports`,
  `test_concurrent_cron_runs_get_distinct_environments`,
  `test_cron_subagent_shares_its_parent_run_environment`,
  `test_persistent_docker_cron_run_keeps_the_profile_container`).

## Fork-patch check pins an owner-chosen setting

- Fork patch identity: `fork-patch-check-owner-config`.
- `scripts/check_fork_patches.py` required `auxiliary.background_review.enabled`
  to be `false`. That is an owner preference, not a fork-patch invariant, and
  the owner enabled background review on 2026-09-24, so `verify-hermes-fork`
  failed every day on a correct config. The key is no longer pinned. The
  remaining expectations guard fork behavior (`memory.write_approval`) or
  required routing (`delegation.model`, `auxiliary.review.model`).
- Guard: `tests/scripts/test_candidate_scripts.py`
  (`test_check_config_does_not_pin_background_review`).

## Live vault regression launched the user's branded Chrome

- Fork patch identity: `live-vault-test-browser`.
- The live vault regression fixture searched PATH and then fell back to the installed macOS
  `Google Chrome.app`. Each forced teardown could leave a `code_sign_clone` copy behind. The
  fixture now resolves Playwright's bundled Chromium and skips outside CI when that test browser
  is absent, so it never launches the user's signed-in browser or its code-signing clone. The
  opt-in browser supervisor integration suite also uses the bundled Chromium instead of a
  PATH-resolved branded browser. The pinned dev dependency and Python shard provisioning install
  Chromium on the hosted gate; CI fails if it is missing rather than silently losing coverage.
- Regression coverage: `tests/tools/test_vault_shadow_dom_live.py`,
  `tests/tools/test_browser_supervisor.py` (opt-in).


## Kanban worker exit trailers identify the worker

- Fork patch identity: `kanban-worker-exit-pid`.
- Per-task worker logs are append-only across retries. The exit trailer previously carried only the
  return code, so a fresh dispatcher process could read an exit code from another retry when it
  reaped a dead PID. Trailers now include the worker PID and the dispatcher selects the matching
  trailer, while retaining a legacy fallback for old logs.
- Guard: `tests/hermes_cli/test_kanban_worker_exit_trailer.py`
  (`test_logged_exit_code_matches_worker_pid_across_appended_retries`).

## GPT-6.1 Sol support on the Codex route

- Fork patch identity: `gpt-61-sol-support`.
- The maintained runtime did not recognize `gpt-6.1-sol`, so model metadata fell back to 256K and triggered compaction at 192K on the Codex route. The support correction registers the direct 1.05M context, the 272K Codex context, the model's reasoning and pricing metadata, and the static catalogs. It preserves the later Portal catalog behavior and does not claim an unverified 900K Codex variant.
- Guard: `tests/hermes_cli/test_gpt6_tiers_registration.py` (`test_gpt61_sol_takes_astra_ladder_without_astra_gating`, `test_openrouter_omits_disable_the_openai_ladder_rejects`, `test_gpt61_sol_resolves_context_and_pricing_like_its_tier`).

## Native Checkpoint Update Admission

- Fork patch identity: `update-lifecycle`.
- The checkpoint adaptation had lost upstream's active Git-operation refusal even though its native helper and regression tests remained. Restore the guard before any release recovery, snapshot, fetch or checkout mutation. Immutable rollback remains exempt because it restores the release transaction without changing the source Git checkout. This preserves the behavior introduced by upstream commit `353ac62b316c7b420858a708489cf009ebf918f4` by JoaoMarcos44.
- Historical source fleet catch-up now enters the native `_old_updater.stop_for_relaunch` completion route. The selected fresh interpreter owns dependency installation and fleet verification. The removed duplicate process scan/restart implementation must not return. Immutable catch-up retains its transaction acknowledgement path.
- Existing invariants: `tests/hermes_cli/test_update_parked_branch_guard.py`, `test_update_fleet_restart_pending.py` and `test_update_head_moved_gate.py`. The HEAD fixture advances only after the actual fast-forward merge and observes the current `_complete_source_update` owner.

## Darwin State-Database Holder Admission

- Fork patch identity: `sqlite-darwin-holder-scan`.
- The macOS foreign-holder scan used psutil path metadata for every process's open files, then resolved every path. An unrelated protected or unreachable file could therefore refuse or stall structural maintenance of a healthy isolated database. Match the existing libproc descriptor identities against the watched SQLite family instead. Current hardlink aliases and retired generations remain holders. A failed or interrupted enumeration retains the unknown-holder sentinel alongside any known holders. The real-home I/O tripwire remains enabled.
- Current checkpoint owner: `hermes_state_holders.foreign_state_db_holders` and the existing `hermes_state_dbfile._iter_darwin_fd_targets`. Related upstream [issue 130610](https://github.com/NousResearch/hermes-agent/issues/130610) and [PR 130616](https://github.com/NousResearch/hermes-agent/pull/130616), commit `d4a9f5ee12c90686dffd6582df29b7abe74cad51` by Yuan Li, independently choose the same native scanner. The fork adaptation also preserves retired-path detection and propagates scan exceptions to the existing fail-closed authority. It does not import the proposed timeout constant absent at this checkpoint or add a parallel scanner.
- Two native invariant functions in `tests/hermes_state/test_state_db_holders.py` reproduce the failure before the change and exercise real foreign descriptors plus interrupted enumeration. Existing repair, FTS, vacuum and deleted-WAL suites verify sibling admission paths. Retire this patch when a selected upstream revision uses the native scanner with equivalent descriptor and uncertainty contracts.

## Inline Launcher Relaunch Replayed Consumed Arguments

- Fork patch identity: `update-lifecycle`.
- A release-origin `hermes update` re-enters the source checkout with `python -c "...sys.argv.pop(1)..." <source> update ...`. `hermes_bootstrap` then relaunched that `-c` program under the managed interpreter with the already-popped `sys.argv`, so the replayed code popped again and consumed `update`. `hermes update --check` and `--plan` exited 2 with "unrecognized arguments", which made the gateway `request_update` tool fail with `update_check_failed`. `relaunch_command` now rebuilds a `-c` relaunch's argv from the original command line.
- Guard: `tests/hermes_cli/test_venv_sync_relaunch.py`.

## Stdio Wrapper Chain Recursion

- Fork patch identity: `stdio-wrapper-chain`.
- `agent.process_bootstrap._install_safe_stdio` (run on every `AIAgent` init) wrapped the thread-routing proxy that `thread_scoped_silence` installs in a `_SafeWriter`. The next silence then saw a non-proxy `sys.stdout` and installed a new proxy over the wrapper. Every agent init followed by background review or code-execution RPC therefore added two layers. In a long-lived gateway the alternating `__getattr__` chain reached the recursion limit. On 2026-10-05 `review_candidate` and subagent construction failed at `agent_init._setup_logging` with `maximum recursion depth exceeded` across many sessions. Safe stdio now leaves the routing proxy unwrapped, since it already tolerates a dead target, and the proxy installer adopts a proxy directly under one transparent wrapper. Upstream `main` has the same code.
- Guard: `tests/agent/test_thread_scoped_output.py` (`test_safe_stdio_and_silence_do_not_grow_a_wrapper_chain`).

## Update notice wedged on a routeless marker and a pm-overwritten receipt

- Fork patch identity: `update-lifecycle`.
- A `request_update` issued from cron or the CLI writes a pending marker with no `platform` or
  `chat_id`. When no target resolved, the final notice called `Platform(None)`, raised, and retried
  forever, so the admission marker never cleared and every later update was refused as already
  pending (2026-10-08, six attempts after the 05:22 promotion). A routeless marker now waits for the
  update to finalize, then releases its admission without sending anything.
- The notice also read `logs/update_receipts/latest.json`, which `pm` sync and plugin-check
  receipts replace. A sync finishing after the update made a successful update read as
  `Updater outcome: ok. Runtime completion is unverified.` When `latest.json` holds a `pm` receipt
  (it carries `kind`), the notice now reads the newest per-run `update_*.json` instead. An
  update-owned `latest.json` stays authoritative because the live-fleet settle rewrites only it.
- Regression coverage: `test_update_lifecycle_notifications.py`
  (`test_unroutable_marker_waits_for_the_outcome_then_clears`,
  `test_final_outcome_reads_the_update_receipt_after_a_pm_sync_overwrites_latest`), both red on the
  base. A replay of the real 2026-10-08 marker and receipts reports success on `fa95c7c4`.

## Failed Update Notices Explain Unchanged Revisions

- Fork patch identity: `update-lifecycle`.
- A nonzero updater exit previously skipped its finalized receipt and sent only an unverified-runtime warning. The notice now reads fresh, request-bound evidence before classifying failures. A failed or partial receipt with matching valid pre/post SHAs, positive working-tree proof (no autostash needed, edits restored, or the immutable apply explicitly left the source untouched), no release transition, restart, fleet mutation, or pending runtime transaction reports `Nothing changed. Still on <sha>. Cause: <reason>`. Immutable homes name their ready active release rather than the frozen updater checkout.
- Meaningful `stop_reason` or restart-error lines are joined and bounded to 240 characters after strict support-text redaction. Dependency validation retains its exception head and the offending requirement, while progress timings, incompatibility counts and the generic retry hint are omitted. Package-conflict lines retain priority over verbose preambles. Command-boundary `sys.exit(N)` text is skipped in favor of a readable `stop_class` explanation, then the exit-code fallback. Traceback headers and their following frame/source lines are never sent. An interrupted-pull marker, source-completion debt, or host restart obligation also prevents the unchanged claim, even when HEAD has already returned to the starting SHA. Missing, malformed, stale, contradictory, or mutation-bearing evidence retains the unverified-runtime warning and never claims an unchanged version.
- A failed update can park local edits while leaving HEAD unchanged and clearing recovery markers. The updater records a clean-tree fact when it takes no autostash, and a pending disposition as soon as an autostash is taken. The shared receipt-finalization boundary records any still-pending stash with the existing `local_changes_stash` step before sealing either an inner or command-boundary receipt. Missing positive proof fails closed to the unverified-runtime wording, not an unchanged claim. Successful restores, explicit park choices and discards replace the transient pending step, leaving no stale failure for receipt consumers. Finalization suppresses stash-recorder failures under the receipt module's never-raise contract. Pending `user_action`, parked or discarded edits, and unconfirmed restores prevent the unchanged claim. Parked edits name the saved stash and restore command. `tests/gateway/test_update_failure_autostash.py` exercises real Git autostash, commit-point refusal, syntax rollback, CalledProcessError after autostash, rejected upstream targets, unsafe restores after no-op updates or failed branch switches, and torn branch-switch notices through receipt finalization and notification. Clean-checkout controls retain the useful unchanged explanation.
- ZIP fallback invalidates earlier unchanged-tree proof before settling an interrupted source swap. ZIP/non-git installs without a source checkout keep the unverified warning because source-keyed recovery debt cannot be excluded.
- Known conservative behavior: immutable updates whose preparation cleanup writes source files retain an `unverified`, `ok: false` `local_changes_stash` step even if the later fetch-only apply records `immutable_source_untouched`. This intentionally remains fail-closed: notices say runtime state is unverified, and `scripts/check_fork_patches.py` does not excuse a partial outcome as only fleet settle-window delay. Clearing that step requires proving the earlier cleanup preserved the starting source state, not merely that the later immutable apply left it alone.
- Immutable apply now records an `immutable_update` receipt fact: the active release identity (symlink text, resolved path, build sha, readiness-marker inode/mtime/size) read before the candidate build, the candidate path, and `activation_started: false`. Receipt finalization re-reads the identity into `after`. `activation_started` flips to `true` before the fleet-restart pending marker, not before staging. The notice accepts the immutable-path tree proof only when `activation_started` is exactly `false`, `before == after == the pointer read at notice time`, the candidate differs from the active release, and no staging runtime-mutation path started. Missing, older, or contradictory records stay unverified, and every other fail-closed check (release transaction, fleet marker, interrupted pull, completion debt, host obligation, `user_action`, runtime mutation, stash evidence) is unchanged. The proof identifies the release by pointer and markers. It does not hash the release tree, and it does not observe live processes, which the existing runtime-mutation checks still cover. Guard: `tests/gateway/test_update_failure_notice.py`.
- Staging a ready candidate can carry active extras with an eviction-enabled PM sync. Before that sync starts, `staging_runtime_mutation_started: true` invalidates the unchanged proof through the existing runtime-mutation checks. This conservatively covers profile `config.yaml` edits (disabled plugins and cleared `memory.provider`), candidate-keyed install facts/generations, and recovery of interrupted plugin publications, even if the sync fails or its worker loses the result. A fresh candidate dependency-build failure without this sync remains eligible for the unchanged claim. Failures before the `immutable_update` fact is recorded, including FETCH_HEAD resolution, invalid current pointers, and candidate selection, intentionally stay unverified, including when no current pointer exists yet. Missing or non-boolean staging evidence also fails closed. Guard: `tests/gateway/test_update_staging_failure_notice.py` publishes real eviction edits to 2 temporary profiles, then fails during sync, smoke, or candidate identity validation. It also covers fresh-build failures and early selection failures.
- Other staging writes outside the candidate are build-owned tools under `releases/.build-tools`, PM tool/runtime acquisition and download/uv caches, Windows ARM64 compiler state, temporary smoke sandboxes, and receipts/locks. Fresh environment builds do not discover profile plugins or publish application selections. These build/tooling artifacts are not evidence of a changed active application release. The notice does not claim filesystem-wide byte equality.
- The 2026-10-26 candidate-build failure remains unverified. Its receipt contains only 2 successful backup steps, equal source SHAs (`e8387e275844`), and no positive source-untouched fact or before/after active-release pointer and live-process identity. Its planned gateway code SHA (`c7c67d7aade8`) differs from those source SHAs, so source equality cannot establish the running revision. The existing immutable apply now records `immutable_source_untouched` before staging, but that cannot retroactively prove this older run or supply missing runtime observations. The redacted real-receipt fixture checks that even a ready pointer matching the source SHA does not manufacture an unchanged claim. Only receipts that carry the `immutable_update` fact can earn the claim.
- Regression guard: `tests/gateway/test_update_failure_notice.py` drives the Telegram completion path for legacy and immutable layouts and failed receipts with zero or nonzero process exits. Its immutable recovery cases use distinct release and journal-bound source roots, arm the updater's real source-keyed host record without legacy breadcrumbs, and check interrupted-pull and completion markers in the source. The shared host-path resolver accepts that resolved source root. Its default identity resolves the bound profile's installation root before consulting the migration journal, so default and named profiles share one host obligation under both environment and multiplex context binding. The real-resolver regression in `tests/hermes_cli/test_update_host_obligation.py` checks cross-profile write, read, clear and release against the gateway reader in source and ready immutable-release layouts. Explicit roots and unknown-layout/historical-import fallbacks retain their existing behavior. Existing successful no-op and runtime-verification contracts remain unchanged.
- Upstream status: no matching gateway failure-notice fix found. NousResearch/hermes-agent#111147 presents receipt summaries in the web UI, and #132110 improves receipt production for early failures. Neither consumes the gateway receipt on a nonzero process exit. Retire this adaptation when upstream supplies equivalent request-bound gateway behavior.

## Watchdog Dumps Preserve the Main Thread

- Fork patch identity: `watchdog-main-thread-dump`.
- CPython's `faulthandler.dump_traceback(all_threads=True)` stops after 100 threads, so a busy gateway can omit the event-loop thread from watchdog diagnostics. Each shutdown, loop-liveness, and startup watchdog dump now writes the main thread's Python stack and live thread count first, then retains the existing faulthandler dump and exit path.
- Guard: `tests/gateway/test_shutdown_watchdog.py` (`test_main_thread_stack_is_written_before_faulthandler_thread_limit`). Retire when equivalent upstream behavior is available.



## Display watcher duplicate profile homes

- Fork patch identity: `display-watch-duplicate-homes`.
- The Bot Desktop watcher combined the launch home with served profile homes without canonical-key deduplication. A served profile represented by an alias of the launch home could therefore be visited twice by each watcher pass and duplicate status delivery. Watched homes now deduplicate by `hermes_home_key`, while the test fixture clears process-global watcher maps between tests.
- Upstream status: no equivalent fix found on `upstream/main` or in upstream issue/PR searches for the display-watch duplicate symptom.
- Guard: `scripts/run_tests.sh tests/tui_gateway/test_display_watch.py`.

## First PM Sync Preserves Payload Extras

- Fork patch identity: `pm-shipped-extras`.
- When a release payload had shipped optional dependencies but PM had no facts or frozen feature inventory yet, the first on-demand extra sync selected only the requested extra and replaced the payload environment. Infer concrete shipped extras from the payload's site-packages before creating the first PM generation, while excluding umbrella aliases that share anchors with their member extras.
- Guard: `tests/pm/test_environment_build.py` (`test_first_on_demand_extra_preserves_payload_extras`).

## v0.22.5 merge integration

- Recall's read-only open must translate SQLite open errors to the shared
  database-unavailable result, including absent canonical storage, without
  initializing it. Its lifecycle double patches the opener rather than replacing
  the SessionDB type used by main's independent-reader scope. Main's running-agent
  lifetime fixture supplies the successor's display config and keyword-only
  fallback-policy argument, retaining every retirement assertion and timeout.
  Guards: session-search, read-only search, search-isolation review and running-agent
  eviction-memory suites. The proxy silence nodes require the declared aiohttp
  environment and run in Linux alongside the existing scope checks.
- Integrating main's affected-group display fold requires beginning the fold inside
  each compaction transaction, before mutation, including the successor's extracted
  on-connection entry point. Preserve carrier-by-UID identity and prune-history
  slots in the off-lock plan, affected groups and full-fold oracle. Keep the
  independent recall scope around both discovery and hydration, including managed
  worker and cross-profile read-only handles. Guards: display-fold incremental,
  one-step, summary-carrier, prune restart-safety and search-isolation suites.
- The successor extracted session creation into `tui_gateway/session_registry.py`,
  which replaced main #779's facade factories at registration time. Port its
  creation-time publisher into both extracted factories, so even a single-peer,
  stdio or compute-host session retains one ordered mailbox through membership
  changes and graceful final-event draining. Keep atomic prepare/admission,
  burst batching, bounded overflow resume notices and physical flush unchanged.
  Guards: unchanged session-event-publication, multi-client fanout, fanout burst
  batching and gateway session-events-overflow suites.
- Immutable release retention probes the executable and cwd of real processes.
  Its source and tests are unchanged from fork main `39358bac528d`, where the
  same 3 retention tests fail on macOS when a live process uses a release under
  the real Hermes home. The home I/O guard correctly rejects that metadata
  resolution. Treat this as parent-inherited host contamination, not fanout or
  extras-carry merge fallout. Keep the guard and tests intact and qualify the
  release-retention suite in disposable Linux without live-home mounts.
- The hosted-room and secondary-route scope suites import `aiohttp` at
  collection. An isolated core/dev/test environment without a messaging or
  slack extra cannot collect them. Use a disposable PM-built declared-extra
  environment to exercise these contracts, not a production dependency install
  or a test skip.

- The classic gateway goal proxy reuses the completion-history renderer added by
  autonomous goals. `/goal`, `/goal status` and `/goal show` remain local reads of
  mirrored gateway state, with no judge call or state write. Without the binding,
  status dispatch raised `AttributeError` before emitting any output while goal
  continuations kept running. Regression coverage exercises all 3 read spellings
  and verifies completion-refusal history, unchanged state and no gateway call.
  The existing Linux/tmux goal-status expectation and timeout remain unchanged.
  Linux Python 3.14.9 with the selected test environment and tmux required:
  old proxy reproduces the live timeout and 3 new read-command failures, then
  gateway automation, classic live chat, autonomy and goal dispatch pass 146 tests.
  Upstream preflight: guidance at `930e5ed557ed` and related goal-status issues
  preserve visible status reads. This is a fork #800 adapter omission, not a
  routing or pacing change, so the existing proxy read path remains the owner.

- The serve maintenance test waits for the completed consolidation-off summary,
  not the early run-count checkpoint plus a fixed delay. Linux reproduction
  delayed report writing after the checkpoint and observed the final summary in
  the same isolated home. Pause, active-turn suppression and exactly-once count
  assertions remain unchanged.
- Canonical compression fixtures bind authority profile IDs to their isolated
  database home instead of the relative placeholder `owned`. Real post-compaction
  plugin discovery initializes that profile and previously seeded `owned/SOUL.md`
  in the checkout. The live-memory-hook test now observes the actual background
  hook's home while still running discovery, so this isolation defect fails at
  its source without changing the CI modified-source guard.

- Completion-batch retry narrowing removes the original primary event, not the
  authority-only metadata copy. Already-admitted siblings are removed after their
  lost-ACK settlement too, so successful batches do not falsely request a retry.
  Guards: automation retry, completion admission and completion delivery suites,
  78 passing tests on Linux Python 3.14.9.

- Service identity fixtures use account-native home naming. Detached restart mocks only
  the gateway launch, leaving process scans real. The vanished-file race probes preflight
  directly before reopening because upstream serializes startup preflight and quarantine.

- Post-turn goal/loop bookkeeping retains the owner's failed-result fence even for text-only
  refusal returns. Soft eviction honors the memory manager's teardown claim during a reset's
  pre-extraction flush and cleanup captures its transcript before eviction clears the agent.
  The compression-lock fixture holds the lock until transport retry instead of exhausting
  a call-count list after the upstream added another pre-model fence.

- Compaction hook fixtures mark discovery complete before synchronous dispatch, matching
  the upstream plugin observer contract. Cron runner delivery assertions follow
  `cron/executions.py:750-763`, which intentionally projects terminal receipts into
  `delivery_outcome` for detached owners as well as gateways. The tmux requirement follows the fork's Python/e2e shard
  workflow and explicitly requires the TUI environment and verifies tmux before the shard.
- Integration fixtures supply the actual memory warning sink and retired-agent turn state.
  The 1Password service-account stub returns the exact revealed JSON item, preserving vault
  identity and purpose-selected password validation. Drain assertions follow upstream background
  delegation wording, including the worker PID, while retaining both fork timeout knobs.
  Guards: synthetic prompt, vault backends, drain report and model reset suites (1,410 passed).

- Local history preparation offloads receipt restoration and its guarded session-store lock.
  The synchronous snapshot re-reads the receipt target without repeating restoration under the
  viewer event lock, preserving live-row and deletion races. History, side-question and preview
  RPCs likewise resolve their target off-loop. The loop-thread guard stays unchanged.
  Guards: local recovery, compression resume, display-history race coverage, canonical history
  and fresh CLI first-message suites (26 passed, 13 platform skips), including a guarded-loop
  attach regression. Hosted qualification remains required.

- Worker route commits acknowledge the owner-loop state handoff before agent construction
  reads the decision and the expiry notice. Fire-and-forget activity receipts retain their
  monotonic merge. Guards: 274 tests across model-pick expiry and attended-model suites.
- The public replace seam now forwards to the extracted bootstrap implementation, restoring
  the durable ownership-refusal stamp while retaining process-slot PID isolation. Its scoped-lock
  release uses the canonical source module, not the removed status export.
  Guard: the complete replace-ownership suite, including stamp durability and no-signal refusal.
- Fork patch identity: `v0225-merge-integration`.
- The Windows gateway checks the legacy venv's Python major/minor before processing
  its site-packages or .pth files. Missing version metadata refuses the overlay,
  while same-ABI and committed-generation behavior remain unchanged.
  Guard: `tests/gateway/test_windows_venv_import_guard.py`.
- Integrating fork main through #818 keeps atomic fanout prepare/stamp/admission alongside
  overflow resume notices and their physical-drain barrier. Routing replacement performs its
  diff and retirement-fence check in SQLite and returns only rejected route keys, without
  fetching the live index under the writer lock. Residual compaction phase metrics retain the
  successor's held-row CAS and schema-32 display-only carrier projection. Active-release
  extras carry and candidate acknowledgement are inherited intact from #784.
  Guards: fanout burst/publication, routing diff and retirement scope, compaction metrics,
  carrier display/inspection and immutable-release extras suites.
- Memory-provider eviction resolves the shutdown handoff under the shutdown lock.
  A worker that captured the manager owns session-end and shutdown, while eviction
  detaches its claimed reference without taking that ownership back. Reset's
  pre-flush still retains the manager until the worker captures it.
  Guards: both `test_agent_cache_memory_lifetime.py` and
  `test_agent_cache_memory_lifecycle.py`, 18 passing tests on Linux Python 3.14.7.
- Linux service-identity fixtures bind both the account-native default and the
  process fallback to the simulated sudo home. Detached-launch mocks recognize
  the isolated bootstrap's embedded module name while leaving process scans real.
  Guards: gateway-service and no-unit-restart suites, including actual unit-home
  adoption and exact pinned launch arguments. The live-system guard is unchanged.
- Merging upstream `v0.22.5`, current fork main and the installed display-fold fix (#765) left five bindings that Git merged cleanly but that no longer resolved. Exact-head CI caught them on the first hosted run.
  - `gateway/run_inbound.py` kept the fork's `metadata=_UNSET` default on `_transcribe_and_echo_pending_voice` but lost the `from gateway.run_common import _UNSET` import that upstream's side never had. Every `gateway.run` import raised `NameError`, which accounted for most failures in the first run.
  - `hermes_state_messages.py` raised `CompressionLeaseLostError` from the compaction lease check without importing it.
  - `hermes_state_messages.py` `_with_removed_identities` received a copy of an `ORDER BY {...}` f-string expression as a plain literal, sending `{` to SQLite on every display read that includes removed rows. The removed-row query orders by plain `id` as on upstream.
  - `hermes_cli/cli_model_switch_mixin.py` kept upstream's trailing `restore_reasoning(...)` call after the fork had replaced that local with `_restore_reasoning()`, which already reapplies the snapshot after `switch_model`.
  - The desktop slash-session test imported in an order `perfectionist/sort-imports` rejects.
- Guard: `uvx pyflakes` over the merged tree reports no undefined name that is absent on both parents, plus exact-head hosted CI for `gateway.run` import and the display-read suites.
- The next hosted run exposed attribute and projection losses not visible to undefined-name scans:
  - Display-fold projections omitted the summary-carrier UID and flag. Both the off-lock plan and
    its stale-row fallback now use the same carrier identity and retained-slot rule as the full fold.
    After folding, display reads unwrap an assistant's composite carrier back to its original reply,
    while model reads retain the summary verbatim. Otherwise the selected carrier hides the reply
    as synthetic context even though the original row remains durably archived. Conversation
    display and atomic resume share this projection. Physical SQL message reads and exports
    retain the original carrier, and web history supplies its unwrapped reply via
    `display_content` without rewriting inspection/export `content`. Linux guards cover the
    complete transcript ledger (10 passed), manual compression (29), planned/full carrier folding
    (2), compacted reads (47), off-writer folding (26) and stale held-generation fences (7).
  - The fork turn mixin lost the upstream persistence mixin base, hiding usage-window wording and
    response-before-limit-offer methods. Existing fork overrides still win in the restored MRO.
  - Prune rewrite calls the shared upstream transcript transaction helper, retaining rollback row
    snapshots and the explicit session target rather than a removed private method.
  - Primary-runtime snapshot reads optional reasoning configuration once, after the guarded lookup.
  - `/allow` reads authorization environment names from the shared registry rather than removed maps.
  - Native startup recovery runs once per profile and offloads session-store reads, preserving the
    event-loop blocking guard. ACP unknown-state refresh uses per-input dict identities consistently.
  - Managed workers expose the upstream automatic-title collision read through a scoped, receipted
    owner operation. Only a boolean is returned and exclusion stays bound to the assigned session.
  - Fixtures follow the real `session_db` constructor, `_Batch` dataclass, blocked-goal hook, and
    authored max-iteration config. The deleted importer wrapper's config-write refusal is exercised
    on its unchanged shared `atomic_yaml_write` implementation. No assertion is relaxed.
  - Gateway contract, command-parity document and prompt-surface snapshots use their generators.
  - Fork turn overrides retained pre-extraction behavior after upstream moved shared phases into
    `run_turn_persistence`: restore local-viewer reasoning ownership, finite-turn footer suppression,
    interrupted tool-tail closure, and the extracted worker tracker without dropping fork overrides.
    Voice acknowledgement resolves the answering adapter rather than the default bot. Bind hygiene
    helpers through their owning seam. A second ACP voice-stop path uses the same dict cancellation
    identity, and the initial runtime snapshot tolerates reasoning config before initialization.
- Guards: display-fold off-lock and carrier suites, managed-store surface parity with Unicode
  collision and cross-session refusal coverage, usage-limit delivery, runtime bootstrap, ACP
  cancellation, provider fork contracts and retry/recovery admission tests, plus exact-head CI.
- Delegation child construction again supplies an empty request-override mapping, as on fork
  parent `9ac6acc2`, instead of `None`. Route-field fixtures explicitly opt into tier inheritance
  and set the parent's effective mode, preserving every route and wire-leak assertion.
  Guards: `test_delegation_route_fields.py` (15 passed) and `test_delegate_by_parent.py` (11 passed).
- Restore the fork's post-extraction inbound bindings: canonical `/learn` and `/plan` commands
  enter durable admission, queued command turns are not enqueued twice, and `/stop`/`/new`
  fence uncommitted arrivals while retaining paused unknown-turn followers. Turn preparation
  uses `session_opening_pending`, binds the event's turn origin, preserves FIFO lease waits,
  and defers behind detached compression ownership. The recovered-topic fixture patches the
  renamed lobby helper, with all opening-turn assertions unchanged.
  Guards: attended opening (12 passed), messaging slash FIFO (22), native arrival order (10),
  steer overwrite (2), busy anchors (13), and turn leases (12), via the canonical runner.
- The cron isolation fixture disables optional diagnostic audits on its synthetic repository before
  checking maintenance eligibility. Upstream's audit hook otherwise starts unrelated work while
  the fixture is proving that maintenance uses an isolated worktree. The real subprocess and
  maintenance assertions remain unchanged. Guard: `test_worktree_maintenance_isolation.py`
  passed in the canonical runner (`r5-fixtures.log`, 1 passed).
- Route fork plugin-command effects through the upstream shared handler so opt-in source,
  session ID, live-message bindings and choices survive without dropping the loop-wake fence.
  Cron script tests bind the canonical registered store and admitted execution instead of
  booting an external gateway client. Guards: session env, exec quick commands and cron
  script propagation (19 passed). The live-system subprocess guard is unchanged.
- Keep MCP serve in the agent-command registry and restore inline discovery for commands
  without a dedicated startup owner, while preserving gateway-owned discovery for chat.
  Release-hold isolation explicitly permits dropping the synthetic main branch so the
  independent runtime-format downgrade refusal is the path under test. Guards: bounded MCP
  startup and release-channel holds (26 passed).
- Batched pruning retains upstream on-session-delete hooks after each committed batch,
  before filesystem cleanup. Rewind fixtures assert the structured API error field/detail
  separately from storage exceptions, and routing-diff fixtures use serialized JSON objects.
  Guards: session-delete hooks, runtime rewind and routing replace-diff (15 passed).
- Retry parking retains the atomic process-notification owner hold until routing or shutdown
  requeue, including duplicate replacement. Keep wait-policy observations isolated per synthetic
  registry and preserve the bounded wait requirement when automatic notification is unavailable.
  Cleanup tests no longer patch the upstream-removed scratch reaper. Guards: process wait,
  polling ergonomics, interrupt cleanup, detached keepalive and refusal backoff (51 passed,
  2 platform skips, including duplicate parked-hold regression).
- Preserve the reserved multiplex home set after parking profiles and route drain-time notices
  through an active session authority before falling back to the adapter pending queue.
  Contract fixtures use the real JSON batch result, positional runtime config, authored pinned
  overrides and depth-derived role budget. Retry clocks stay monotonic across watcher runs,
  worktree fixtures isolate host process discovery and drive the actual boot-delay gate.
  Guards: provider fork contracts, completion delivery, watch-reset pins, worktree pushed tier,
  parked-profile refusal and startup-recovery parking (120 passed across the focused runs).
- The high-fd PTY regression waits for the complete marker line before checking idle-read
  timing. A legitimate split between the marker and CRLF otherwise makes pending output look
  like a failed idle wait. Native and forced-split variants retain the fd, output, timeout
  and EMFILE fallback assertions. Guard: `test_drain_high_fd.py` passes 3 tests on Linux, and
  the forced split reproduces the original pending-CRLF failure with the old boundary.
- Real gateway test children retain `HERMES_DISABLE_LAZY_INSTALLS=1`. The environment allowlist
  dropped that parent policy, so `voice.record` could enter PM's `audio-io` dependency sync on a
  selected CI venv and exceed the RPC deadline. Foreign test interpreters refuse that sync,
  hiding the escape in earlier Linux reproductions. The session voice-mode assertions and
  20-second RPC deadline remain unchanged, and an explicit child-environment regression binds
  the hermetic policy independently of the caller's environment.
- Classic CLI fixtures explicitly set `gateway.completion_notification_batch_window_seconds=0`
  in their synthetic profile. The fork intentionally holds successful background results for
  300 seconds by default (`maintenance/goal-lifecycle.md`), while the upstream live-session E2E
  expects immediate completion cards and has a 300-second total file deadline. Linux tracing
  proved the child had completed and persisted its result before the test's 90-second card wait
  expired. Keep the real fan-in floor, delivery assertions and all timeouts unchanged.
- The merge resurrected upstream's `tests/ci/test_cache_report_collection.py` without its
  `scripts/ci/timings_report.py` dependency. Fork main had already retired both with its cache-report
  CI tooling (`d77a654d8605` and retirement), so the orphan test is removed consistently with main.
  No remaining successor source imports `timings_report`.
- Retire once the next release sync no longer carries these merge points.

## Native Admission Synthetic Metadata

- Fork patch identity: `native-admission-synthetic-metadata`.
- Upstream native admission (`78646003aeab0`) rejects ordinary inbound metadata. The
  v0.22.5 fork integration retained gateway-authored goal prompts with routing and
  continuation metadata, but did not project that producer state into the durable
  codec. Goals intentionally have `internal=False` so their turns still evaluate
  goals. Changing that flag would hide the defect by breaking goal lifecycle.
- Only gateway producer entry points mint private synthetic provenance. The native
  envelope carries its metadata and internal semantics separately, and restores
  them before execution. Source, authorization, content and route validation still
  run. Ordinary adapter metadata crosses a per-platform allowlist/projection
  registry. WhatsApp retains its existing validation and 16 KiB drop policy.
- Telegram's `telegram_rich_message` projection keeps exactly `block_count`
  (non-negative int, excluding bool), `block_types` (at most 256 non-empty
  strings, each at most 64 characters), `source` (None or a non-empty string
  at most 64 characters), and `truncated` (bool). The adapter's source labels
  are `attribute` and `api_kwargs`. Missing fields, unknown keys, malformed
  values and unregistered platforms' non-empty metadata refuse admission.
  Native and synthetic replay preserve their separate metadata contracts.
- Guards: `tests/gateway/test_telegram_rich_message_admission.py` drives the
  real Rich Message handler through durable native admission, JSON restore,
  route revalidation and synthetic replay. It covers projection bounds,
  independent nested lists, invalid shapes and foreign-platform refusals.
  `tests/gateway/test_native_admission_metadata.py` retains the unchanged
  WhatsApp bridge and codec regressions.
- Admission refusals for producer/internal inputs are silent in chat. Internal
  refusals share one operator warning per 600-second window, with clause counts
  logged at debug level and summarized in the next window's warning. Human refusal
  notices are limited per authority/platform/chat/reason to one every 600 seconds,
  including all threads of that chat. Suppressed attempts log their count.
- Delegation retry claims and boot-recovery notices have distinct stable producer
  identities, separate from final delegation results. Retry notices persist only
  the validated `delegation_retry_notice` cancellation stamp through native and
  local snapshots and replay. Unknown metadata remains closed. Refusals identify
  producer-identity, event-shape and unknown-metadata clauses.
- Guards: `tests/gateway/test_watch_automation_admission.py` drives real watch,
  process, heartbeat and delegation producers through automation admission,
  including retry settlement, persisted cancellation fencing, boot-notice dedupe,
  native/local retry round trips, malformed stamps and unknown-key refusal.
- Guards: `tests/gateway/test_synthetic_native_admission.py`, including the real
  lifted-barrier and watch-pattern producer paths, durable restore, source/route
  revalidation, foreign human metadata, internal silence, and per-chat notice windows.
  Refusal and callback-rewrite cases use a stored active goal instance so the
  goal-currency fence admits them to the boundary under test. Unbacked or stale
  continuations must still be dropped before admission. The user-refusal case also
  drives a 47-message transport burst and verifies the 599/600-second boundary.
- Pre-ledger native routes may authorize their first automation against the
  current registered transport, runtime home and direct allowlist. They do not
  require a fresh human turn merely to receive a queued process result. Persisted
  origins are candidates, never connector grants. Relay, role and upstream-only
  delegation still require private native provenance. No human admission is synthesized.
- Guards: `tests/gateway/test_automation_upgrade_admission.py` reloads a persisted
  pre-ledger route, initializes the real authority and injects its queued watch
  on Telegram topic and Discord routes, including lost transport, changed home,
  direct-user revocation and unproven relay/role refusals.
- Roll back as one source unit. No config, schema, state migration or new alert
  system is involved. Retire when a released upstream provides equivalent producer
  projection and refusal presentation guards.

## v0.21.6 merge integration

- Fork patch identity: `v0216-merge-integration`.
- Merging upstream `v0.21.6` into fork main lost three fork-side bindings that
  Git merged cleanly or resolved mechanically. `recover_pending_to_db` dropped the
  fork's `deferred_followup` passthrough while `gateway/run_pending_recovery.py`
  still passes it, so every startup pending-spool replay raised `TypeError`.
  `[tool.hermes.extras-platforms]` kept a `mem0` gate after upstream removed the
  `mem0` extra (moved to the plugin catalog). The merged `uv.lock` carried two
  `mem0ai` entries and did not parse. The lock is regenerated with
  `hermes pm lock` from fork main's lock, so it differs only by the catalog-moved
  providers' packages.
- Guard: `scripts/run_tests.sh tests/gateway/test_multiplex_pending_recovery.py tests/pm/test_extras.py tests/gateway/test_cron_active_work_drain.py` and `uv lock --check`.
- Retire once the next release sync no longer carries these merge points.
- The same merge left `cron/scheduler.py`'s `__main__` external-worker entry calling
  `finish_worker_boot()` without importing it, so every restart-safe cron worker died
  with `NameError` before its acknowledgement. Guard:
  `scripts/run_tests.sh tests/cron/test_restart_safe_worker.py`.
- Review of the merged candidate found three more `v0.21.6` integration defects.
  `MemoryStore.compare_and_restore` (fork `memory-transaction-observers`) still called
  `_error` without upstream's new required `failure_class`, so invalid-target and stale
  restores raised `TypeError`. Batch memory operations persisted the `new_text` alias
  without scanning it. The restart-wait warning in `gateway/run_shutdown.py` passed
  upstream's wedged and restart-safe counts to the fork's message, which had no
  placeholders for them, so every no-budget restart raised inside logging. Guards:
  `scripts/run_tests.sh tests/tools/test_memory_transactions.py tests/tools/test_memory_tool.py tests/gateway/test_restart_drain.py`.
- A static undefined-name and import-resolution sweep of the merged tree, compared
  against both parents, found seven more clean-merge losses: the `tui_gateway.checkpoints`
  import in `tui_gateway/server.py`, `platform_ssl_context` in the Telegram adapter,
  `blocked_sessions` threading in `gateway/shutdown_flush.py`, the free-tier cooldown
  block (`attempts_made`) in `agent/turn_api_error.py`, the moved retry-after helper,
  `_hermes_user_agent` in `hermes_cli/models_pricing.py`, and `resumed` plus
  `_FLEET_PROBE_SETTLE_TIMEOUT_SECONDS` in `hermes_cli/update_cmd_fleet.py`. Skill Sync
  housekeeping rows were dropped because upstream removed Skill Sync. Guard: a
  `ruff check --select F821` and lazy-import resolution diff against both merge parents
  reports no new entries.
- Upstream-added `tests/ci` files that only check upstream's hosted workflows or
  `scripts/ci/classify_changes.py`, which the fork deleted in #62, stay deleted with them.
- Fork patch identity for the hosted-CI repair of the merged candidate (PR #396): `release-sync`.
- In that repair, each test that failed only on the candidate passed on the parent
  that owns it, so each fix restores that parent's lost lines and keeps the other's
  additions: memory prefetch fan-out redaction, the cron `progress_at` column
  (additive, column-guarded `ALTER TABLE`) and live-owner stale read, the primary's
  reasoning override through `reinstall_primary_runtime`, interrupted tool-tail
  closure, host-cancelled compression accounting, restart-wait budgets and logs,
  bounded shutdown-spool recovery, the session `/yolo` routing-index flag, launchd
  account-home resolution for an unknown uid, quick-snapshot digest checks before
  restore, the update body's start-of-run steps, `worktree_gc` git isolation, the
  desktop frozen-transport, slash-attachment and Quick Entry bindings, the
  rejected-thinking carry to a compression child, the finalized-row reopen before an
  isolated compute-host dispatch, and the fork's release-aware fleet verification
  (target release probe, post-verify release retention, no migration on rollback)
  ported into v0.21.6's `update_cmd_fleet_verify`. Fork-only cases that exercised only
  the `honcho` or `openviking` providers upstream removed are dropped; their retaindb,
  Hindsight and other consumer cases stay. Fork tests whose doubles or synchronization
  predated v0.21.6 (the Relay monitor-first race, now held before the first provider
  chunk, and the deferred-ack launchd probe) follow the new seams. Four new upstream
  tests assumed a host the fork's CI is not: the media-permission tests now pin the
  not-container branch, the launch-repair and zip-update probes avoid an interpreter
  with another checkout installed, and the incremental multi-pack-index case skips on
  a git too old to write one. Guard: `scripts/run_tests.sh` on the hosted-failing
  files, with `tests/hermes_cli/test_update_head_moved_gate.py` failing on both
  parents.
- The follow-on merge of fork main keeps the upstream pause-stop checkpoint and
  bounded pending-spool replay alongside fork planned-restart markers and full
  media/reply-context recovery. Both `start_chat` and `telegram_topic` remain in
  the shared-metrics toolset enum. Config keys are combined except the four
  intentionally retired `security.tirith_*` defaults: v0.21.6 config migration
  50 removes them and its regression requires they stay absent; no bundled
  scanner is resurrected. The auto-merged legacy handoff keeps upstream's root
  receipt directory while adding fork main's `handoff_path()` discovery helper.
- The merge verification caught schema drift outside the conflict hunk: the v3
  efficiency toolset enum omitted `setup`, and v4 omitted `telegram_topic` plus
  fork browser/setup tools from unavailable-tool counters. Both shipped schemas
  now agree with the current contract for efficiency and unavailable-tool
  dimensions; the regression checks both versions. The fork release-owner test
  also follows upstream's renamed `read_config_version_stamp()` export without
  weakening its migration and write-refusal assertions.
- The sync also dropped upstream's per-turn `_voice_turn_pending = ctx.voice_turn`
  assignment in `gateway/run_turn_runner.py`. Restore it unconditionally so voice
  turns use `auxiliary.voice_chat` and a reused agent resets the flag on typed
  turns. Guard: `scripts/run_tests.sh tests/gateway/test_display_null_turn_wiring.py tests/agent/test_voice_turn_route.py`.
- Restore upstream's `already_restarted()["pids"]` exclusion in the manual/stuck
  gateway sweep: POSIX gateways resumed by this update are healthy successors,
  not stale manual PIDs. Keep the fork's external-supervisor protection and
  cover both mapped and unmapped resumed gateways. Guard:
  `scripts/run_tests.sh tests/hermes_cli/test_update_external_supervisor_sweep.py tests/hermes_cli/test_update_outgoing_gateway_identity.py`.
  The outgoing-identity test's service-PID double returns a set, matching the
  production helper and upstream's set-union contract.
- The sync kept upstream's `enabled_toolsets` normalizer in the cron create map
  but dropped it from `_UPDATE_FIELD_NORMALIZERS`, so `update_job` stored a
  string, dict or int as a job's tool allowlist. Restore the update entry: a
  non-list raises `ValueError` before storing, a list is trimmed, and `[]` stays
  an explicit zero-tool allowlist. Guard:
  `scripts/run_tests.sh tests/cron/test_cron_explicit_empty_toolsets.py`.

- The sync retained upstream's `pm/install_states.py` orphan collector after the fork relocated
  install locks into `.locks/<name>.lock` plus `.recovery.lock`. Its `_held` checked only the
  deleted state's legacy `.install.lock`, so startup provisioning could delete an in-use state.
  The collector now fences both fork locks non-blocking through `install_state_lock` and
  `install_recovery_lock_path` while preserving the legacy lock check and holding that fence
  through deletion. Guard: `scripts/run_tests.sh tests/pm/test_install_states_gc.py`.

## Gateway Subcommand Exit Codes

- Fork patch identity: `gateway-cli-exit-code`.
- `gateway_command` returns the subcommand handler's result, and the fork's `gateway update` and `gateway guardian` handlers report failure as a non-zero int, but `cmd_gateway` discarded it. A rejected update reason or a failed guardian run therefore exited 0, so shell callers, cron and launchd saw success. `cmd_gateway` now returns the result to `main()`, which exits with a non-zero int and treats `None` or 0 as success.
- Upstream status: `upstream/main` drops the result the same way, but none of its gateway handlers return an int, so the defect is only observable through the fork's subcommands.
- Guard: `tests/hermes_cli/test_gateway_exit_code.py`.

## Agent update from a served profile dialed a nonexistent socket

- Fork patch identity: `update-lifecycle`.
- Terminal subprocesses inherit the routed profile's `HERMES_HOME`, but a multiplexer owns
  only the control socket at its launch home. `gateway update` used the routed home and
  returned "gateway update handoff unavailable" for a healthy served named profile.
- The CLI now reuses `host_multiplexer_serving()` (verified owner identity and same-tenant
  filtering), addresses that owner's home, and falls back to the caller's home without an
  owner. Cross-home handoffs identify the requesting profile; the owner validates it against
  its live served-home map, scopes the SQLite lineage lookup, and retains that profile in
  its own update marker. Root-home requests keep their original socket and payload.
- Guards: `tests/hermes_cli/test_gateway_agent_update_owner.py` uses a real isolated owner
  socket and rendezvous record; its named-profile case fails on the base while the root
  control passes. `tests/gateway/test_agent_update_profile_scope.py` restores
  `hermes_state.DEFAULT_DB_PATH` to its import-time sentinel after proving call-time
  resolution remains inside the sandbox, then distinguishes identical session IDs in two
  real profile databases and rejects unserved/traversal profile names.
- Retire with `update-lifecycle` only when a released upstream provides equivalent
  owner-routed, profile-scoped agent handoff and reason-bearing update notifications.

## Container-Key Active Process Reaping

- Fork patch identity: `active-processes-container-key`.
- Upstream's `has_active_processes` regression matched only the raw spawning
  owner, but the terminal idle-environment reaper queries by collapsed container
  key. A delegate-owned background process therefore failed the liveness check,
  and its still-needed environment could be torn down. The query now matches
  either the container key or raw owner, preserving both callers' semantics.
- Guard: `tests/tools/test_process_registry_owner_chain.py`
  (`test_has_active_processes_matches_container_key_and_raw_owner`) and
  `tests/tools/test_terminal_tool_lifecycle.py`
  (`test_cleanup_inactive_env_keeps_container_with_owner_spawned_process`).
- Retire when upstream's process liveness query matches both the container key
  and raw spawning owner, with equivalent idle-reaper coverage.

## Shared-metrics install receipt aging clock

- Fork patch identity: `install-run-clock`.
- `hermes_cli/observability/shared_metrics_install_run.py::_stale` aged receipts
  against wall time while consent-day selection and recording used
  `shared_metrics._utc_now`. The consent-day regression pinned the store to
  2026-10-06, so it began deleting a still-pending receipt after wall time crossed
  the seven-day cutoff on 2026-10-13. Use the store clock throughout; preserve
  the seven-day cap, consent gate, opt-out purge and unreadable-receipt deletion.
- Upstream already carries the same production fix in
  [`10a877bc9e5f6ecd1c32da0fa93af25b1dd1e60e`](https://github.com/NousResearch/hermes-agent/commit/10a877bc9e5f6ecd1c32da0fa93af25b1dd1e60e).
  Adapt only that fix to the accepted fork baseline, not moving upstream main.
  Its test-date-only predecessor PR #137741 does not protect clock consistency.
  Fork #549 pinned the module's `time` attribute in the consent-day test; that
  test now relies on the single store clock instead, since `time` is gone.
- Guard: `tests/hermes_cli/test_shared_metrics_install_run.py`, especially
  `test_receipt_age_uses_the_store_clock_when_wall_time_disagrees`: pin the store
  years before and after wall time, keep a six-day-old pending receipt, then
  delete it unreported at eight days. Keep
  `test_unreadable_and_week_old_receipts_are_deleted_unreported` unchanged.
- **Retirement:** Retire the production patch when an accepted upstream release contains the
  clock fix. Preserve equivalent past/future-clock coverage during that sync.
- **Rollback:** Revert the commits carrying `Fork-Patch: install-run-clock`; keep the
  wall-clock-disagreement regression with the active patch.

## Bot receipt read straddling its settlement commit

- Fork patch identity: `bot-receipt-settled-race`.
- `gateway/session_bot.py::_admission_outcome` read the admission row, then its
  result. When the settlement worker committed between those reads, the stale
  `started` row (no outcome) was projected through the terminal map as `failed`
  with the completed reply as its error, and `remember_receipt` then dropped the
  key from the wake index, so the delivery could never recover. Hosted run
  40424222149 failed `test_paused_receipt_rearms_on_resume_without_polling_rpc`
  twice on it. Re-read a nonterminal row whenever its result is present: the
  result and terminal outcome commit together.
- Upstream already carries the same fix in
  [NousResearch/hermes-agent#144881](https://github.com/NousResearch/hermes-agent/pull/144881)
  (`f62121e133baa525555db08abf23e785316f2efc`). Adapt only that fix; the next
  upstream sync absorbs it as already present.
- Guard: `tests/gateway/test_bot_mailbox_async.py::test_receipt_read_straddling_the_settlement_commit_projects_the_reply`
  forces the interleave through real SQLite settlement and `refresh_receipt` and
  expects `settled` with reply `pong`. The paused-resume contract and its timeout
  are unchanged.

## API admission drain check off the owner loop

- Fork patch identity: `api-drain-owner-no-import`.
- `gateway/platforms/api_server.py` imported `gateway.run` synchronously on the owner loop
  to read the global runner's drain flags, stalling the loop (~0.85 s cold) ahead of the
  off-loop SQLite admission. Read the adapter-attached runner first; keep the standalone
  global fallback.
- Guards: `tests/gateway/test_api_drain_owner.py` (both drain flags, no-import boundary) and
  `tests/gateway/test_api_admission_off_loop.py`.

## Delegation model-rejected banner on finished work

- Fork patch identity: `delegation-model-rejected-banner`.
- `tools/process_registry_notifications.py::_delegation_model_not_found` matched the
  configured model name plus any model-not-found phrase (`does not exist`, `unknown model`,
  ...) anywhere in a result's summary, including a task that finished normally. A completed
  worker whose answer named its own model and said a file "does not exist" therefore raised
  "SUBAGENT MODEL REJECTED … Every task in this batch failed … before doing any work".
  Only results that did not finish normally (`exit_reason != "completed"`, set by
  `tools/delegate_tool_child_run.py`) count, and the banner states the real share
  (`N of M tasks`) unless every task was rejected.
- Guards: `tests/tools/test_process_registry_model_not_found.py`, including
  `test_model_not_found_notice_absent_for_a_normally_finished_task` and
  `test_model_not_found_notice_counts_only_rejected_tasks`; `tests/tools/test_async_delegation.py`
  model-rejection notices (a real rejection that exhausts iterations still raises it).
