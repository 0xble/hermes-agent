# Gateway human-turn silence re-prompt

This unit owns the core retry that prevents a user-typed gateway turn from ending with only a silence marker.

## Preserve

- A gateway turn with a human display kind (`None`) and unknown or true reply expectation must receive one model re-prompt when its final visible text is exactly `NO_REPLY` or `[SILENT]`.
- The check reads `agent._turn_reply_expected` at final-response time, so a typed message folded into a running relay or notification turn (`gateway/run_busy.py::_fold_into_running_turn`) makes that turn re-promptable.
- Internal notification turns, turns with `reply_expected=False`, non-gateway lanes, and a second silence marker remain bounded and retain their existing silence behavior.
- The re-prompt budget belongs to turn-scoped agent state, reset only when a new turn begins; tool and verification stop-gate continuations do not renew it. A second bare marker on a human turn reaches the existing gateway fallback.
- The first marker and nudge are request scaffolding only; remove them from completed or aborted live/result history regardless of position, preserving real tool-call/result rows and role alternation. Neither may become a durable transcript row.

## Provenance and patches

- **Identity and status:** `gateway-silence-reprompt`; active fork adaptation.
- **Source / fork refs:** maintained fork `origin/main` at the implementation base; fork delivery is the commit carrying `Fork-Patch: gateway-silence-reprompt`.
- **Surfaces:** `agent/turn_final_response.py`, `agent/turn_context.py`, `agent/conversation_loop.py`, `agent/session_persistence.py`, and `tests/agent/test_degenerate_final_recovery.py`.
- **Contribution route and rationale:** narrow core patch; the final-text loop owns the bounded model continuation and supported extension points cannot enforce this native gateway invariant before delivery.
- **Links:** related upstream merged PR [NousResearch/hermes-agent#111624](https://github.com/NousResearch/hermes-agent/pull/111624), which supplies the downstream visible fallback but does not re-prompt the model.
- **Upstream disposition:** related behavior is released upstream as a fallback; no upstream equivalent of this one-shot final-text re-prompt was found.
- **Fork delivery:** publish through the maintained fork PR and protected landing path; do not activate a runtime in this unit.

## Update

Compare the current final-text continuation path with upstream's silence filtering and with the gateway's `silence_allowed(display_kind, reply_expected)` contract. Preserve the queued-terminal metadata path: the terminal turn's own display kind and reply expectation must determine whether the retry applies.

## Verify and recover

Run `scripts/run_tests.sh tests/agent/test_degenerate_final_recovery.py tests/gateway/test_folded_steer_silence_reprompt.py` and the affected gateway silence suites. The regressions cover both marker spellings, one successful re-prompt, machinery and unaddressed controls, the exactly-one retry bound, tool continuation followed by completion or interruption (scaffolding removed, real tool rows intact, next human turn re-promptable), a verification stop-gate continuation (the spent turn budget remains spent and the second marker receives the gateway fallback), and a typed message steered into a running `reply_expected=False` relay turn through the real busy fold (re-prompt, then visible answer; a second marker gets the gateway fallback; a relay-only turn stays silent). Rollback is the focused revert of commits carrying `Fork-Patch: gateway-silence-reprompt`; the existing downstream gateway fallback remains available if the retry is removed.
