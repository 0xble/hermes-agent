# Legacy reference inventory

Read-only inventory captured 2026-09-18 for the Hermes Agent Next migration.
This is a discovery record. It does not authorize cutover or deletion.

## Legacy repository

- Path: `/Users/brianle/Repos/hermes-agent`
- Branch: `main`
- HEAD: `c6475a78e92783d240ee9df2bc456a1d0076ad2b`
- Status: 7 commits ahead and 5,188 behind its `origin/main`; untracked `.tmp-hermes-bare/`
- Registered worktrees: 123, including the main checkout
- The old checkout and all its worktrees remain untouched.

## Known local consumers

- Dotfiles maintenance scripts hard-code `/Users/brianle/Repos/hermes-agent`: `dot_hermes/scripts/executable_hermes-maintenance-{preflight,source,verify,review}.py`.
- Dotfiles repository-routing guidance names `0xble/hermes-agent` as the runtime fork and sets it as the default GitHub repository.
- The macOS launch agent `ai.hermes.gateway.plist` runs the installed profile at `/Users/brianle/.hermes/hermes-agent`.
- `com.brianle.promote-hermes-fork.plist` loads the promotion helper and must be retired or retargeted before cleanup.
- `com.brianle.hindsight-hermes.plist` includes the installed Hermes environment in its PATH.

## Cleanup implications

These references are cutover blockers, not deletion targets. Slice 18 must first
retarget or retire each maintenance and launchd owner, prove one active gateway
per host, archive the old repository and unique state, and read back the final
GitHub names. Only then may the old checkout be moved to trash and the
replacement be renamed locally to `/Users/brianle/Repos/hermes-agent`.
