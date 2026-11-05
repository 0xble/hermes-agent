# Lost native synthetic admissions settle interrupted

Fork patch identity: `synthetic-unattended-recovery`.

## Ownership

- Owner: gateway native admission / unknown-execution recovery.
- Scope: `gateway/session_unattended_unknown.py`, the native attended-activity
  projection in `gateway/session_envelope.py`, and
  `tests/gateway/test_unknown_synthetic_settles.py`.
- Incident: a lost native goal continuation remained `unknown_execution` and
  paused the Hermes Reliability topic's FIFO for 68 hours until a human discarded it.

## Upstream relationship

- Upstream precedent: [NousResearch/hermes-agent#141357](https://github.com/NousResearch/hermes-agent/pull/141357)
  merged as `eea83bb603c397396acaf6cc453cf3012a8524aa`.
  Keep its fenced terminal/interrupted resolution, one transactional interruption
  notice, no replay, and continued FIFO draining.
- The native synthetic-descriptor gap is still present on upstream `main`
  (verified from the GitHub Contents API before this patch). The fork base is
  `62fa30c11e0bfcf72dfc506c7081470aaf358ae8`.
- Do not adopt #146094's human-unknown settling behavior. Human or mixed
  human/synthetic unknowns must still pause for a human decision.

## Contract and provenance

Recognize only a schema-valid private `native_text_v1.synthetic` descriptor
projected by `gateway/session_synthetic.py`, and only when its metadata identifies
`goal_continuation`, `loop_wakeup`, `hermes_loop_wakeup`, or `notification_origin`
in `{process_registry_synthetic, delegation_result, relay_delivery, cron_delivery}`.
Presence of `native_text_v1.attended_activity` vetoes this additional predicate.
The native codec also persists `absorbed_synthetic`: an empty list proves no
absorbed inputs; each merged input contributes its private descriptor (or null
without the capability). Recovery requires the head and every entry to identify
a trusted producer. This veto precedes even older unattended shortcuts when a
synthetic/absorbed descriptor is present; unmixed legacy automation remains unchanged.
Restoring and re-merging an envelope retains its trail. A legacy synthetic row
without the field has unknown merge history and conservatively pauses.

Producer fields were checked in `gateway/run_goals.py` (the real goal event
factory), `hermes_cli/session_controls.py` and `gateway/run_loop_wake.py`
and `gateway/run_notifications.py` (`process_registry_synthetic`). The full
allowlist matches the supported external-event identities in `gateway/run_goals.py`
and turn-origin handling in `gateway/platforms/event.py`; `hermes_loop_wakeup`
is the legacy loop stamp consumed by `gateway/run_loop_wake.py`. Modern process
notices can already carry the older `automation` proof; this patch also covers
private native descriptors without that block. No allowlist spelling changes.

Safety detail exposed by RED coverage: an already-marked human follower merged
into a synthetic head previously lost its attended-activity marker in the native
codec. If the head has no receipt and there is no unmarked one, preserve the
newest stamped merged receipt; otherwise retain the existing head clock and
priority for the newest unmarked receipt. This is necessary
for the conservative veto to work in both merge orders, not broader recovery.

Independent-review follow-up: receipt presence is insufficient proof of human
absence. Compression-deferred `merge_text=True` can absorb human input that
never gets a receipt (automation-looking prefixes, edits, reactions, recovered
follow-ups, or an unstamped event). Persist positive absorbed provenance at the
codec boundary instead of consulting the attended-model heuristic. The marked
clock remains provenance only and never refreshes activity.

## Tests and rollout

- RED commit: production unchanged; 10 expected failures and 41 passes across
  the new synthetic file plus existing automation recovery tests. Failures cover
  seven trusted producer identities, real native goal boot/drain recovery, and
  the already-marked synthetic-first merge's missing human receipt.
- Regression contract: schema-malformed/unknown descriptors, quoted continuation
  text, forged metadata/token fields, relay-looking text, normal human Telegram,
  attended veto, and both mixed merge orders (marked and unmarked).
- Follow-up RED `4030092d9b07adcce771a2120ad4c9006dcbc562`: 21 expected
  failures, 52 passes across the synthetic and existing automation files.
  Twelve failures reproduce six unstamped human merge kinds through both boot
  and drain recovery; nine cover nested/roundtrip, unknown producer, legacy,
  malformed-trail and automation-shortcut cases. The original 43 cases pass.
- Follow-up GREEN: 429 passed / 0 failed in 12 files through the official
  `scripts/run_tests.sh` wrapper under the urgent heavy slot, including all 65
  synthetic-unknown cases (the original 43 plus 22 provenance regressions), the
  native codec, attended clocks, queue, automation, Kanban and cron recovery.
- Boot and in-owner drain recovery use disposable stores and a real native goal
  producer event; verify interrupted/no replay, exactly one notice, and one human
  follower execution. Human and mixed unknowns retain the pause.
- Run `scripts/run_tests.sh` with checkout-local `HERMES_PYTHON` and file paths,
  then the committed exact-head `ci-gate gate <sha>` before publishing a draft.
- Draft PR only. No merge, activation, runtime restart/update, or live queue/DB
  mutation. Maintenance owns any later activation.

## Removal condition

Remove after upstream implements the private synthetic classification and both
merge-order human-veto regressions pass on the replacement. Never remove merely
because the older automation-only precedent is upstream.
