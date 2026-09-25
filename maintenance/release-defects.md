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
