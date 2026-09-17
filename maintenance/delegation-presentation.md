# Delegation presentation and receipt ownership

This responsibility covers parent-owned cards, stable references, display windows, dismissal and transport receipts. The [root contract](../MAINTENANCE.md)
requires it on every run and owns shared adoption/publication policy.

## Coupled current adaptations

The same review's malformed replacement cleanup finding did not match the
candidate: `_release_replacement_claims` already checks `isinstance(..., dict)`
before reading claim fields. A direct reproduction and real card-claim cleanup
regressions verify that truthy string/list siblings do not prevent release after
a lost validation acknowledgement. No production cleanup change was needed.

Initial background children execute in process-local daemon threads and report
card events through captured in-memory callbacks. They do not survive a gateway
process restart. Recovery replays durable `async_delegation` result notifications,
not claimless `subagent.complete` callbacks. That callback guard retains its
admitted-resume claim requirement. A separate proven crash window can commit the
terminal result while its asynchronously scheduled card observation is pending.
Recovered result notifications now carry their durable delegation IDs into result
processing. The card's own profile ledger must prove the immutable owner, parent,
thread, original call, explicit attempt and child session before an unknown row
can adopt a recorded per-child terminal status. Aggregate batch outcomes,
presentation and handled receipts are insufficient, and ambiguous or duplicate
evidence leaves the row unknown. Completed members of a recovered partial unit
can resolve while its unrecorded siblings remain unknown.

Reconciliation preserves handling, presentation and delivery receipts and prior
attempt history. It starts the ordinary terminal display window and never
manufactures a child composition-turn identity or nested delivery receipt.
`tests/gateway/test_recovered_card_terminal_evidence.py` reproduces the crash
window through real durable storage, notification injection and card recovery,
with stale, foreign, missing and conflicting evidence refusals.

Audited presentation dismissal now overrides original-call batch retention,
including ancestor projection, transport binding and late admission callbacks.
Ordinary handled-batch retention and execution outcomes remain intact. Focused regressions cover this boundary.

Unknown members deliberately block the all-terminal countdown, whether recovered
after restart or reported by a completion event: neither proves execution finished.
They have no automatic display-expiry deadline under this contract. Audited
operator dismissal can remove their presentation without inventing execution or
delivery evidence. The batch-TTL regression exercises both sources, a completed
sibling, far-future reconciliation, audited dismissal and a completed resumed
attempt that starts the ordinary countdown.

## Compact delegation activity copy

User-approved presentation-only refinement: completed and idle-running rows omit
the activity subline, as do generic Error/Failed/Interrupted states (symbols remain).
Specific blockers and observed tools remain, and deferred rows show their full detail
without a prefix. Task symbols,
labels, root windows, hierarchy and all lifecycle/receipt semantics are unchanged.
Verify with the delegation cards, anchoring, dispositions and root-cap suites.
Revert the renderer change to roll back; activation remains parent-owned.

## Delegation root window — source candidate

User explicitly authorized the newest-five root-group display cap on 2026-09-13,
superseding the earlier no-row-truncation comment. Preserve the full lifecycle
ledger and whole descendant groups, with admission (not activity) chronology.
`display.delegation_max_visible_roots` uses the existing profile/platform resolver;
invalid non-null values use five. This is not result retirement or flood control.
The coupled hardcoded label policy derives the admission limit from actual runtime
parent depth: 24 → 20 → 16 → 12 Unicode code points, floored at 12. Admission
rejects a whole invalid batch before side effects; never truncate. Grandfather
existing same-row resume labels unchanged and keep canonical profile-role names.
No label configuration framework or additional display switches are introduced.
Schema/runtime length agreement and whole-batch refusal are covered in
`tests/tools/test_delegate_required_labels.py` and the real saved-resume suite.
Retire this fork delta only when upstream supplies equivalent whole-root semantics
and persisted admission ordering across consolidated execution records. Verify with
`tests/gateway/test_delegation_root_cap.py` plus the existing anchoring, disposition,
and retention suites. Activation is separate and parent-owned; preserve both
capabilities in the existing runtime-compatibility manifest on any installation.

## Delegation terminal-card display TTL — source candidate

`display.delegation_terminal_ttl_seconds` is a display-only original-call batch
TTL (default 300 seconds). Complete immutable birth rosters span independent
completion units and nested owners. All members must be terminal before the
countdown starts; running, queued and missing members block it. Handling and final
delivery do not shorten it. Duplicate terminal events do not reset it, restart
preserves it, and resume reopens the original batch. Legacy rows without call
metadata retain the per-attempt fallback. Expiry uses one coalesced timer per shared
presentation scope and only removes a confirmed status message. It never retires
rows, releases result/handling obligations, or mutates approvals/dispositions.
Root-cap selection precedes TTL pruning; active descendants retain expired terminal
ancestors as context. Legacy terminal rows without a valid timestamp hide
immediately without fabricated history. Verify with
`tests/gateway/test_delegation_card_batch_ttl.py`,
`tests/tools/test_delegation_original_call_metadata.py`,
`tests/gateway/test_delegation_card_ttl.py` and the existing cards/anchor/reconcile
suites. Source landing does not activate or restart a gateway; parent review owns
promotion.

## Deferred follow-through and durable presentation — source candidate

- **Recovered receipt budget:** An old retained-card result recovered once keeps its renewed delivery budget across restart. Original completion age cannot park it again before attempts are spent. Preserve the actual attempt cap and single recovery allowance when adopting upstream replay changes. `tests/tools/test_async_delegation.py` owns persisted restart coverage; reverting the age-condition correction can strand already-recovered rows.

- **Contract:** [follow-through](../website/docs/developer-guide/delegation-followthrough.md). Logging preserves callback receipts; a new same-owner completion re-presents a bounded page of exact retained deferred results for explicit disposition. Admission is non-claimable but not delivery; durable user-row receipts and restart replay close the volatile-queue loss window.
- **Verification:** real logging/relay/card and temp-SQLite result retrieval, exact-owner/attempt rejection, re-deferral, canonical presentation, queued input metadata, restart/race/replay-budget tests. Exact candidate native review required before landing. No live state cleanup or independent-completions default changes.
- **Retirement/rollback:** fork PR166 introduced return-valued callback semantics into an upstream logging wrapper. Retire when upstream satisfies these complete contracts; no matching released fix found. Managed updates enforce the admitted-reader and guard-continuity capability floor, even with zero admitted rows. This avoids count/writer races across served stores. Unverified merges and stash overlays require a reviewed compatible revision. Manual Git and legacy installers are outside this boundary; see the contract for scope. Never fabricate ledger clearance. Source landing and runtime activation remain separate.

## Explicit delegation result disposition — source candidate

- **Contract:** [result disposition and continuation](../website/docs/developer-guide/delegation-result-dispositions.md). Track only exact terminal attempts delivered to a processing turn; one narrow boundary correction; persist incorporated/blocker/deferred disposition before response; verified delivery retires, deferred/failed delivery remains visible.
- **Continuation:** existing native resume grants and leases preserve durable child/session/logical row, frozen route and label. Attempt history links revision only at native admission. Unknown tool effects, uncheckpointed legacy identities, user stops, active leases and ambiguous starts fail closed instead of duplicating work.
- **Upstream:** gateway card implementation is absent upstream (live contents lookup 404); PR search found no matching shared contract. Fork-specific; no unrelated PR update or new issue.
- **Verification:** real temporary SQLite checkpoint/delivery tests and existing delegation/cards/owner/resume/interruption suites. Exact-candidate native review is mandatory before fork merge; the user authorized the configured native availability chain, including isolated Astra fallback explicitly labeled same-provider/non-independent. Runtime promotion/restart and historical reconciliation are separately unauthorized here.
- **Review-handoff boundary:** a `review_dispatched` turn ends at a phase boundary with no assistant answer, so it never triggers the bounded correction or the missing-disposition warning. It durably marks only this turn's exact missing owner/ref/attempts in the existing card ledger. The next text-capable turn (including after reload, without a new child arrival) retrieves a bounded page of those actual owner-verified results and re-registers them under the new turn ID before disposition. Locators alone grant no authority; unrelated owners, unpresented siblings and superseded attempts do not carry forward. Repeated handoffs retain the obligation; a fresh explicit disposition clears its marker, but retirement still requires verified delivery. Missing/oversized payloads remain retained, not auto-presented; the canonical contract documents paging and pre-save crash limits. Tracking failures remain reported. This reconciles PR166 with PR191; changing either requires re-checking the other. `tests/gateway/test_review_handoff_dispositions.py` owns the real-ledger multi-turn/reload/delivery regression; agent warning tests cover only suppression. Reverting the replay code can strand saved handoffs on an older reader; retained results still require exact result retrieval, not ledger deletion. Parent owns review, landing and activation.

## Symbol-first delegation presentation

- **Contract:** [symbol-first presentation](../website/docs/developer-guide/delegation-card-anchoring.md#symbol-first-presentation). Status symbols, no visible numbering/ref prefixes, inline roles, no heading or leading blank line, canonical tool names only, two-space activity inset and actual-parent indentation capped at three layers. Stable internal refs/ownership and exact handling remain unchanged. This supersedes historical tool-preview/display-prefix contracts below, not their lifecycle protections.
- **Upstream:** inspected upstream `main` at `45a6101f36576367359c171cd5820ee76a3d047b`; `gateway/delegation_cards.py` is absent (contents API 404), and matching delegation-card PR search was empty. Fork-local projection, not an independently applicable upstream change. No model-preset PR changes.
- **Verification:** symbol/status table, unknown-state fallback, native Telegram MarkdownV2 send/edit boundaries, three-layer actual parentage, duplicate-label independent ownership and exact handling; existing card/reanchor/consolidation/final cleanup regressions. Native client pixels are separate delivery evidence, never implied by mocked Bot API tests.
- **Rollback:** revert only this presentation change; no migration or lifecycle data edits. PR159 scheduler-deferred cleanup code remains untouched.

## Delegation presentation reanchoring — source candidate

- **Stable subject:** `fix(telegram): reanchor active delegation cards after conversation displacement`.
- **Contract:** [activity-driven anchoring](../website/docs/developer-guide/delegation-card-anchoring.md): six observed same-topic ordinary messages, no elapsed-time eligibility gate; shared API spacing and flood cooldowns remain mandatory, genuinely running rows only, no timer or Bot API polling. One logical card with delete-first replacement: persist deletion/send phases, confirm old absence before sending, accept a short no-card gap, render fresh after scheduler waits, and retain ambiguous-send fences across restart. Legacy send-first receipts keep exact-old cleanup. Uses existing shared expendable scheduling/final-reply priority. Preserves original starts/refs and current NBSP three-layer, heading-only bold, inline role, tool-only renderer; no label truncation or separate review card.
- **Lifecycle:** explicit audited startup dismissal uses the existing exact-target validator; changed targets reject atomically and unrelated live rows survive. Does not forge `handled`, change durable task outcomes or infer success from age/prose.
- **Verification:** real manager/persistence/Telegram adapter with fake Bot API covers interleaving, coalescing, 429, cancellation/ambiguity, restart, old cleanup and final priority. Live activation/transport evidence belongs to the delivery receipt, not source-test assertions.
- **Rollback/retirement:** revert the scoped commit only after reconciling any persisted replacement receipt/obsolete ID; older readers do not understand an in-flight replacement. Preserve task/dismissal history. Retire when adopted upstream passes the complete anchoring and exact-retirement contract.

## Exact parent handling for delegation cards — active

- **Stable subject:** `fix(delegation): retire terminal rows only on exact parent handling`.
- **Contract:** terminal return/failure/interruption remains awaiting parent. `delegate_task(action="handle")` attests exact owned refs and incorporation or a composed blocker report. Root handling commits only on successful final delivery (or explicit silent incorporation); nested parents can incorporate only their own children. Receipt arrival, elapsed time and prose never infer handling. Failure outcomes are unchanged.
- **Recovery:** handling intent IDs persist in the existing card store; final outbound obligations carry the exact receipt in the existing delivery ledger and successful replay recovers it. Explicit `tasks[].replaces` is validated before spawn and linked/handled with the confirmed registered child's start in one card save. Failed construction does not handle the old row. Handled ancestors remain grouping labels while visible descendants exist.
- **Assistant activity:** acknowledged plain/rich replies, physical stream continuations and media count alongside inbound messages toward the six-message activity-only event-driven reanchor gate. Deduplicate physical IDs; exclude edits/drafts/status/card traffic.
- **Verification:** `test_delegation_handling.py`, `test_delegation_assistant_activity.py`, updated card/anchor regressions and delivery-ledger producer/replay tests. Never infer old-history handling from transcripts; only exact existing receipts may reconcile it.
- **Activation:** user authorized one coordinated default-profile activation including already-landed indentation/MoA source. This supersedes the older delegation-overlay no-restart delivery note for this requested rollout only; organizational runtimes remain excluded. Council configuration remains on hold, owned by the parent conversation.

- Two review allegations were disproven through actual boundaries: successful delegation-card replacement receipt adoption already advances the anchor revision and schedules the needed resumed-row edit; the pinned Hindsight SDK omits expansion token fields when their include flags are removed on baseline retry. `tests/gateway/test_delegation_card_anchor.py` and `tests/plugins/memory/test_hindsight_provider.py` preserve these contracts without speculative production changes.

## Maintained patch index

| ID | Status |
| --- | --- |
| HERMES-136 | Active |

## Patch records

### HERMES-136 — Preserve nested delegation card indentation

- **Cause and contract:** Telegram's MarkdownV2 client collapses leading ASCII spaces in ordinary rich-text paragraphs, so rows rendered with `"  " * depth` visually flatten despite retaining distinct `A`, `A.1`, and `A.1.1` references. Use four U+00A0 no-break spaces per rendered depth (capped at the existing two indentation depths: three visible layers) for task and tool rows. Keep hierarchy references, inline roles, the heading-only bold treatment, tool-only rows, and the existing no-preview/no-`Last tool:` contract unchanged. The no-break spaces remain ordinary plain text: no code block, quote, border, wrap-policy, or execution-limit change.
- **Model-facing authoring:** The task-card-row target is 24 characters total, counting four spaces per nesting depth plus hierarchy reference, separators/spaces, inline named role, and label. It remains guidance only: authored labels are neither truncated nor rejected; the tool-name 40-character bound is unchanged.
- **Upstream tracking:** All-state GitHub issue/PR searches for `Telegram delegation indentation` on 2026-09-09 found no exact relevant Hermes issue or pull request. The local visual reproduction and fork-only card renderer establish this targeted correction; no upstream contribution is authorized.
- **Regression:** `scripts/run_tests.sh tests/gateway/test_delegation_cards.py tests/tools/test_delegation_label_guidance.py`. Covers actual Telegram formatter payload construction for initial send and edit, visible depth-one/depth-two prefixes, third-level hierarchy references, tool-only activity rows, preserved full overlong labels, and the model-facing 24-character guidance on both task-label schemas.
- **Rollback:** Revert only `fix(telegram): preserve nested delegation card indentation`, restoring ASCII prefixes and the former 32-character guidance; no schema, configuration, state, or migration is involved.
- **Retirement:** Retire when released upstream preserves visible nested Telegram delegation indentation using an ordinary text-safe representation and retains equivalent formatter/send/edit coverage; remove this fork delta rather than retain duplicate behavior.

- Correction transport validation imports only the native SDK selected by api_mode.
  A base OpenAI installation does not require the optional Anthropic extra, and
  unsupported modes refuse without loading either SDK. Exact native types, zero
  retries and MoA refusal remain mandatory. Real-client import-blocked regressions
  live in `tests/agent/test_delegation_correction_real_loop.py`; retire this scoped
  repair only when upstream preserves optional dependency isolation and the bound.
