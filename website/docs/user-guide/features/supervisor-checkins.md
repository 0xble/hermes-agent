---
title: Supervisor Check-ins
description: Automatic, bounded status updates while a gateway conversation owns background work.
---

# Supervisor Check-ins

When a gateway conversation finishes a turn but still owns background work, Hermes automatically schedules a supervisor check-in. You do not need to set a loop or heartbeat.

By default, the first check is due **20 minutes after the last visible assistant message**. Subsequent updates are due every **30 minutes**. A visible assistant message or streamed edit resets that timer. A failed or held delivery does not.

## What It Tracks

A conversation is supervised while it owns any of:

- An active asynchronous delegation, including a queued or finalizing child.
- A running background process registered for completion notification.
- An active goal waiting on a process session or PID.

A timer-only goal wait, a wait for your answer, an unnotified daemon, and a standing loop do not arm a check-in on their own. Cross-session dependencies not represented by a goal wait are not inferred.

## What Happens At A Check-in

Hermes re-enters the original conversation and topic only when it is idle and its input queue is empty. Your messages, completion reports, and already-queued automation take priority. Overdue checks coalesce into a single turn rather than accumulating.

The generated turn lists the outstanding work, its state, age, and last observed activity when available. Hermes inspects logs and artifacts, distinguishes liveness from progress, and steers or recovers stalled work within its existing authority. A check-in never grants permission to restart work with unreconciled external effects.

While work remains, Hermes must write a **1–2 line status** explaining what finished, what is still running, and when it will check again, unless it delivers the final result. Ordinary loop and goal silence rules do not suppress this bounded status. If the model returns a silence marker, Hermes retries once. If it still produces no written status, the gateway delivers a factual fallback through the normal reply path.

Completion notifications remain the fast path. The supervisor automatically disarms when no tracked work remains, and a stale queued check cannot run after the last completion. Check-in state survives a gateway restart. Normal conversation compression retains it, but a new or forked conversation does not inherit it.

## Configuration

```yaml
checkins:
  enabled: true
  first_after_seconds: 1200
  interval_seconds: 1800
```

Both durations have a **300-second minimum**. Invalid durations use the defaults. Set `enabled: false` to disable automatic supervision. Changes are read by the gateway scheduler, no separate service or cron job is required.

Check-ins do not consume the session's [`/loop`](./loops) or [`/heartbeat`](./heartbeat) slot. An active goal does not defer them, and existing quiet goal and heartbeat behavior remains unchanged when no background work is outstanding.
