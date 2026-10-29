# Slack API Human Users on Bot-Stamped Posts

Load this unit when changing `_event_declares_bot_sender`, `_slack_api_human_users`, or Slack bot-sender admission.

## Required behavior

- A Slack message whose `user` is in `platforms.slack.extra.api_human_users` is human-authored even when Slack stamps it with the posting app's `bot_id`, `bot_profile` or `app_id` and no `client_msg_id`, as user-token (`xoxp-`) `chat.postMessage` deliveries are. Its `source.is_bot` is False and it takes the normal human path.
- `subtype: bot_message`, a bot `user_profile`, and user-less posts stay bots. Hermes's own bot user (`_bot_user_id` or any `_team_bot_user_ids` value) stays a bot even if listed, so its posts keep the echo-loop guard.
- Other bots' posts keep their existing classification.

## Provenance

- Patch identity: `slack-api-human-users-stamped`.
- Upstream: [NousResearch/hermes-agent#117922](https://github.com/NousResearch/hermes-agent/pull/117922), open and unreviewed, backported by cherry-pick (`7718a366aac2d`). The fork adds the own-bot exclusion and the inbound-handler regression.
- Incident: 2026-10-29 on Personal (`d32adcc276c4`). Brian's user-token posts (`user=U07EUUXTP28`, `bot_id=B07K85WLG9K`) in a private channel ran no turn while hand-typed messages from the same user in the same channel did.
- Regression: `scripts/run_tests.sh tests/gateway/test_slack_api_human_senders.py`.

## Retirement

Retire the backported part when released upstream consults `api_human_users` before the `bot_id`/`bot_profile` short-circuit. Keep the own-bot exclusion until upstream has an equivalent guard, or retire it with the rest if upstream rejects it and Brian accepts relying on operators never listing the bot user.

## Rollback

Revert this PR's fork commit, then the `fix(slack): consult api_human_users before the bot_id/bot_profile short-circuit` cherry-pick.
