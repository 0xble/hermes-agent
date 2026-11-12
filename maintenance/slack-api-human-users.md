# Slack API human users

## Required behavior

A Slack `chat.postMessage` made with an allowlisted person's user token may carry the posting app's `bot_id`, `bot_profile`, and `app_id` stamps. The Slack adapter must admit that message as human when its `user` is in `api_human_users`, while still dropping classic `bot_message` events, bot profiles, non-allowlisted bot-stamped users, and the app's own bot user in any connected workspace.

## Provenance

Fork patch identity: `slack-api-human-users`.

This Meridian runtime adaptation ports the reviewed Hermes fix `2ca4f6516510c1c31414746497f8927e2c033ca8` onto the pinned Meridian base `2f0a6c16228686c9995214ca98f7f4b5e366ae0a`. Do not repin Meridian to the divergent Everguide runtime `b849a00ca05c4aee94c6036480fed6f05d972003`.

## Verification

Run:

```bash
scripts/run_tests.sh tests/gateway/test_slack_api_human_senders.py
```

The focused tests cover allowlisted user-token posts with bot stamps, real bot posts, the app's own bot user, and the `allow_bots: none` drop gate.

## Rollback

Revert the commit carrying `Fork-Patch: slack-api-human-users`.
