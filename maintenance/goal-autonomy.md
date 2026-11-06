# Autonomous Goal Lifecycle

## Contract And Source Ownership

Fork patch identity: `goal-autonomy`.

`goals.autonomy` defaults to `false`. The existing judge, approval and continuation
behavior remains the default. When explicitly enabled, native goal management
supports agent set, revise, replace, pause, resume and complete without approval.
The external `goal-lifecycle` plugin in `0xble/agents` owns the model tool. Clear
is user-only on that tool in every mode. Native `/goal clear` remains available.

This belongs in core because contract preservation, evidence admission, atomic
completion, and idle wake admission share durable goal state across CLI, gateway
and TUI. The plugin remains a thin caller rather than keeping another goal store
or overriding scheduler internals. No core tool or dependency is added.

Contract tests: `tests/hermes_cli/test_lifecycle_policy.py::test_every_authority_cell`, `tests/hermes_cli/test_lifecycle_policy.py::test_hidden_approval_refusal_precedes_request_validation`.

## Declarative Authority Policy

`hermes_cli/lifecycle_policy.py` is the sole permission table for goal and loop
contract edits and session controls. It expands every row below across item owner
(user/agent), trusted actor (user/agent), autonomy (off/on), and current valid-scoped
quote (absent/present). A = allow, Q = require quote, D = deny. The four columns
below describe **agent callers without a quote**. A trusted native user caller is
A in every supported cell (loop `clear` is unsupported for everyone). A valid
quote turns Q into A, never D. Unknown actors/operations
are D; legacy/unknown ownership is user ownership. A quote is valid only after the
existing current-typed-message or authenticated-relay check, full-message refusal
checks, exact removed-item match where required, and typed-origin target scope.
An agent cannot select autonomy as its actor; autonomy comes from effective config.

| Field/delta/action | User owner, off | Agent owner, off | User owner, on | Agent owner, on |
| --- | --- | --- | --- | --- |
| goal / outcome | Q | A | Q | A |
| goal / verification | Q | Q | Q | Q |
| goal / constraints | Q | Q | Q | Q |
| goal / boundaries | Q | Q | Q | Q |
| goal / subgoals | Q | Q | Q | Q |
| goal / stop_when | Q | A | Q | A |
| goal / max_turns | Q | A | Q | A |
| goal / objective | Q | A | A | A |
| goal / revise | A | A | A | A |
| goal / replace | Q | A | A | A |
| goal / pause | Q | A | A | A |
| goal / resume | Q | A | A | A |
| goal / resume_user_pause | Q | Q | Q | Q |
| goal / stop | Q | Q | D | D |
| goal / clear | Q | Q | D | D |
| loop / outcome | Q | A | A | A |
| loop / stop_when | Q | A | A | A |
| loop / budget | Q | A | A | A |
| loop / cadence_faster | Q | A | A | A |
| loop / cadence_slower | A | A | A | A |
| loop / budget_reduce | A | A | A | A |
| loop / revise | A | A | A | A |
| loop / replace | Q | A | A | A |
| loop / pause | Q | A | A | A |
| loop / resume | Q | A | A | A |
| loop / resume_user_pause | Q | Q | Q | Q |
| loop / stop_user_pause | Q | Q | Q | Q |
| loop / stop | Q | A | A | A |
| loop / stop_user_condition | Q | Q | A | A |
| loop / clear | D | D | D | D |

Field rows describe destructive changes/removal; additive goal criteria and
preserved fields stay allowed through `revise`. `outcome` means the goal contract
outcome (not the objective text); loop `outcome` means its prompt. `budget` is a
loop run-count increase including finite-to-zero/unlimited; `max_turns` means any
change to a goal turn budget. Cadence floors and the global tick cap still bind.
The stop-condition field uses that **item's** recorded owner, not just the creator
of the enclosing lifecycle. A user stop commitment on an agent-created loop uses
`stop_user_condition` for stopping it. A user/system pause uses `resume_user_pause`
even when the enclosing goal/loop was agent-created. `stop_user_pause` also
prevents an agent from escaping a user/system loop pause by finishing instead.
Goal `stop` is the clearing
alias, not evidence-backed completion. Loop `clear` is the unsupported alias;
its native user command is `stop`; the unsupported `clear` row is D even for a user.
These distinctions are exercised by the table
boundary test rather than by constructing permissive synthetic ownership rows.

Autonomy explicitly grants goal/loop pause, resume (except user/system pauses),
replace and loop prompt/cadence/budget/stop-condition latitude. It does **not**
release a user's goal outcome or turn budget. Goal verification, constraints,
boundaries and additive criteria remain protected for **both** owners. A reason
is required for every model edit; same-intent wording is not a semantic oracle
that can authorize removing a user's requirements.

Default-off user-owned goal edits retain the native additive revision and
explicit quoted/approved replacement/removal surfaces. The table does not turn
`revise` into a generic user-contract removal API. The native user command is a
trusted caller, not an agent-supplied actor label. Existing snapshot/CAS publication,
transaction-lock quote rechecks, approval receipts and outbox delivery remain in
charge. An agent-owned edit records `authority=agent` when autonomy is disabled;
only actual configuration-derived autonomy records `authority=autonomy`.

`tests/hermes_cli/test_lifecycle_policy.py::test_every_authority_cell` exercises
every expanded cell against real managers and SessionDB persistence, using native
creation and pause APIs. This includes authenticated quotes, scoped controls,
default-off user command surfaces, durable refusal readback and authority receipts.

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
verification item and additive criterion is retained unless a current verbatim
user removal instruction names the exact item. The same protection applies to a
user-set stop condition. Ordinary agent-owned wording, agent-owned stop conditions and agent-owned turn
budgets can change with a reason while keeping the same intent. A user-owned
contract outcome or turn budget requires current user authority to remove/change. Creation APIs
record ownership for the model tools, which are maintained in the agents repo.
Replacement advances the continuation-instance fence and requires fresh proof,
while preserving quality gates, consumed turns and breach history. Its reason
and before/after diff are recorded. A removal quote is rechecked under the commit
lock. Quotes can arrive through a receipt-authenticated relay only when the exact
quote matches the source user's current typed message, and the receipt is audited.
Starting another goal after completion retains the preceding completion history.

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

`goals.notices` and `loops.notices` default to `true`. When `goals.notices` is false, the shared profile-scoped helper
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

`loops.notices: false` hides loop outcome cards and `loop_set` receipts the same
way, and settles autonomous loop control receipts like goal ones.

`notices: hidden` (per section) hides every automatic card for that lifecycle in
the gateway. That covers achieved, failed, blocked and paused outcomes, loop
completion and backstop cards, `goal_set`/`loop_set` receipts and the target's
session-control receipt for every authority. The level is `hidden`, not `none`,
because `hermes config set` turns `none` into null. Approval cards cannot be
answered while hidden, so `request_control` refuses with `approval_cards_hidden`
and asks the agent to quote the user's words, and the watcher fails any older
unposted request with the same code. A requester that was not the autonomous
actor still receives the outcome injection. Replies to the user's own `/goal`
and `/loop` commands, including the `/goal` set confirmation and drafting
progress, and the agent's own replies stay visible. CLI and TUI loop outcome and
wakeup lines follow `loops.notices` the way goal lines follow `goals.notices`.

A hidden stall card (goal blocked, paused or failed, a loop tick-budget pause or
an unachievable stop condition) must not leave the lifecycle silent. The gateway
records it in SessionDB `state_meta` (`hermes_cli/lifecycle_wakes.py`), and the
session-control watcher wakes that exact session with an internal turn through
the shared plugin injector, pinned to the recorded session ID. The agent resolves
the stall within its authority or tells the user in its own reply. Records
survive restarts, refused admission retries on the next pass, and an
undeliverable wake settles after a day. The same card text wakes a session at
most once every 6 hours, so a stall that repeats each turn cannot loop. A user's
`/stop` pause never wakes the agent. Other hidden cards only log, because the goal
or loop row keeps the outcome.

## Pause Authority

`GoalState.paused_by` records who paused a goal (`agent`, `user` or `system`).
It is never inferred from `paused_reason`. Under `goals.autonomy` a session-control
resume lifts only an `agent` pause and refuses any other with `user_paused`, so a
`/goal pause`, `/stop`, Ctrl+C, quoted or system pause stays the user's. Autonomous
pauses are timed waits rather than pauses, so in practice the agent never resumes a
paused goal. A current user quote to resume still applies with quote authority.
Legacy rows load with an empty owner, which counts as not the agent's.

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
`tests/gateway/test_lifecycle_cards_hidden.py` (including the durable stall wake),
`tests/gateway/test_goal_notices_config.py`,
`tests/gateway/test_quiet_goal_control_recovery.py` and both
`test_goal_notice_surfaces.py` files under `tests/hermes_cli` and `tests/tui_gateway`.
The companion plugin tests drive the registered goal_set handler through the
native managers and durable readbacks.

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
