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

Adapted from upstream [PR #143850](https://github.com/NousResearch/hermes-agent/pull/143850),
commit `46e9b2aac7bc34a69a72d499f2c4a8da37973cb7`, open when inspected on
2026-10-24. The fork additionally preserves thread-aware authorization on its
older adapter boundary, subscribes Assistant manifests, and tests the running
turn through the real adapter and gateway. No moving upstream baseline is adopted.

The [Slack event contract](https://docs.slack.dev/reference/events/agent_session_stopped/)
specifies that clicking Stop does not stop work automatically. Without a listener
the gateway remains busy. A missing subscription prevents the event from arriving.

## Verification

`scripts/run_tests.sh tests/gateway/test_slack.py tests/gateway/test_slack_native_stop.py tests/gateway/test_stop_chat_scope.py tests/gateway/test_stop_pauses_goal_and_holds_wakes.py tests/hermes_cli/test_slack_cli.py`

Verify the live app subscribes to `agent_session_stopped`, start a disposable Slack
turn, click the native Stop button, and confirm the running agent is interrupted
and the native status clears. Send another message to verify the session remains
usable. Unit tests alone do not prove delivery from Slack.

## Retirement And Rollback

Retire when the accepted upstream release implements the listener and subscriptions
and these regressions pass without the fork adaptation. Revert the patch to roll
back code. The event subscription may remain enabled, but cancellation will no
longer work until a listener is restored. No data migration is required.
