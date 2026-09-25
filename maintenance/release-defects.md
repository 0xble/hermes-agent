# Upstream release defects

Load this unit when changing gateway status for parked profiles, Bot Desktop
teardown during profile delete and rename, the workspace snapshot pin, or
memory-provider config cloning, or restarting a parked profile.

These are narrow fixes for defects that shipped in upstream `v2026.9.24` and
were still present on upstream `main` when the fork synced. Offer each upstream
and drop it once upstream carries an equivalent fix.

## Parked status hides a live gateway

- Fork patch identity: `parked-status-live-gateway`.
- `hermes gateway status` returned "parked" before inspecting processes. A
  gateway started with `--force` bypasses parking and leaves the marker, so it
  was hidden, including from `--deep` and `--full`. Status now reports the
  parked marker and still shows a running gateway.
- Guard: `tests/hermes_cli/test_gateway_multiplex_lifecycle.py`
  (`test_parked_status_still_reports_a_forced_gateway`).

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
- `hermes -p <name> gateway stop` parks the profile and the host stops serving
  it. A later `gateway restart` then found no host serving the profile and fell
  through to the standalone restart path, so the profile stayed parked and
  stopped. Restarting a parked profile now unparks it and asks the host to serve
  it, the same as `gateway start`. A separate `--force` gateway keeps its own
  restart path.
- Guard: `tests/hermes_cli/test_gateway_multiplex_lifecycle.py`
  (`test_restart_after_stop_unparks_and_serves_the_profile`).

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

## Alias-cache isolation guard scanned nothing from a worktree

- Fork patch identity: `alias-cache-guard-discovery`.
- The DIRECT_ALIASES in-place-write guard skipped any path whose absolute parts
  contained `.worktrees`, so from a linked worktree (the fork's standard review
  and sync checkout) it scanned zero production files and passed vacuously.
  Discovery now filters repository-relative parts, and a coverage assertion
  requires the scan to reach the alias-cache owner.
- Guard: `tests/hermes_cli/test_model_alias_credentials.py`
  (`test_the_scan_covers_the_alias_cache_owner`).
