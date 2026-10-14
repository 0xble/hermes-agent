# Cron skip missed occurrences

**Patch identity:** `cron-skip-missed`.

Cron had no way to deliberately skip one occurrence of a recurring job. `pause` stores the
slot and stops scheduling, and `resume_job` deliberately keeps a stored `next_run_at` that is
already past as the due instant (#113603, `cb8d652b4997`): the first tick after resume then
applies the ordinary late-fire / catch-up / `cron.catch_up_missed` policy, so a slot that
elapsed while paused is never lost silently. That is the right default — a pause must not
swallow an occurrence with no run, no ledger row and no log line — but it also meant there was
no way to say "I meant to skip that one": resuming after a pause across a slot fired it
immediately, and `cron edit --schedule` on a paused job left the stale past instant untouched.

Preserve:

- Default resume is unchanged: a past stored instant survives resume and stays due.
- `resume_job(skip_missed=True)` / `hermes cron resume --skip-missed` / `cronjob_manage`
  `action=resume, skip_missed=true` drops the occurrences that elapsed while paused (the
  first 20 INFO-logged one by one, any rest as one count line) and resumes on the next future slot.
- `skip_job(job_id, count=N)` / `hermes cron skip --count N` / `cronjob_manage action=skip`
  drops the next N occurrences of a *scheduled* recurring job, counting the pending
  `next_run_at` as the first. Paused jobs are refused: resume re-derives a future instant, so
  the paused case goes through `skip_missed`.
- A schedule or timezone edit on a paused recurring job re-anchors `next_run_at` to the next
  future slot of the new schedule, so resume can never fire a slot from the old expression.

Source: `cron/jobs.py` (`_skip_slots`, `skip_job`, `resume_job`, `_rearm_paused_recurring`),
`tools/cronjob_tools.py`, `hermes_cli/cron.py`, `hermes_cli/subcommands/cron.py`,
`website/docs/user-guide/features/cron.md`.

Proof: `scripts/run_tests.sh tests/cron/test_jobs_skip_missed.py tests/hermes_cli/test_cron.py
tests/tools/test_cronjob_tools.py` (classes `TestSkipMissedOccurrences`, `TestCronSkipMissed`,
and `TestRegisteredHandlerForwardsAttachToSession.test_resume_skip_missed_and_skip_count_through_registry`).

Upstream: searched 2026-10-13. No PR implements an explicit skip. Two open upstream issues
describe the same gaps and neither has a fix: [#131592](https://github.com/NousResearch/hermes-agent/issues/131592)
(editing a paused job's schedule, then resuming, fires the old schedule's elapsed slot) and
[#104802](https://github.com/NousResearch/hermes-agent/issues/104802) (an operator opt-out from
missed-run catch-up; that one asks for a config flag, this patch exposes the opt-out as an
explicit action instead). Contribute upstream. Retire when upstream offers an explicit skip of
missed occurrences on both the CLI and the agent tool, keeps the #113603 default, re-anchors a
paused job's next run on schedule edit, and these regressions pass there.

Rollback: revert the commits carrying `Fork-Patch: cron-skip-missed`. Nothing is migrated.
