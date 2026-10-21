# Turn-Origin Provenance

Load this unit when changing gateway turn provenance, the `pre_llm_call` plugin contract, or per-turn session environment bindings.

## Required behavior

Every gateway turn carries exactly one default-deny origin: `user`, `relay`, `process`, `goal_loop`, `cron`, or `unknown`. Positively identified human ingress wins when it is merged with an internal notification. All other events remain non-user unless their gateway metadata identifies the corresponding relay, process, goal-loop, or cron path.

The selected origin reaches `pre_llm_call` as `turn_origin` and is bound task-locally as `HERMES_TURN_ORIGIN`. `run_conversation` reads that binding when no explicit origin is passed, so gateway and cron callers never add `turn_origin` to the `run_conversation` call shape that test doubles and adapters implement. Invalid values normalize to `unknown`, and cron workers explicitly preserve `cron` across their worker boundary.

## Provenance and patches

Fork patch identity: `turn-origin-provenance`.

Source surfaces are `gateway/platforms/event.py`, `gateway/run_turn.py`, `gateway/session_context.py`, `gateway/run.py`, `gateway/run_turn_runner.py`, the agent turn plumbing, `cron/scheduler.py`, and the focused gateway and agent tests. Each drained queued follow-up rebinds the origin of its own event for its turn and restores the chain head's origin afterward.

API-server turns (`gateway/platforms/api_server.py`) bind through `set_session_vars` without an origin and stay `unknown`. That is deliberate default-deny: API callers are not positively identified human ingress.

## Verification

Run `HERMES_PYTHON=.venv/bin/python bash scripts/run_tests.sh tests/gateway/test_turn_origin.py tests/agent/test_turn_origin.py tests/gateway/test_session_context_inheritance.py -q` and `git diff --check`. The focused proof must cover all 6 origin values, default-deny unknown, merged human plus notification precedence, task-local environment binding, invalid-value normalization, and the `pre_llm_call` kwarg.

## Retirement and rollback

Retire when released upstream exposes equivalent default-deny turn provenance to plugins and session context with merged human-input precedence. Roll back by reverting the commits carrying `Fork-Patch: turn-origin-provenance`.
