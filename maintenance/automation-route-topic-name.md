# Automation Route Topic Name

Load this unit before changing `_route_identity` or `_UNCOMPARED_FIELDS` in
`gateway/session_automation.py`.

## Required Behavior

Before `snapshot_automation` lends a route's last committed envelope to a producer wake
(async-delegation completion, process notice, heartbeat), it compares the wake's route
source with the envelope's source. It does not compare `chat_topic` or `message_id`.
Every other field still has to match, including `user_name`, which
`test_native_completion_wake_refuses_other_source_drift` pins. The admitted payload
carries the committed envelope's own source and provenance.

## Source

Fork patch identity: `automation-route-display-labels`.

Telegram omits a forum topic's name on ordinary messages inside the topic. A route origin
that recorded `chat_topic` therefore never matched its committed envelope. From about
9:00 AM PT on 2026-10-29, every async-delegation completion for topics `2027045491:195852`
and `:194029` was refused with `admission_conflict`. That reason is not permanent, so each
completion was retried forever. 14 results were undelivered, and the retries tripped
bff96cdf's admission canary.

## Verification And Rollback

Guard: `scripts/run_tests.sh tests/gateway/test_automation_route_display_labels.py
tests/gateway/test_automation_notification_category.py`. To roll back, revert this
identity's comparison change and tests. No state migration is needed.
