# Async delegation capacity admission

Load this unit when changing background `delegate_task` admission, capacity
handling, pending work visibility, cancellation, or completion routing.

## Lock-owned lifecycle table

`tools/async_delegation.py` treats each record as one state machine under
`_records_lock` (a re-entrant lock only so an already-complete Future can invoke
its callback synchronously):

| State | Durable precondition | Allowed next state | Capacity rule |
| --- | --- | --- | --- |
| `new` | no ledger row yet | `queued` | no admission eligibility |
| `queued` (persisted) | durable INSERT committed | `admitted` or `cancelled` | no executor slot |
| `admitted` | conditional `queued -> admitted` committed; no Future yet | `running` only after a Future is attached, or terminal failure/cancellation | slot reserved before submit |
| `running` | Future attached and conditional `admitted -> running` committed | `completed`, `failed`, `interrupted`, `stalled`, or `unknown` | slot held until Future done callback |
| `terminal` | conditional UPDATE from the expected prior state committed | none | slot remains held if a Future is still running |

Admission reserves the slot while holding the lock, then submits the Future;
the Future's done callback is the sole normal release authority, including when
the worker returned before `submit()` returned and when stale/force finalization
reported a terminal result early. A queued cancellation is serialized with the
initial INSERT, never submits a runner, and updates an existing row rather than
using `INSERT OR REPLACE`, preserving `event_json` and `result_json`. Every
conditional write treats a zero-row result as a reconcile path; it never submits
or silently advances in-memory state on that result. `_admit_pending` runs only
from a new persisted record or a slot-release callback and loops to capacity.

- A gateway or other async-capable session must never run a rejected background
  delegation inline merely because the async pool is full. The tool returns a
  non-blocking `queued` handle while bounded pending capacity remains, and a
  clear non-blocking rejection when that queue is full.
- The existing synchronous fallback remains for sessions that cannot receive a
  detached completion (`no_async`), including one-shot, cron, Kanban, and
  stateless HTTP paths.
- Pending work is admitted when a slot finishes, preserves the original routing
  identity, and is visible to `delegate_task(action='list')` with cancellation
  honoring `/stop` and session teardown.
- All independent completion units from one `delegate_task` call reserve one
  capacity slot and one bounded queue reservation. If queued, the units are
  admitted together when that slot frees; a sibling is never silently dropped
  because an earlier sibling consumed the queue limit.
- Queued responses advertise only their `delegation_id` controls. They do not
  claim that a live `subagent_id` exists before admission.
- Pending admission is bounded. Queued state is durable or is surfaced as an
  explicit interrupted/unknown outcome on owner restart; it must not disappear
  silently.
- Sibling-group admission is all-or-nothing: the queued-to-admitted durable transition updates every selected sibling in one transaction before in-memory promotion. Submission then settles the whole selected group: each record becomes running only after its own Future is attached; siblings without a Future are restored to queued in FIFO order, or terminally failed together when the retirement fence closes. An admitted or running record without a Future is recovered by the stale monitor after the short grace period. Cancellation treats an admitted no-Future record like queued work and terminally claims it; recovery operates on durable rows in one transaction, while stale-monitor requeue is conditional per record.
- The durable INSERT commits independently of retention pruning. Post-insert
  housekeeping failures are logged and do not convert an accepted queued
  dispatch into a rejected in-memory-only record.
- Dispatch callers use an explicit accepted result flag. Any accepted unit,
  including one that reaches a terminal state before dispatch returns, is handled
  as asynchronous work and is never re-run synchronously.
- Admission persistence, submit-failure finalization, and queued cancellation run
  in the queued unit's captured profile context. A process serving multiple
  profiles must never write another profile's `state.db` or completion manifest.
- The existing stale-delegation monitor also retries pending admission, so a
  transient retirement prepare fence reopening retriggers the queue without
  requiring another completion.
- The low-level async registry defaults to `max_queued_delegations=0`, preserving
  reject-at-capacity behavior for direct callers. Only the `delegate_task`
  background path opts into bounded queueing through
  `_get_max_queued_delegations()`.
- Queued and admitted-but-unstarted records keep the stale monitor alive.
  Retirement requeue wakes the monitor, and its exit decision is atomic with
  monitor startup so admission cannot lose a wakeup.

## Independent hypothesis (frozen 2026-10-04T19:14:32Z, before upstream search)

Current `tools/delegate_tool_dispatch._dispatch_background` treats the async
registry's capacity rejection as a reason to call `_run_sync_with_note()`.
That call executes the entire batch in the gateway tool handler, so the parent
turn cannot drain queued user messages until every child returns. The correction
belongs at the shared async admission boundary, not in gateway adapters: retain
`no_async` inline behavior, but give async-capable delegation units a bounded
pending-admission queue and a distinct queued result.

The narrow solution is a process-local bounded queue backed by the existing
async-delegation records. Queued records retain the same owner/routing and
interrupt callback as dispatched records, become runnable when `_finalize`
releases a slot, and are included in live control/list views. A call-wide slot
key makes independent completion units share one queue reservation; admission
promotes every queued sibling for that key together. Persisting the queued state
allows restart recovery to mark an unadmitted unit explicitly unknown instead of
silently losing it. The queue cap bounds pending calls; queue-full admission is a
non-blocking rejection. Queued responses expose delegation handles rather than
pre-admission child-agent ids.

Alternatives rejected for now: raising the global worker count (removes the
safety invariant and still permits unbounded work), sleeping/retrying in the
handler (still blocks the gateway), or inline fallback (the incident behavior).
A durable cross-process scheduler is broader than this defect and is not needed
for process-local background delegation; restart recovery must nevertheless
leave a truthful terminal record/event.

Expected failing regression: occupy the configured async slot, call
`delegate_task(background=True)` from an async-capable session with a child
that waits on a gate, and prove the call returns a queued handle before the
child gate opens. Additional coverage must prove slot-release admission,
queue-full rejection, queued cancellation, and owner completion routing.

## Upstream status

Fork patch identities: `Async delegation capacity admission`.

Search performed against `NousResearch/hermes-agent` on 2026-10-04 after the
independent hypothesis was frozen.

- **Exact, closed without merge:** [PR #80526](https://github.com/NousResearch/hermes-agent/pull/80526), `feat(delegation): bounded resource-aware background admission queue`, proposed a larger FIFO admission queue with `max_queued_delegations`, timeout, memory/PSI gating, queued interruption, persistence, and restart recovery. Its implementation is the closest prior art, but it was closed without merge; the fork patch keeps the smaller defect boundary and does not copy its resource-governor or timeout machinery.
- **Exact, open policy alternative:** [PR #123145](https://github.com/NousResearch/hermes-agent/pull/123145), `feat(delegation): add delegation.at_capacity policy (sync | reject)`, adds an opt-in `sync | reject` choice and preserves synchronous fallback by default. It confirms the same root cause and has focused tests, but does not provide queued admission; this patch intentionally chooses bounded queueing for async-capable sessions so the default path cannot block the parent.
- **Related incident:** [Issue #52868](https://github.com/NousResearch/hermes-agent/issues/52868) was closed as a duplicate of [Issue #52484](https://github.com/NousResearch/hermes-agent/issues/52484). It documents pool exhaustion causing repeated sequential session creation and token explosion, matching the observed silent-fallback failure mode. The issue's referenced PR #52557 addresses per-turn spawn limits, not non-blocking capacity admission.
- **Related failure mode:** [Issue #63769](https://github.com/NousResearch/hermes-agent/issues/63769) remains open for a saturated pool crashing the synchronous fallback with missing `_initializer`; it is a Python 3.14 daemon-pool compatibility problem, not the parent-turn blocking contract fixed here.
- **Related but not equivalent:** [PR #49690](https://github.com/NousResearch/hermes-agent/pull/49690) uses the executor's unbounded internal queue for batch tasks, which does not bound independent background calls or provide queued lifecycle/control visibility. [PR #102112](https://github.com/NousResearch/hermes-agent/pull/102112) adds dependency-aware scheduling and is broader than this fix. [PR #109940](https://github.com/NousResearch/hermes-agent/pull/109940) addresses restart delivery of already-admitted completions, not admission at pool capacity.

Current `upstream-live/main` still contains the synchronous `at capacity;
running the whole batch synchronously instead` path and has no
`max_queued_delegations` implementation. The selected fork design therefore
remains a narrow core patch: preserve `no_async` synchronous behavior, add a
bounded FIFO for async-capable calls, reject queue overflow without running
inline, and retain synchronous fallback only for non-capacity scheduler failures.

## Surfaces and verification

Primary surfaces: `tools/delegate_tool_dispatch.py`, `tools/async_delegation.py`,
`tools/delegate_tool_config.py`, `tools/delegate_tool_registry.py`, and focused
`tests/tools/` plus gateway stop/delivery tests. Run the focused regression and
affected modules, then `./bin/ci preflight` and the exact-SHA gate contract.

Retire this fork patch when a released upstream Hermes version provides the same
non-blocking admission, bounded pending queue, cancellation, restart truth, and
completion-routing contract. Roll back by reverting the commit carrying this
unit and its tests/record; queued rows use their existing terminal/unknown
recovery path and require no destructive schema rollback.
