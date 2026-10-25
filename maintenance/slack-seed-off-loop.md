# Slack DM Session Seed Off the Event Loop

Load this unit when changing `_seed_dm_session` or the Slack `app_home_opened` and assistant thread lifecycle handlers.

## Required behavior

- `plugins/platforms/slack/adapter.py` `_seed_dm_session` primes the session store in a worker thread (`asyncio.to_thread`) as a tracked background task (`spawn_ingress_reply`). The Socket Mode ack never waits on the session store.
- Seed failures are logged at debug level, as before. The seed is lifecycle priming only, with no agent loop.

## Provenance

- Patch identity: `slack-seed-off-loop`.
- Incident: 2026-10-25 10:06 and 10:44 PDT on Personal. The gateway event loop missed 10 liveness probes and exited 75. The main-thread stack both times: slack_bolt ack → `_handle_app_home_opened` → `_seed_dm_session` → `gateway/session.py get_or_create_session` → `_is_session_ended_in_db` → `state_lock`, behind state.db write transactions held for 3-13s.
- Regression: `scripts/run_tests.sh tests/gateway/test_slack.py -k app_home_opened_does_not_block`.

## Retirement

Retire when released upstream seeds Slack DM sessions off the event loop or makes the session store non-blocking on the loop.

## Rollback

Revert the `fix(slack): seed the DM session off the event loop` commit.
