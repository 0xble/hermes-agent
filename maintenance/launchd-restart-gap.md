# Launchd Restart Gap

Fork patch identity: `launchd-restart-gap`.

## Contract and Placement

A planned forward-only restart (`request_restart` with
`gateway.forward_only_handover.enabled: true`) exits cleanly and hands the
label to a deferred `launchctl submit` helper. Until the reloaded process
claims a row, the service label has no live generation. That gap is not
retirement. The patch keeps four promises around it:

- `cleanup_exited` never boots out or unlinks the service label's plist. The
  service label is the label of the `active_generation` lease holder, even
  when that lease is `released`, or the install label when no lease exists.
  Labels the service has moved away from, including `ai.hermes.gateway` after a
  promotion to a `g-<uuid>` label, are still retired. Only an explicit
  uninstall removes the service definition.
- The deferred reload writes `<HERMES_HOME>/launchd-reload-pending.json`
  (label, old generation id, nonce, expiry). Cleanup skips that label and the
  guardian stands down entirely while the record is unexpired. The helper
  removes its own record on success and on failure, so a failed reload is the
  guardian's to repair at once. Expiry is the helper's own worst case:
  `2 × reload budget + 15 s`, where the budget is
  `max(30 s, restart drain timeout)`.
- When the installed service plist is missing, the guardian regenerates it
  with the install renderer pinned to `current`, then reuses its existing
  bounded bootstrap repair. It does this only for the home's own install label
  and path, never for a stopped intent (`gateway-guardian-stopped`) or a
  pending reload, and it re-reads both fences immediately before any
  bootstrap. Regeneration is capped at three attempts per rolling hour, then
  the guardian records `regenerate/capped` and alerts. Bootstrap keeps its own
  existing three-per-hour cap. `hermes uninstall` and profile service removal
  record the stopped intent for the plist's own home before removing it, as
  `hermes gateway uninstall` already did, so a guardian tick inside an
  uninstall cannot restore the service.
- Every `alert` or `capped` receipt sends one Telegram message straight to the
  Bot API, never through the gateway. The bot token and home channel resolve
  through `load_hermes_dotenv` and `load_gateway_config`, as the gateway does.
  The token never enters a receipt or log. The persisted per-chat flood
  deadline (`telegram-flood-state.db`, plus its fallback directory) suppresses
  the send. A 429 records its `retry_after` there for the gateway to honor.
  Delivery is deduplicated per `(action, outcome, reason)` per hour through
  the receipt directory, and capped at four sends per hour in total, because
  some reasons carry free exception text. A flood-suppressed alert is retried
  on a later tick once the recorded chat's deadline passes, without resolving
  credentials meanwhile. A sent, failed, unconfigured or in-flight
  (`pending`) alert is not retried within that hour. The receipt is written
  before the send, so a tick killed mid-send never resends.

Every removal of a LaunchAgents plist appends one line to
`logs/launchd-reload.log` naming the caller path, generation id and reason.
That covers generation bootout, refused-standby cleanup, gateway and guardian
uninstall, profile delete and `hermes uninstall`. The reload helper also logs
each bootstrap failure, with launchctl's own error line, and a success line.
Guardian receipts carry the coordinator lease and the latest three generation
rows they observed.

`hermes gateway guardian test-alert` writes one drill alert through the same
receipt, dedupe and flood path.

## Evidence and Provenance

The incident happened on 2026-10-05 on the Mac Studio Personal runtime, release
`d0251975205af0923c5f12127e96be16825e9af5`. A session called `request_restart`,
and the gateway stopped at 11:11:33. The unified log for `launchd` then shows:

| Time | Event |
|---|---|
| 11:11:34 | Generation `7680dc1e` heartbeat ends. Reload helper starts. |
| 11:11:36.362 | `removing service: ai.hermes.gateway`. This is the helper's bootout. |
| 11:11:37.444 | The helper job ends. Its bootstrap succeeded and `launchctl list` showed a PID, so it wrote no retry or failure line. |
| 11:11:37.699 | `removing service: ai.hermes.gateway` again. A second bootout hit the reloaded job. |
| 11:11:37.715 | Guardian receipt: `alert`, "gateway plist missing". |
| 11:11:37.734 | The guardian job ends. |

The deleter was the guardian tick itself. `run_once` calls `cleanup_exited`
before `_run_bounded` checks the plist. The planned restart had released the
lease to exited row `7680dc1e`, and the earlier exited row `321025b9` from the
05:13 restart still carried `ai.hermes.gateway`. That older row was not the
holder, and no live row had its label. So cleanup booted out the label, which
killed the freshly reloaded gateway before it logged anything, and unlinked
the plist. The same tick then reported the plist missing.

The 05:13 restart survived only because no earlier exited row had that label
yet. Every later planned restart was exposed whenever a guardian tick landed
in the few seconds between exit and claim. The gateway stayed down until a
manual `hermes gateway install` at 11:17.

This is fork-only code: the forward-only lifecycle (`259fc0c510`) and the
guardian. Upstream prior-art search was skipped under the local-overlay rule.

## Verification

Run the following through `scripts/run_tests.sh`:

- `tests/hermes_cli/test_gateway_forward_update.py -k "restart_gap or retired_generation_labels or pending_reload"`
- `tests/hermes_cli/test_gateway_guardian_self_heal.py`

`test_restart_gap_never_retires_the_service_label_definition` replays the
incident, once directly through cleanup and once through a guardian tick. It
fails on the base revision because the plist is deleted.
`test_retired_generation_labels_are_still_cleaned_after_the_service_moves`
guards against overcorrecting. `test_reload_helper_logs_bootstrap_failures_and_clears_its_fence`
runs the real generated helper script against a fake `launchctl`.

## Rollback and Retirement

Roll back by reverting the patch commit. Doing so restores the race, the
guardian's alert-only handling of a missing plist and log-only alerts. A
leftover `launchd-reload-pending.json` is inert after a rollback and expires on
its own.

Retire this patch when the forward-only lifecycle and guardian are retired, or
when generation cleanup no longer derives retirement from label liveness. The
out-of-band alert and plist self-heal can be retired independently once an
upstream or external watchdog supplies both.
