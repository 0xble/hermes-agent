# Agent Update / Restart Window Card

Fork patch identity: `update-window-card`.

## Contract

Agent-facing `request_update` and `request_restart` dispatch requires one-shot
native consent outside **02:00 inclusive–06:00 exclusive America/Los_Angeles**.
Inside that Pacific civil-time window, including DST, no card is requested.
The wall clock is injectable; the machine's local timezone is not consulted.

The enforcement point is `ToolRegistry.dispatch`, after tool resolution and
before the plugin handler. The installed lifecycle tools live in external
plugins, so keeping the gate in the fork prevents a plugin replacement from
silently discarding the policy. No new tool, stored approval, allowlist, or
lifecycle subsystem is introduced.

The gate reuses `tools.approval_prompt.request_elicitation_consent`, the same
native gateway/CLI approval bridge used by the browser vault manager card.
Session and permanent approval are disabled. Approve calls the original handler
once with unchanged arguments; Deny, timeout/cancellation, and unavailable
approval surfaces return distinct failure statuses without invoking it.
Approvals-off and dangerous-command allowlists cannot bypass this consent.

The short-lived `agent-update` control-socket RPC (`hermes gateway update
--reason`) is also agent-facing. Outside the window it refuses before writing
markers or spawning anything and directs the caller to `request_update` for the
card. It does not wait for a human beyond its RPC deadline or increase timeouts.
Inside the window its original native handoff remains unchanged.

Human shell `hermes update`, human `/restart` (and `/update`), and the scheduled
nightly native updater bypass the agent dispatch/RPC admission paths and remain
unchanged. This is not a sandbox against arbitrary agent shell commands or code
execution; existing lifecycle plugin command guards still own those routes.

No drain, idle wait, restart delay, replay, or lifecycle timeout is added or
changed. Approval permits the existing immediate restart/update path. There is
no live-outage exception: no existing trusted outage marker was found for these
admission paths, and model-supplied reasons must not grant an exception.

## Verification

Contract tests: `tests/tools/test_lifecycle_approval.py`, `tests/gateway/test_lifecycle_approval.py`, `tests/gateway/test_agent_update_launcher.py`, `tests/tools/test_browser_vault_manager_card.py`.

The new tests cover both tools, Pacific window boundaries in PST/PDT, approve,
deny, timeout/cancellation, unavailable approval, a real native queue/callback
round trip, no persistent approval, unchanged arguments, unrelated tools,
agent socket bypass refusal, native updater spawning, human CLI update,
scheduled updater CLI mode, and immediate human slash restart. Existing socket
lineage tests explicitly inject an in-window clock without removing assertions.

Before deployment, run the exact-head local gate. Keep the PR draft until that
qualification and delivery-owner review pass; a partial or failed gate is not
an installation authorization.
