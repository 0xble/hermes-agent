# Session controls

## Identity

- **Fork patch identity:** `session-controls`

Session controls let one Hermes session pause, resume, clear, or replace another session's goal, and pause, resume, or stop another session's loop. A fresh verbatim quote from the requester's latest typed message authorizes the action; otherwise the durable request waits for an allowlisted Telegram Approve/Deny press.

## Required behavior

- Store one audit/outbox record per control under `state_meta` with a 24-hour pending expiry.
- Resolve only bare session IDs or `hermes:<current-profile>/<session-id>` targets; reject unknown and cross-profile targets.
- Treat relay and gateway-authored rows as non-user text. Quotes must be whitespace-normalized, at least 12 characters, present in the latest typed user message, and drawn from a source message no longer than 4,000 characters.
- Use the existing GoalManager and LoopManager methods for mutations. Goal replacement preserves revision history and refuses without explicit authority.
- Telegram callbacks must pass the existing callback allowlist and resolve through a durable compare-and-set; a second, expired, or unauthorized press must not mutate state.
- Gateway delivery is profile-scoped and drains durable request/outcome rows after restart. The watcher
  runs SessionDB and manager work in the gateway executor, expires requests with an in-transaction CAS,
  retries interrupted approvals only within their bounded recovery window, and marks undeliverable
  CLI/TUI rows skipped/done rather than growing the outbox forever. The watcher drains only the launch
  profile's SessionDB and `session_store`; it does not discover or drain another profile's store.

## Proof surface

Core regression tests cover quote freshness, relay rejection, target resolution, immediate controls, pending approval, atomic resolution/expiry, goal replacement, revision isolation, and loop actions. Gateway tests cover the non-blocking watcher/outbox behavior, Telegram-topic metadata, route skipping, continuation cleanup, and goal-resume admission. Telegram tests cover callback authorization, callback rendering that preserves the request text, and no-op second presses.

## Upstream disposition

The `hermes_cli/session_controls.py` core primitive, `GoalManager.replace`, gateway outbox watcher, base adapter fallback, and Telegram `ctl:` buttons are upstream candidates. The fork patch remains local until the S1 review and landing path is authorized.
