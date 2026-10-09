# Session controls

## Identity

Session controls let one Hermes session pause, resume, clear, or replace another session's goal, and pause, resume, or stop another session's loop. A fresh verbatim quote from the requester's latest typed message authorizes the action; otherwise the durable request waits for an allowlisted Telegram Approve/Deny press.

## Required behavior

- Store one audit/outbox record per control under `state_meta` with a 24-hour pending expiry.
- Resolve only bare session IDs or `hermes:<current-profile>/<session-id>` targets; reject unknown and cross-profile targets.
- Treat relay and gateway-authored rows as non-user text. Quotes must be whitespace-normalized, at least 12 characters, present in the latest typed user message, and drawn from a source message no longer than 4,000 characters.
- Use the existing GoalManager and LoopManager methods for mutations. Goal replacement preserves revision history and refuses without explicit authority.
- Telegram callbacks must pass the existing callback allowlist and resolve through a durable compare-and-set; a second, expired, or unauthorized press must not mutate state.
- Gateway delivery is profile-scoped and drains durable request/outcome rows after restart.

## Proof surface

Core regression tests cover quote freshness, relay rejection, target resolution, immediate controls, pending approval, atomic resolution, goal replacement, and loop actions. Gateway tests cover the watcher/outbox behavior and continuation cleanup. Telegram tests cover callback authorization, callback rendering, and no-op second presses.

## Upstream disposition

The `hermes_cli/session_controls.py` core primitive, `GoalManager.replace`, gateway outbox watcher, base adapter fallback, and Telegram `ctl:` buttons are upstream candidates. The fork patch remains local until the S1 review and landing path is authorized.
