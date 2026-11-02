# Slack Native Stop

Load when changing Slack session lifecycle events, native task status, or manifest
subscriptions.

## Required Behavior

Slack's `agent_session_stopped` event must dispatch a normal `/stop` with the
clicking user's identity, workspace, channel, and thread preserved. The gateway
owns authorization, cancellation, session release, pending-input cleanup, standing
goal pause, and the stop latch. The adapter must not create a second cancellation
path. Reject malformed events, ignored channels, and unauthorized users before
changing session status. Move the Slack session out of `processing` after stopping.
Assistant and Agent manifests must subscribe to the event using the existing
`chat:write` scope. Continuing requires a new user message, not an invented native
resume event.

## Provenance

Fork patch identity: `slack-native-stop`.

Carries upstream [PR #143850](https://github.com/NousResearch/hermes-agent/pull/143850)
in its own shape, head `46e9b2aac7bc34a69a72d499f2c4a8da37973cb7` (open, checked
2026-10-29). `plugins/platforms/slack/adapter_native_stop.py` and
`tests/gateway/test_slack_native_stop.py` are byte-identical to that head, and the
adapter import, listener registration and Agent manifest lines match its hunks, so a
sync that contains the merged PR resolves them without conflicts. If upstream lands a
different shape, compare it against this unit instead of keeping both.

Fork-only remainder:

- Assistant manifests also subscribe to `agent_session_stopped`. Upstream subscribes
  Agent manifests only.
- `tests/gateway/test_slack_native_stop_gateway.py` covers Assistant and `none`
  manifests, malformed and gated events, and a real gateway turn interrupted through
  the adapter. `tests/gateway/test_slack.py` asserts the listener is registered.
- The Assistant note in the Slack messaging guide.

The [Slack event contract](https://docs.slack.dev/reference/events/agent_session_stopped/)
specifies that clicking Stop does not stop work automatically. Without a listener
the gateway remains busy. A missing subscription prevents the event from arriving.

## Verification

`scripts/run_tests.sh tests/gateway/test_slack.py tests/gateway/test_slack_native_stop.py tests/gateway/test_slack_native_stop_gateway.py tests/gateway/test_stop_chat_scope.py tests/gateway/test_stop_pauses_goal_and_holds_wakes.py tests/hermes_cli/test_slack_cli.py`

Verify the live app subscribes to `agent_session_stopped`, start a disposable Slack
turn, click the native Stop button, and confirm the running agent is interrupted
and the native status clears. Send another message to verify the session remains
usable. Unit tests alone do not prove delivery from Slack.

## Retirement And Rollback

When a sync brings in #143850 or an equivalent listener and Agent subscription, drop
the identity from the shared files and keep only the fork-only remainder above, until
upstream also subscribes Assistant manifests. Retire the remainder then, once the
regressions above pass without it. Revert the patch to roll back code. The event
subscription may remain enabled, but cancellation will no longer work until a listener
is restored. No data migration is required.
