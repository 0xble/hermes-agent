# Personal cutover runbook

Slice 15. Three separately authorized moves, each with a readback, then sustained acceptance.
Nothing in this file executes anything; every move needs Brian's explicit go for that move.

## Facts the runbook rests on (read 2026-09-19)

- The production gateway is launchd job `ai.hermes.gateway`, running the installed checkout's
  venv at `~/.hermes/hermes-agent/venv` with `--external-supervisor` and `KeepAlive` on
  unsuccessful exit. So in-chat `/restart` and `/update` exit 75 for launchd to relaunch; the
  candidate must be installed the same way or that contract changes.
- `HERMES_HOME` is `~/.hermes` (160 GiB; `state.db` 33.8 GB, `schema_version` 31). The
  candidate opened a schema-31 fixture without dropping anything; the real copy has not been
  rehearsed yet.
- Legacy writers that can touch the profile: the gateway itself, the LaunchAgents
  `com.brianle.promote-hermes-fork` and `com.brianle.hindsight-hermes`, and the Hermes crons
  `sync-hermes-fork`, `watch-hermes-updates`, `maintain-hermes-crons`, `maintain-hermes-home`,
  `curate-skills`, plus the Hindsight maintenance jobs.
- The migration diff of the live job file reports 94 jobs, 19 needing the per-job timezone
  patch (present on the candidate), one relying on a `model_preset` (`maintain-targets`), one
  duplicate enabled schedule pair, and no overdue rows.
- Production model routes go through CLIProxyAPI on `127.0.0.1:8317`; the candidate expresses
  them as two named `providers` entries (see the live isolated profile's `config.yaml`).

## Preconditions (all must hold before move 1)

1. I6 integration review receipt exists for the frozen candidate and its verdict has been
   dispositioned (findings fixed or explicitly accepted).
2. Slice 13 rehearsal passed on a consistent COPY of the real `state.db`:
   `scripts/schema_rehearsal.py <copy> --report ...` says `compatible`.
3. `scripts/cron_migration_diff.py` on a fresh copy of `cron/jobs.json` reports zero
   `decisions_needed`, which means `maintain-targets` has been pinned to a concrete model.
4. The candidate config for the real profile is written and checked (`hermes config check`),
   translating `model_presets` and `providers[].transport` into upstream vocabulary, with
   `browser.camofox.accounts`, `context_file_max_chars`, and the review/delegation routes set.
5. Rollback rehearsed once on the isolated install: `scripts/rollback_fork_runtime.sh --dry-run`
   then a real run against a fixture.

## Move 1: freeze and capture

Authorization: Brian's go for "freeze".

1. Stop new work: `launchctl bootout gui/$UID/com.brianle.promote-hermes-fork` and the
   `hindsight-hermes` job; disable the legacy maintenance crons listed above with
   `hermes cron pause <id>` (recorded, reversible).
2. Drain: wait until `hermes gateway status` shows no active turn, goal, delegation, or cron run.
   If quiescence is not reached, stop here and leave the legacy runtime primary.
3. Stop the gateway: `launchctl bootout gui/$UID/ai.hermes.gateway`. Verify no process holds
   `~/.hermes/state.db` (`lsof`), and the file's mtime stays unchanged for five minutes.
4. Capture: `sqlite3 ~/.hermes/state.db ".backup /Volumes/<external>/hermes-cutover-<stamp>/state.db"`,
   then rsync the durable directories (`cron/`, `memories/`, `skills/`, `pending/`, `hindsight/`,
   `auth.json`, `.env`, `config.yaml`, `pairing/`, `browser_auth/`, `vault/`), write SHA-256s.
5. Readback: the capture's `schema_version`, table count, and `sessions`/`messages` counts
   match the live file's from step 3.

## Move 2: activate one owner

Authorization: Brian's go for "activate", after move 1's readback.

1. Install the candidate as the managed checkout: the replacement source at
   `~/Repos/hermes-agent-next` (branch `candidate/release-v2026.9.14` plus the follow-ups),
   editable-installed into a fresh venv. Profile plugins are sourced separately
   from the external `agents` repository; this Hermes checkout no longer installs
   a second copy of their source.
2. Write the translated `config.yaml` (precondition 4) over the profile, keeping a copy of the
   legacy file beside it.
3. `hermes gateway install` for the profile, preserving `--external-supervisor` semantics, then
   `launchctl bootstrap`. Exactly one `ai.hermes.gateway*` job loaded.
4. Readback: `hermes gateway list` shows one running gateway for the profile; the loaded
   executable path is the candidate venv; `hermes --version` reports the candidate;
   `scripts/check_fork_patches.py --home ~/.hermes` says OK.

## Move 3: accept

Authorization: Brian's go for "accept" is implicit in move 2; the checklist is the gate.

- Telegram round trip on the production bot (one message, one reply).
- Memory recall of a known fact, one disposable write, `memory_undo` reverts it, journal intact.
- `skills_list` resolves a canonical skill from the external directory.
- `browser_navigate` with `account=brianle` on a known logged-in site; a vault fill on a
  disposable item.
- `hermes cron list` shows the same job identities and enabled flags as the capture; no
  duplicates; `next_run_at` values did not trigger a catch-up storm on the first tick.
- `hermes update --plan` reports the candidate install and no pending update.
- Sustained: one full daily cycle of the normal schedule with `hermes cron runs` and
  `hermes cron incidents` inspected and every `delivery_failed` or `unknown` explained.

## Rollback triggers and procedure

Trigger on any of: Telegram round trip fails twice; a memory or skill write lands in the wrong
place; a cron job runs twice or under the wrong home; `state.db` integrity check fails; the
gateway restarts more than twice in an hour without an operator cause; a Camofox account
resolves to the wrong identity.

Procedure: bootout the candidate job; take a second capture of the profile as it now is (post-
cutover data is preserved, never discarded); restore the move 1 capture over the profile;
restore the legacy `config.yaml`; bootstrap the legacy `ai.hermes.gateway` plist; verify the
legacy `hermes --version` and one Telegram round trip. Code rollback, if the managed checkout
was replaced in place, is `scripts/rollback_fork_runtime.sh --sha <legacy sha>`.

## What this runbook leaves to slice 18

Retargeting or retiring the legacy LaunchAgents and crons permanently, the archive bundle of
the old repository, the two GitHub renames, and removal of the old local checkout. None of it
happens during cutover.
