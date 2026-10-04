# Restart-parked goal wake

## Provenance

Fork patch identity: `goal-restart-parked-wake`. This is a fork-only adaptation of the existing parked-goal idle wake path; no upstream issue, pull request, or released equivalent was found for stale restart markers blocking the ticker as of 2026-10-04.

## Required behavior

A parked gateway `/goal` whose wait barrier has lifted must resume from the idle ticker even when its session still carries `resume_pending` from an interrupted gateway turn. Fresh restart markers remain owned by startup auto-resume; once the marker is outside the auto-resume freshness window, the stale flag must no longer block the goal wake. Legacy entries without `last_resume_marked_at` use `updated_at` for the same freshness decision. A non-positive `HERMES_AUTO_CONTINUE_FRESHNESS` disables stale takeover, consistently leaving ownership with startup auto-resume. The wake must preserve existing busy-session and admission fences so startup auto-resume and the idle ticker cannot both run the session. The stale marker is cleared through the async SessionStore facade only after the barrier prompt is confirmed and immediately before admission; a failed CAS aborts admission and leaves the marker for retry.

The continuation prompt must retain the shared lifted-barrier note, including that the awaited process exited with code 1 when the durable process receipt provides that fact.

## Independent proposal before upstream search

The defect is the unconditional `resume_pending` return in `GatewayRunner._goal_wakeup_fire_one`. Startup auto-resume only admits entries whose interruption marker is fresh, but the idle ticker never takes ownership after that freshness window, leaving the parked goal permanently blocked. The smallest complete repair is to keep the current early return while the marker is fresh, then atomically clear the stale marker through `SessionStore.clear_resume_pending(expected_marker=...)`, log the takeover, and continue through the existing idle-wake admission path. Existing running-agent, adapter active-session, queued-message, and `admit_internal_event` fences remain the single-run protection. The regression should cover a restart-killed process with a stale `resume_pending` marker: one continuation is admitted, the process-exit note is present, the stale marker is cleared, and a fresh marker still defers to startup auto-resume.

Important alternatives: unconditionally ignoring `resume_pending` would race startup auto-resume; clearing every marker at startup would discard fresh recovery; adding a second scheduler or changing goal persistence would duplicate existing owners and broaden the patch. No schema or runtime-state migration is needed.

## Upstream and fork disposition

Independent prior-art search on 2026-10-04 found no matching upstream issue or pull request for stale `resume_pending` markers blocking parked-goal wake. This is native gateway lifecycle behavior and therefore belongs in the maintained fork core, not a skill, plugin, or configuration.

## Verification and retirement

Focused gateway regression, affected goal/loop/restart suites, repository canonical gate, exact-head review, and remote readback. Retire when upstream provides equivalent stale-resume takeover behavior and the regression passes without this fork-only implementation. Roll back by reverting the focused patch; parked goal rows and session transcripts remain intact.
