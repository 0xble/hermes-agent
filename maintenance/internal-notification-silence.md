# Internal notification silence and parked-goal notices

Fork patch identity: `internal-notification-silence`.

## Required behavior

Internal background-process and async-delegation notification turns must tell the model the exact silent response token when no user-facing response is needed. A parked-goal status notice is emitted only when its waiting target/reason changes and is not repeated after unchanged turns. Silent internal turns skip post-turn goal evaluation entirely, so they cannot produce parked-goal notice spam.

## Independent hypothesis (frozen before upstream prior-art search)

- **Observed failure:** internal notification prompts describe silence only as prose, so the model sometimes emits prose such as `No user-facing reply for internal process notification`; exact-match filtering correctly delivers that prose. Separately, `evaluate_after_turn` returns the same parked message on every wait, and the gateway defers every non-empty message after every turn without remembering the last notified wait or distinguishing intentional silent internal turns.
- **Causal chain:** notification builders and the shared internal footer do not expose the exact `NO_REPLY` contract → model prose is delivered; parked state is durable but notification history is not → each eligible turn re-runs the same waiting decision → the gateway sends the same status indefinitely.
- **Smallest complete correction:** extend the canonical shared internal-notification footer with an exact `NO_REPLY` instruction; ensure model-waking synthetic notification paths use the same marking seam, while persist-only API-server delegation delivery rows remain verbatim. Persist one normalized last-notified wait key with goal state, emit parked status only when that key changes, clear it when the wait ends or the goal resumes. Silent internal turns do not enter the post-turn goal hook, which already prevents parked spam.
- **Rejected alternatives:** loosening the global silence filter or suppressing prose heuristically would change human-turn behavior and could hide legitimate replies; adding duplicated instructions to each notification formatter would drift; an in-memory gateway dedupe would re-spam after restart and would not cover multiple workers; suppressing all goal notices after internal turns would break achieved/resumed/other actionable statuses.
- **Regression boundary:** model-waking gateway notification injection asserts the canonical footer for single, coalesced, and async-delegation batch wakes; API-server persistence asserts its verbatim content; a real `GatewayRunner._post_turn_goal_continuation` path proves unchanged parked waits send once and changed waits send again; `GoalState` round-trips the persisted key. Silent internal turns are covered at `_run_post_turn_hooks`, which skips the goal hook for an empty delivered response.
- **Compatibility/rollback:** old goal rows load with an empty optional field; footer consumers recognize the exact old and current footer forms and strip only those forms before retained human suffixes; rollback reverts only this patch's source/tests/maintenance record and leaves existing goal state readable.
- **Uncertainty:** whether an upstream change already defines a stronger internal silence contract or parked-notice dedupe; search upstream before implementation and reconcile this proposal explicitly.

## Upstream status

Related upstream work was checked after the independent hypothesis was frozen:

- [NousResearch/hermes-agent#66507](https://github.com/NousResearch/hermes-agent/pull/66507) is open and unreleased. It fixes the async-delegation shared-session sender prefix and adds an exact `NO_REPLY` no-news instruction to single and batch delegation formatters. This patch agrees on the model-visible contract but keeps the instruction in the canonical gateway footer on model-waking paths only, preserves API-server persist-only delivery rows verbatim, and addresses parked-goal notice dedupe.
- [NousResearch/hermes-agent#99941](https://github.com/NousResearch/hermes-agent/pull/99941) is open and unreleased. It adds the exact no-news contract to process notification formatter paths. This patch adopts the behavior at the shared injection seam so coalesced process batches and all synthetic notification routes receive the same footer without duplicating formatter prose.
- [NousResearch/hermes-agent#66480](https://github.com/NousResearch/hermes-agent/issues/66480) documents redundant replies from late async completions and links #66507. It supports the failure model but does not cover durable parked-goal status dedupe.
- [NousResearch/hermes-agent#113031](https://github.com/NousResearch/hermes-agent/issues/113031) and [#112327](https://github.com/NousResearch/hermes-agent/pull/112327) concern trusted scheduled-heartbeat silence and are distinct; they reinforce keeping the exact-match human-turn filter unchanged rather than broadening it.

No released upstream equivalent was found. The fork remains a necessary maintained implementation until released upstream behavior covers both contracts.

## Final design and rejected alternatives

The shared `INTERNAL_NOTIFICATION_FOOTER` now tells machinery turns to reply with exactly `NO_REPLY` and nothing else when no user-facing reply is needed. `_mark_internal_notification` remains idempotent and is used at model-waking synthetic injection seams; persist-only api_server async-delegation delivery rows stay verbatim because no model turn runs there. Silent internal turns with no delivered text skip the post-turn goal hook, so they cannot emit parked-goal notices; ordinary turns still evaluate goals and emit actionable statuses.

Goal state persists `last_wait_notice_key` plus the requested `waiting_seconds` for timed waits. `_park()` clears and replaces barrier fields without clearing the prior notice key, so a judge re-parking on the same target/reason—or the same duration/delegation count/reason—dedupes across new deadlines; a changed target/reason or a wait that actually lifts clears the key and re-announces. Judge-generated waits use the same dedupe path.

Rejected alternatives remain: loosening the global silence filter, suppressing prose heuristically, duplicating instructions in each formatter, in-memory-only dedupe, or suppressing every goal notice after an internal turn.

## Verification, budget, rollback, and retirement

Fail-before evidence: the new footer assertion failed against the base footer, the spoof cases failed against relaxed prefix matching, and the parked-goal regression observed two callback registrations for two unchanged waits. The focused post-fix suite passed:

- `scripts/run_tests.sh tests/gateway/test_goal_status_notice.py tests/gateway/test_goal_verdict_send.py tests/gateway/test_internal_notification_marker.py tests/gateway/test_background_process_notifications.py tests/gateway/test_completion_delivery.py -q` — 102 passed.
- `scripts/run_tests.sh tests/gateway/test_goal_*.py tests/hermes_cli/test_goal_*.py tests/hermes_cli/test_goals.py tests/agent/test_goal_set_receipt_notice.py -q` — 368 passed, 1 skipped on macOS.

The outbound rate-budget check enumerated the changed behavior: this patch adds no new outbound call type, widens no send/edit/typing path, and suppresses duplicate goal/status sends. Therefore its worst-case per-conversation call budget is reduced (not increased); no limiter sum or ceiling is worsened. Full CI preflight remains the release gate.

Rollback is a source revert of this patch's files; the added goal field is optional and old rows remain readable, so no destructive state migration is required. Retire when a released upstream implementation satisfies the exact internal-notification silence contract, API-server path coverage, and durable parked-goal state-change dedupe; remove the fork-only implementation and duplicate tests after equivalence is verified.

## Quiet heartbeat and /loop wakeups

Heartbeat and `/loop` wakeup prompts ask for exactly `[SILENT]` when nothing meaningful changed. Gateway `/loop` wakeups are internal events with `reply_expected=False`, so the bare marker is suppressed like other machinery turns; the CLI, TUI and Desktop hide a successful bare marker on wakeup turns (`hermes_cli.loops.is_quiet_wakeup_prompt`) using the canonical `gateway.response_filters` helpers. The streamed hold (`hold_silence_delta`) re-arms at each tool-round segment break, so commentary before a tool call cannot expose a trailing bare marker, and it also gates CLI streaming TTS; the TUI voice fallback speaks the delivered text, not the raw marker. `LoopManager.complete_tick` treats every canonical silence marker form as unchanged for self-paced backoff and skips the `--until` judge call. The pre-contract template literals stay registered in `agent/synthetic_prompt.py` so stored rows without `display_kind` still classify as generated for memory recall and retention. Focused regression: `scripts/run_tests.sh tests/gateway/test_loop_quiet_wakeup.py tests/tui_gateway/test_quiet_wakeup_silence.py tests/agent/test_synthetic_prompt.py tests/hermes_cli/test_loops.py`.
