# Cron fire ownership

## Contract

A process that has started a cron side effect is the safer authority for that
side effect. Delayed or failed fire-claim heartbeats are advisory and must not
interrupt the live process. Owner compare-and-refresh remains the fence for
terminal state and delivery, so a replacement owner cannot be overwritten.

## Verification

Exercise repeated heartbeat misses and a claim rewritten by a competing
recovery tick. The active run must reach its normal completion path without its
cancel event being set; owner-fenced terminal writes and delivery may still be
rejected for the replacement owner.
