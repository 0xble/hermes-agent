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
- Explicit pending peer work registered on its active goal, including an idle peer whose result has not been verified by the parent.

A timer-only goal wait, a wait for your answer, an unnotified daemon, and a standing loop do not arm a check-in on their own. Arbitrary relay traffic and external task assignments are not discovered or imported.

## Native Peer Waits

After handing work to a peer, the originating parent registers the **exact peer address**, the **existing work reference**, and the **requested result and verification** on its current goal. Registration arms the existing supervisor automatically, including when the peer is the only outstanding work. It does not start another poller or consume a loop slot.

The shared goal commands are:

```text
/goal peer list
/goal peer wait <goal-created-at> <hermes:profile/session> <work-ref> "<requested result and verification>"
/goal peer resolve <goal-created-at> <hermes:profile/session> <work-ref> "<concrete recorded tool proof>"
```

`list` returns the goal's existing `created_at` instance fence. Use that value rather than minting another identity. A stale instance, a different parent context, a foreign profile, or an inexact peer/reference is refused without changing the goal. Repeating an identical registration is a no-op. A different requested result under an existing reference is refused.

A parent agent can use the same controls from its terminal through the owning gateway. These commands attach only, never start or update the gateway, never select another parent, and never write a local state database:

```bash
python -m hermes_cli.goal_peer_command list
python -m hermes_cli.goal_peer_command wait "$GOAL_INSTANCE" "$PEER" "$WORK_REF" \
  'Return the artifact and verify its persisted contents'
python -m hermes_cli.goal_peer_command resolve "$GOAL_INSTANCE" "$PEER" "$WORK_REF" \
  "$RECORDED_TOOL_PROOF"
```

Set `GOAL_INSTANCE` from `list`, `PEER` to the handoff's literal `hermes:<profile>/<session>` address, and `WORK_REF` to its existing work reference. The originating session is taken from the terminal's current session context, not a caller-supplied target argument. The corresponding existing `session.automation` goal RPC methods are `wait_on_peer(peer, ref, requested_result, expected_instance=...)` and `resolve_peer(peer, ref, evidence, expected_instance=...)`.

Resolution requires concrete tool output already recorded in the parent session during this goal. First inspect the returned artifact with an appropriate tool, then cite that result. The existing goal evidence validator rejects missing, stale, foreign-session, bookkeeping-only, or negated evidence. Generic success claims do not qualify. An idle peer, a reply, a peer's reported completion, or a reassignment alone is not success. The exact resolved reference and its proof remain in goal history.

Multiple pending references coexist with process and time waits. Ordinary barrier resets, re-parking, resume, gateway restart, and conversation compression preserve them. Ordinary goal revisions and route changes cannot silently retire them. Both the judge's done verdict and autonomous completion are blocked until pending results have been verified. Explicit user pause, clear, and replacement retain their existing goal semantics. Pausing suspends supervision, resuming restores it, and clearing explicitly retires the goal.

The scheduler reads only the local goal inventory. It never sends remote heartbeat requests to peers. A check-in may inspect or contact a peer within the parent's existing authority, but that activity does not itself resolve the dependency.

## What Happens At A Check-in

Hermes re-enters the original conversation and topic only when it is idle and its input queue is empty. Your messages, completion reports, and already-queued automation take priority. Overdue checks coalesce into a single turn rather than accumulating.

The generated turn lists the outstanding work, its state, age, and last observed activity when available. Hermes inspects logs and artifacts, distinguishes liveness from progress, and steers or recovers stalled work within its existing authority. A check-in never grants permission to restart work with unreconciled external effects.

While work remains, Hermes must write a **1–2 line status** explaining what finished, what is still running, and when it will check again, unless it delivers the final result. Ordinary loop and goal silence rules do not suppress this bounded status. If the model returns a silence marker, Hermes retries once. If it still produces no written status, the gateway delivers a factual fallback through the normal reply path.

If the local roster cannot be read, an already-armed supervisor retains its persisted due clock, including after restart. It does not reset or disarm merely because observation failed. At the due time it enters the normal admission and written-status path with an honest **unknown roster** item. An unarmed conversation with an unavailable roster stays silent. Only confirmed visible output resets the normal cadence.

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
