# Autonomous Goal Lifecycle

## Contract And Source Ownership

Fork patch identities: `goal-autonomy`, `background-turn-admission`.

`goals.autonomy` defaults to `false`. The existing judge, approval and continuation
behavior remains the default. When explicitly enabled, native goal management
supports agent set, revise, replace, pause, resume and complete without approval.
The external `goal-lifecycle` plugin in `0xble/agents` owns the model tool. Clear
is user-only on that tool in every mode. Native `/goal clear` remains available.

This belongs in core because contract preservation, evidence admission, atomic
completion, and idle wake admission share durable goal state across CLI, gateway
and TUI. The plugin remains a thin caller rather than keeping another goal store
or overriding scheduler internals. No core tool or dependency is added.

## Completion And Contract Preservation

`GoalManager.complete(reason=..., evidence=...)` accepts a mapping from every
exact verification item and additive criterion to a recorded proof excerpt. With
no verification items, the outcome or objective is also a required key. Proof must
match an eligible tool result recorded since this goal began, including archived
rows. Agent prose and goal bookkeeping cannot supply proof. Existing quality
gates run before completion and must pass. This validates recorded provenance
and coverage, while the calling agent owns the semantic adequacy of the proof.
Generic acknowledgments such as `ok` are refused. A proof excerpt must be a
measured passing test count, URL, SHA, or substantive text with at least 2 words
and the existing 12-character evidence minimum. Recorded provenance still binds.

Missing coverage, unrecorded proof, failed gates or a recorded hard-limit breach
refuse completion, keep the goal active and record the named gap. Successful
completion and refusals are appended to revisions and rendered by `/goal status`.
Autonomous mode does not accept the auxiliary judge's DONE as completion.

Autonomous edits patch only named contract fields. Every constraint, boundary,
stop condition, outcome, verification item and additive criterion is retained
unless a current verbatim user removal instruction names the exact item. A
replacement may change the objective. It advances the continuation-instance fence
and requires fresh proof, while preserving quality gates, consumed turns, budget
and breach history. Its reason and before/
after diff are recorded. A removal quote is rechecked under the commit lock so a
newer user message cannot inherit stale authority. Starting another goal after
completion retains the preceding goal's completion history.

PR #781 owns preservation for the existing user-authorized replacement path.
This patch leaves that draft's broader default-off behavior separate and extends
the shared manager and payload normalizer only for autonomous edits.

## Blockers And Waits

A blocked judge verdict becomes CONTINUE with an instruction to choose another
route. It never pauses the goal for input. Explicitly recorded hard-constraint
violations persist as breach events and cannot be escaped through replacement.
Input waits become timed sleeps. PID and session waits have timed rechecks even
when the target stays live. Existing native idle wake admission, CAS markers,
source routing and busy-session fences remain in charge of waking the goal.

Automatic budget and gate-retry pauses become timed holds. In autonomous mode,
`max_turns` is a pacing checkpoint: exhaustion records the consumed turns in
history, resets the continuation counter and sleeps before automatically waking.
This permits ongoing model spend. Explicit contract hard limits still bind.
Autonomous resume and replacement do not refund the current counter. Genuine explicit pauses
remain paused. Legacy blocked/input/approval states and automatic pauses migrate
to active timed waits on load. Idle scans include those rows so migration is not
dependent on a new user message. Stale resume_pending takeover continues through
the existing generation-fenced gateway path.
Agent-requested pauses use the same native retry interval as other timed holds.
A reason string or unverified quote is not user authority. A current explicit
user pause quote is revalidated under the session-control write lock and keeps
the goal paused. Native user pauses and default-off controls remain unchanged.
The CLI idle hook refreshes its cached goal snapshot before deciding whether a
tool-authored wait is due. Reload adopts durable state and retains the existing
snapshot/CAS fences.
Cleared and completed rows remain terminal even if legacy wait fields survive.
Load and idle-store scans cannot reactivate those rows.

Autonomous continuation text rides the next user-role injection, never a system
prompt rebuild. It calls for another route or timed sleep instead of stopping
for input. The generated wording registry includes it and preserves all earlier
shipped entries, including wordings whose source branches have been retired.

## Notice Visibility

`goals.notices` defaults to `true`. When false, the shared profile-scoped helper
suppresses routine gateway lifecycle output, automatic CLI/TUI revival, barrier
and judge output, and every persisted `goal_set` receipt, including completion.
The legacy `goals.auto_notices` preference still independently controls inline
receipts. Loop receipts and loop completion output retain their existing policy.
Explicit `/goal status`, `/goal show`, command errors and approval requests remain
visible. Structured tool results and TUI control snapshots remain available.

Terminal session-control records with `authority.via=autonomy` settle their
cosmetic target and requester flags as `target_notice_skipped` and
`requester_notification_skipped`, including when adapters are offline. They still
perform normal cleanup, continuation admission, supersession checks and expiry.
Reconnect does not replay the skipped notices. Quote, button and session
authorities retain their outcome replies. Visibility never authorizes a mutation
or bypasses evidence, inherited requirements or wake fences.

Upstream preflight read root and affected-subsystem guidance at
`0c771d7a5ba7742bd336adb98d30058ba545e327`. Searches for goal notices and the
exact `goals.notices` preference found no applicable upstream implementation.
The correction reuses the visibility helper from `417de3eea2ef23ff4ca7df928d511e4267453eb4`
and the automatic surface guards from `53ca66814c6a47556ab6622d93d5cb7123fd7901`,
adapted to current source and extended to blocked-goal revival and both control
recipients. No upstream contribution or moving-main synchronization is included.

## Verification And Activation Boundary

Run the complete goal and session-control test inventory with the canonical
`scripts/run_tests.sh` through the machine-wide heavy-work queue. Regressions in
`tests/hermes_cli/test_goal_autonomy.py` exercise evidence completion, inherited
requirements, removal quotes, blocker and gate/budget waits, external rechecks,
legacy migration, profile selection, and default-off behavior. Historical prompt
classification runs in `tests/agent/test_synthetic_prompt_legacy_goals.py`.
Notice visibility runs in `tests/agent/test_quiet_goal_receipts.py`,
`tests/gateway/test_goal_notices_config.py`,
`tests/gateway/test_quiet_goal_control_recovery.py` and both
`test_goal_notice_surfaces.py` files under `tests/hermes_cli` and `tests/tui_gateway`.
The companion plugin tests drive the registered goal_set handler through the
native managers and durable readbacks.

The preserved LPG runtime carries the `background-turn-admission` fixture
correction for restart-resume identity coverage. The fixture uses the runner's
native lazy session-state map rather than shadowing its admission methods. This
owns only that test correction, not a new admission implementation. Autonomous
control-receipt regressions exercise the business `goals.notices` switch in both
states. Explicit receipts remain visible and must not invent Telegram approval.

Default-install worktrees can place their shared Git metadata inside the real
Hermes home. Startup recovery must apply its existing pytest checkout exclusion
before resolving or probing recovery markers. Both Git and ZIP paths leave the
running checkout unprobed, while temporary copied installations remain eligible
for recovery tests. The real-home I/O guard is unchanged.

No Hermes gateway or agent.service may be restarted, updated or reloaded by this
change's implementation task. Source merge does not establish live adoption.
Runtime installation and config enablement, real platform probes on each host,
and installed plugin activation require the separately controlled rollout.

## Retirement And Rollback

Retire this fork patch when an upstream release provides the same opt-in
contract-preserving autonomous lifecycle and passes the behavioral regressions.
Rollback reverts this source/test/doc change and disables `goals.autonomy` through
the normal configuration owner. History uses the existing optional revision rows,
so earlier builds can still read the store. Reverting code alone does not reverse
already recorded goal transitions or authorize a service restart.

Stack fit: existing Python, GoalManager, optimistic SQLite snapshots, session
controls, native idle wake admission and pytest. No new dependencies or services.
