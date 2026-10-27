# Session controls

## Identity

- **Fork patch identity:** `session-controls`

Session controls let one Hermes session pause, resume, clear, or replace another session's goal, and pause, resume, or stop another session's loop. A fresh verbatim quote from the requester's latest typed message authorizes an action when supplied. Cross-session goal clear and replace without a quote are session-authorized and apply immediately. A supplied quote remains subject to the normal freshness and safety checks. A self-targeted destructive goal change without a fresh quote creates an asynchronous Telegram Approve/Deny request, so the requesting session keeps working while Brian decides.

Agents reach this primitive through the `goal_set` and `loop_set` tools, which the `goal-lifecycle` and `loop-lifecycle` plugins in the agents repository register. Those plugins call `apply_control` (and `resolve_target`) when a control action carries `target` or `user_quote`; core itself registers no tool, so a build without those plugins has no initiating entry point.

## Gateway Turn Ownership

A fresh gateway turn has its own task-local delegation scope, even when an inline
producer submits it from a delegated child. Admission masks the producer's child,
immediate-parent and chain-root bindings. New children created by the admitted
turn inherit that turn's session as their parent and chain root. Executor work
inherits the same scope.

This reset never removes a genuine subprocess child marker. Compatible lifecycle
plugins must check the core process-aware delegation predicate as well as the
task-local flag, so a child process cannot gain `goal_set` or `loop_set` access by
entering the gateway again. The required result for both tools is `parent_only`
in that process. The core reset alone does not enforce a plugin's permission check.

Fresh adapter tasks keep the handler's cleared session identity after success,
errors or cancellation, rather than restoring an inherited identity or enabling
the process-global environment mirror. The adapter's explicit inline-reply boundary
restores the caller's bound session variables, delivery capabilities and task-local
working directory. Unbound inline callers remain cleared. The delegation boundary
restores nested delegation bindings, and the existing profile and Hermes-home scopes
retain their own restoration.

Core coverage is `tests/gateway/test_top_level_delegation_context.py` and the
session-context, delegation-binding and subprocess-environment suites. Plugin
compatibility additionally requires registered-handler and real-subprocess coverage
against the lifecycle plugin sources selected for deployment, from a compatible core
checkout with its own interpreter and disposable Hermes homes. Core test results do
not certify the permissions of a separately installed plugin version.

## Required behavior

- Store one audit/outbox record per control under `state_meta` with a 24-hour pending expiry.
- Resolve only bare session IDs or `hermes:<current-profile>/<session-id>` targets; reject unknown and cross-profile targets.
- Treat relay and gateway-authored rows as non-user text. Quotes must be whitespace-normalized, at least 12 characters, present in the latest typed user message after the gateway `[Replying to…: "…"]` pointer is stripped, and drawn from a source message no longer than 4,000 characters.
- Refuse a quote (`user_quote_negated`) when it contains a negation token (`not`, `don't`, `never`, `no`, `shouldn't`, …) or one appears earlier in the same sentence (only `.`, `!`, `?`, `;` or a newline ends the clause). `stop` is a control verb, not a negation. Also refuse (`user_quote_qualified`) when the latest message anywhere contains a negation, a `?`, or a condition, timing or hedge word (`if`, `only`, `when`, `after`, `how`, `would`, `explain`, …), so a trailing refusal ("Clear the X goal? Not now") or a conditional or informational mention never authorizes. The control must match the quote's whole sentence, not the agent-chosen span, so one sentence naming two actions or both a goal and a loop falls back to Approve/Deny. The sentence must also be a direct instruction: optional courtesy words, the action verb, a noun phrase naming only the target, then only filler (or, for a replacement, `with`/`to` the new objective). "Keep the X goal and clear the Y goal" or "Print the text clear the X goal" never authorize. A replacement objective longer than `REPLACEMENT_MAX_CHARS` (1500) is refused outright, and approval cards show the current and proposed goal as separately bounded lines so Approve never applies text the card hid. A refused quote (stale, paraphrased, relayed, from another session, or negated) never grants quote authority. For a cross-session goal clear or replace without a quote, the session-control authority applies directly instead of creating an approval request.
- A valid quote must also state this control (`user_quote_mismatch` otherwise): it names the kind (`goal`/`objective` or `loop`), exactly one action's verbs (clear/remove/drop…, pause/hold…, resume/continue…, replace/change/switch… for goals; stop/end/cancel…, pause, resume for loops), and, for another session, that session's id or a title no other named session shares (whole words; "Deploy v2" never names "Deploy"). A replacement's new objective stays agent-authored, so the quote binds the replace intent and the target.
- A fresh direct removal instruction naming the stop condition, such as "Clear the stop condition" or "Please remove this stop condition now", also authorizes a self-targeted replacement that empties only `stop_when`. The instruction must consist of courtesy words, `remove`/`drop`/`delete`/`clear`, optional determiners, `stop condition`, then optional filler. Compare the objective, all other contract fields and any supplied turn budget under the same write lock. Preserve user-added subgoals for any stop-only edit, including button-approved replacements. When both a goal and a loop exist, callers must resolve which stop condition the user means before selecting a control, because this quote path does not infer referents. A removal never authorizes a nonempty replacement restriction or dropping completion criteria or hard limits in other fields. Deictic instructions such as "Remove what says this" remain in Approve/Deny: the existing quote source strips the gateway reply pointer and supplies no verified referent binding, so neither a pronoun nor assistant-authored reply text grants stop-condition authority.
- Residual risk: quote authority is a verbatim substring matched against deterministic word sets, not an interpretation of intent. It is mitigated by using only the latest typed message, the negation and control-match checks, the full source message shown in the target-topic notice, and Approve/Deny buttons as the fallback.
- Goal resume applies only to a `paused` goal; done, cleared, and already-active goals return `nothing_to_resume`. Pause, clear, and replace require an active or paused goal.
- Button requests are created only for self-targeted destructive goal changes and other controls that lack a valid quote, and only for targets whose session source has an Approve/Deny surface (Telegram). Cross-session goal clear and replace without a quote never wait for a button. The base adapter's text fallback remains for safety, and the quote path still works for any routable target.
- A replacement records `actor="user"` with `authority` `quote`, `button`, or `session` (plus `approved_by` for buttons). The latest such revision resets the entire outcome scope: both judge and continuation label every preceding requirement, contract and dropped criterion as superseded audit history, including requirements in older agent revisions. Subsequent ordinary revisions keep earlier requirements in the new scope binding unless specifically user-authorized. Audit data is preserved; the boundary is derived from existing revision kind/authority, not a second persisted field. Replace notices show `old -> new`.
- Use the existing GoalManager and LoopManager methods for mutations. Goal replacement preserves revision history and refuses without explicit authority.
- Telegram callbacks must pass the existing callback allowlist and resolve through a durable compare-and-set; a second, expired, or unauthorized press must not mutate state.
- Gateway delivery is profile-scoped and drains durable request/outcome rows after restart. The watcher
  runs SessionDB and manager work in the gateway executor and expires requests with an in-transaction
  CAS. An approval's pending check, manager mutation and outcome receipt commit in one SessionDB
  write transaction, so a crash before commit leaves the request pending with nothing applied. It
  marks undeliverable CLI/TUI rows skipped/done rather than growing the outbox forever. The watcher drains every served
  profile's SessionDB under that profile's runtime scope, and an Approve/Deny tap resolves under the
  scope of the profile that owns the tapped chat.
  Requester outcome injection passes the record's requester session ID to the shared plugin injector,
  which refuses a rebound key and carries the original ID in the strict gateway event pin through
  admission and execution. A `/new` or `/resume` race cannot notify a different conversation, and
  refused admission leaves `requester_notified` false for the existing retry/skipped lifecycle.
  `GatewayTurnMixin._strict_session_pin_holds` is the one strict-pin rule outside
  `_hmwa_resolve_session`: `admit_internal_event`, busy admission and the FIFO follow-up drain each
  drop (without an admission receipt) a strict event whose pinned ID no longer owns its key or is not
  the session the drain is running.

## Continuation lifecycle fence

Every gateway goal continuation is stamped by the shared synthetic-event constructor with its
session ID, goal instance (`created_at`), authored-definition fingerprint, and event creation time:
replace or clear/set changes the instance and drops old work, while same-goal edits keep the instance
and refresh the prompt and fingerprint from the current GoalManager when idle ingress or FIFO
follow-up consumes the event. Control approval cards remain strictly bound to the full authored
definition (objective, contract, criteria, gates and revisions), not progress counters or status.
Wake admission, idle ingress (including ordinary non-internal continuations), busy ingress and FIFO
follow-up all require an active matching instance. Resolved-session ingress also compares the stamp
with the actual session ID after routing and Telegram binding recovery, so a /new or /resume cannot
move an old continuation into the replacement conversation. Control wakes use the existing strict
gateway session pin. Unstamped legacy continuations fail closed.

Restart-persisted control outbox records use that same check, retaining their durable discard
receipt. An applied historical pause/clear only removes queued continuations created no later than
the control, leaving a later resume's continuation and human queue items intact. A busy session or
arbitrary queue depth is not an enqueue receipt: the outbox retries until its own continuation is
accepted or superseded. Queue cleanup runs synchronously on the event loop and removes the adapter
slot only while it still holds the inspected continuation. A persisted gateway origin with an
unavailable adapter retains its unsettled outcome flags for retry, including cleanup and notices;
only a missing origin is permanently skipped. Offline continuations still record supersession,
and the existing 24-hour outcome expiry bounds retries.

## Proof surface

### Telegram Topic Retirement

**Fork patch identity:** `telegram-topic-retire`. `/retire [topic|now|cancel]`
and `telegram_topic`'s `retire`/`cancel_retire` actions share the profile-local
`state_meta` record and executor in `gateway/telegram_retire.py`. Pending
retirement gates new user work, but accepts cancellation and already-owned
internal completions. Only an explicit user request to retire or delete this topic
authorizes invocation, including a deferred request after its named conditions
are verified and its result delivered. Finishing, inactivity, splitting or handing
off never grants permission. `now` requires an explicit interruption request.
Retirement waits until the active turn settles before discovering obligations.

`gateway/telegram_retire_work.py` discovers goals, loops, active and paused cron
routes, background processes and watchers, durable delegations and pending
results, and the task ledger's exact-session assignment inbox. Active task claim
tokens cannot be moved by assignment, so they hold retirement until settlement.
Existing tasks retain their original assigner and report recipient. Self-owned
work reports in the destination topic. Task creation and assignment use the
ledger APIs, with exact task/context and typed-assignment readbacks before a
source-origin relay. This avoids both an unreportable `assigned_by != report_to`
row and relay's self-send refusal. Stable, bounded plain-token relay keys and
assignment reconciliation prevent duplicate offers during recovery.
A changed session binding holds retirement for an explicit cancel/retry instead
of guessing which session owns a compacted turn's work. The existing live
exact-title matcher can select unambiguous subject-owner candidates. A bare
command or unresolved owner quietly reuses the source session's normal internal
turn to decide merge versus delete and choose compatible owners, creating a
subject-specific successor only when needed. This wake is pinned to the source
session and retirement generation. A cancelled or superseded wake cannot re-arm
retirement. Active claims are a quiet wait, not a failed retirement. No standing
commitment is cancelled merely because its owner is unclear. A merge hands off
the source context summary (the tool's existing `message` field) even with no
executable items. This context-only settlement is a verified FYI, not a fabricated
task. Actual obligations retain their task-linked handoffs. The merge records
its destination and keeps
existing automation owner keys as shared redirects. Source histories are not
combined. The closing note states the choice, its reason, and continuing owners.
Receipt IDs, transfer intentions and verified moves persist independently of
discovery, so a restart cannot forget an item already moved.

Deletion requires the same generation's complete successful closing SendResult,
including a message ID and the original thread. Partial, deferred, held,
thread-fallback, failed or cancelled sends never authorize it. Callback execution
alone is not evidence. A saved uncertain closing attempt is not replayed. Remote
delete uncertainty probes the exact thread before any CLI fallback, and reads
back absence before removing the existing topic binding and routing index entry.
The session and transcript remain. The user CLI's exact `topic list --id` probe
is preferred when installed. Its DM peer is the bot's dialog, not the Bot API
user chat ID. The strict Bot API missing-thread probe serves other installations.
Housekeeping never retries an unresolved destructive request or sends a probe.
When the user CLI is available, it may passively reconcile exact-topic absence
under the executor lock and the saved generation-matching closing receipt, then
finish unbinding. Existing, unknown or incomplete lookup results, disconnected
transports and invalid receipts leave the record quiet and unchanged. `/retire`
or `/retire cancel` explicitly probes the exact topic under the same lock.
Confirmed absence only cleans up its binding. Confirmed existence permits
cancellation or a fresh generation that inventories again and requires a new
closing receipt. An unknown result never repeats the previous dispatch.

Proof surfaces: `tests/gateway/test_telegram_retire.py` and
`test_telegram_retire_work.py`, plus the existing topic-tool, topic-mode,
process-registry, async-delegation and completion-delivery regression suites.
Live validation must use disposable topics and preserve their session histories.
Roll back the gateway/tool hooks and retirement modules together. The additive
metadata may remain ignored by older versions. Retire this fork patch only after
upstream provides equivalent durable handoff and delivery-before-delete behavior.

Core regression tests cover quote freshness, relay rejection, target resolution, immediate controls, pending approval, atomic resolution/expiry, goal replacement, revision isolation, and loop actions. Gateway tests cover the non-blocking watcher/outbox behavior, Telegram-topic metadata, route skipping, continuation cleanup, and goal-resume admission. Telegram tests cover callback authorization, callback rendering that preserves the request text, and no-op second presses.

## Upstream disposition

The `hermes_cli/session_controls.py` core primitive, `GoalManager.replace`, gateway outbox watcher, base adapter fallback, and Telegram `ctl:` buttons are upstream candidates. The fork patch remains local until the S1 review and landing path is authorized.

## Loop Wake Fence

Every loop tick's wake, prompt or slash command, carries its loop instance (`created_at`) and claimed tick (`ticks_fired`); a command wake is marked `loop_wakeup_command`, because the scheduler completes its tick right after dispatch, so its fence checks instance, tick and active status but not `awaiting_response`. Admission (`run_inbound`, `run_busy`, `wake.admit_internal_event`) and execution (`_hmwa_resolve_session`, the queued follow-up consumer) drop the wake unless the stored loop is still active, awaiting that tick, on the same instance. So a stop or pause approved after the claim never starts the obsolete prompt. An unreadable store admits the wake, because a control is itself a committed write to that store.

## Replacement Cards

A goal replacement installs its goal, contract and max turns, so the approval card shows all three: the proposed contract (or that the current one is dropped) and the max turns (or "unchanged"). Payload keys outside `goal`, `contract` and `max_turns`, a non-integer `max_turns`, and a goal plus contract longer than `REPLACEMENT_MAX_CHARS` are refused when requested.

## Quote Source Hygiene

- Only current rows count: `messages_by_role(..., current_only=True)` keeps live and compaction-archived user rows and drops rewound (`/undo`) or superseded ones.
- A gateway reply pointer whose end is ambiguous (the quoted assistant text itself contains `"]` plus a newline, so the shortest and longest strips differ) refuses quote authority (`user_quote_ambiguous`) instead of guessing.
- A message containing quotation marks (double, typographic, or an ASCII single quote at a word edge; apostrophes inside words such as `don't` are fine), a backtick or code fence, or a `>` block quote (judged on the raw message, before whitespace is collapsed) is treated as quoted data, not an instruction (`user_quote_qualified`).
- A replacement payload is normalized once (stripped goal, full contract, positive integer max turns) and stored, shown and applied in that form.
- Loop completion judges only the tick the completed turn's wake carried (`loop_tick` from the terminal turn's identity, passed to `complete_tick(tick=...)`), so a stop, re-set and new tick before the hooks run never lets the old response finish the new loop.
- A turn that ran no loop wake owns no tick: the post-turn loop hook settles only the tick the turn or its queued chain ran (`queued_chain_loop_tick`, the deepest loop wake in the chain). With no tick it leaves an in-flight tick claimed by this gateway process to its own wake, and only releases one claimed before this process started, whose wake can no longer arrive.
- The loop scheduler injects every wake through `admit_internal_event`, so an adapter refusal without an exception rolls the claimed tick back for the next scan. A command tick that a busy adapter dispatches inline (`_dispatch_inline_reply`) is stamped admitted once its handler ran, so the tick counts toward `times`/`max_ticks` instead of being rolled back after the command already executed. A command tick's fence accepts its own tick after the post-admission completion reaches the run cap (`done`) or tick budget (budget `paused` reason); a user or control pause or a stop drops it. A missing loop row admits the wake, because rows are never deleted.
- Wakes are fenced at admission, at session resolution, and again at the model boundary (`_internal_wake_input_at_execution`, right before `_run_agent` for both the head turn and a queued follow-up, recognizing wakes by their goal-continuation or loop stamp rather than the `internal` flag, since goal continuations are built with `internal=False`), so a control committed while the turn was being prepared still drops the obsolete wake, and an edited goal's continuation runs its refreshed prompt.
- The latest message must be exactly one sentence, the instruction itself (`user_quote_multi_sentence` otherwise). Another sentence can revoke, correct or reframe it ("Disregard the next sentence. Clear ...", "Clear ... Actually, keep it"), so any multi-sentence message falls back to Approve/Deny instead of being interpreted.
