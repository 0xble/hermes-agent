# Cron one-shot grace

**Patch identity:** `cron-oneshot-grace`.

This unit owns the per-job catch-up allowance for `kind=once` cron jobs. Load it
when changing one-shot eligibility, retirement, recovery or provider misfire
backstops, or the `max_catch_up_lateness_seconds` interaction with those paths.

## Required behavior

- One-shot catch-up uses `max(120, max_catch_up_lateness_seconds)` when the
  field is set, and 120 seconds when it is absent. A field value of zero cannot
  tighten the one-shot safety floor. The same allowance must be used by due
  scan retirement, scheduler-stall continuity, outage coverage, missing-slot
  recovery and the provider misfire backstop.
- A one-shot beyond its allowance is held with its payload and disabled; it is
  never sent or routed through `missed_windows.claim_slot`. The missed-run
  diagnostic names the actual allowance used. Create/update/resume rejection
  of an already-past one-shot remains the fixed 120-second scheduling guard.
- Recurring `cron` and `interval` expiration continues to use
  `max_catch_up_lateness_seconds` as its own policy and is not widened by this
  unit.

## Provenance

Upstream commit `b81e71addd96` adds `grace_minutes` for recurring cron
schedules and explicitly refuses one-shot and interval schedules. It does not
cover this field or one-shot eligibility, so this is a fork-only adaptation.

## Verify

- The RED contracts in `tests/cron/test_oneshot_per_job_grace.py` cover the
  119/120/121-second baseline, 300-second inclusive bound, continuity/outage
  provenance, missing-slot recovery, provider catch-up, and recurring-policy
  separation. Scheduling an already-past job stays rejected at 120 seconds.

Run `tests/cron/test_oneshot_per_job_grace.py` and the existing one-shot grace,
retirement, missed-window, provider and scheduler tests. Run ruff on touched
Python files and the health check before delivery.

Retire when a selected upstream release honors the same per-job field at every
one-shot eligibility site with the same 120-second floor and held-payload
contract. Roll back by reverting commits carrying `Fork-Patch:
cron-oneshot-grace`.
