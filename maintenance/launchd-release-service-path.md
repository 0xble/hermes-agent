# Launchd Release Plist Service PATH

Load this unit before changing `generate_launchd_plist`, `_installed_service_path_entries`,
or any caller that renders the gateway plist for a release target (`hermes update`, the
gateway guardian's rollback, service repair).

## Required Behavior

A release render's PATH is a property of the installed service, not of whoever renders it.
It leads with the installed service plist's user PATH, dropping dirs under `releases/` and
`current/`. Caller PATH entries the installed plist lacks are appended. Release-owned dirs
(`current/.venv/bin`, the target's `node_modules/.bin`) come from the target release. An
unreadable installed plist (missing, truncated or invalid XML) contributes nothing and is
rewritten. Non-release renders (`hermes gateway install`) keep the caller's PATH.

## Source

Fork patch identity: `launchd-release-service-path`. On 2026-10-29 the guardian rendered a
rollback plist from launchd's bare `/usr/bin:/bin:/usr/sbin:/sbin` using the code of the
release it was rolling back from. The plist lacked `~/.local/bin` (the 1Password `op` CLI)
and named that release's `node_modules`. The rolled-back gateway loaded no Slack or
Telegram tokens and exited, and the gateway was down from 11:07 PM to 11:13 PM PT. Fork-only:
upstream has no immutable-release guardian.

## Verification And Rollback

Guard: `scripts/run_tests.sh tests/hermes_cli/test_launchd_release_path.py`. Retire with the
guardian. Rollback reverts this identity's generator change and tests. The next release
render then takes PATH from the caller again, with no state migration.
