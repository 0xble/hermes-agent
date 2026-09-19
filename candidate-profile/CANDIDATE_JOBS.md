# Candidate scheduled jobs

The scheduled work the replacement needs, expressed as `hermes cron create` invocations so they
can be applied to any profile and diffed against what is running. Nothing here is applied
automatically; each host's operator applies it at cutover with the right `--workdir`.

All four are `--no-agent` script jobs: they need no model, deliver their JSON result locally, and
mark failure with the `[CRON_FAILURE]` first line the candidate's cron records truthfully. The two
that may write externally (`--publish`) stay dry-run until the slice 14 trust gate is met.

```sh
# Slice 14, source-sync stage: nightly, dedicated worktree, dry-run until trusted.
hermes cron create --name sync-fork-candidate --no-agent \
  --script sync_fork_candidate.py --schedule "4 3 * * *" --deliver local \
  --workdir /Users/brianle/Repos/hermes-agent/.worktrees/sync

# Slice 14, post-promotion feature check: runs after every promotion (also fine hourly, it is read-only).
hermes cron create --name check-fork-patches --no-agent \
  --script check_fork_patches.py --schedule "17 * * * *" --deliver local \
  --workdir /Users/brianle/Repos/hermes-agent

# Slice 6, weekly skill curation from observations: dry-run stages and validates; --publish opens the PR.
hermes cron create --name curate-skill-observations --no-agent \
  --script curate_skill_observations.py --schedule "23 9 * * 1" --deliver local \
  --workdir /Users/brianle/dotfiles/.worktrees/curation

# Slice 12/13, weekly small-state snapshot with verification (state.db is covered by host backups).
hermes cron create --name snapshot-profile-state --no-agent \
  --script snapshot_profile_state.sh --schedule "41 4 * * 0" --deliver local
```

Scripts are installed into `$HERMES_HOME/scripts/` (the candidate's `--script` root) by the same
installer that places the extensions. Arguments such as `--dotfiles` and `--publish` are set in a
thin wrapper per host so the canonical scripts stay argument-free from cron's point of view.
