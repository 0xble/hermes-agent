# Desktop core completion recovery

Fork patch identity: `desktop-core-completion-recovery`.

## Divergence and provenance

The 2026-10-27 nightly first failed after the v0.22.5 integration
(`de45867aa9552`, integration `e1147915076b2`). Run `41639452032`, job
`125928932460`, confirms the actual failures are `reply-settle-paths.spec.ts`
and `transcript-integrity.spec.ts`, not the clarify/reconnect filenames in the
initial maintenance triage. The X11 header warning is unrelated.

The steer matrix assumed immediate process completion while inheriting the
production 300-second successful-result batching window. Its sandbox explicitly
sets `gateway.completion_notification_batch_window_seconds: 0`; the production
policy, zero retries and 120-second assertions are unchanged. Completion
admission additionally needs the registered local source object, not an equal
reconstruction from the route origin. Only the private producer may bind a
matching identity to its still-live registered source; local ingress remains
object-identity checked.

Canonical replay restores past frames but did not attach the replacement socket
to future authority events. Resume must precede replay, against the retained
epoch's owner profile. Retain the existing renderer settlement path: an idle
active-list poll is not a substitute for the missing subscription and must not
retire stream identity while its replay is still in flight.

## Maintenance boundary

Load this unit when updating local-source admission, canonical replay/attach, or
Desktop active-list settlement. Preserve socket-generation and profile fences.
Do not replace production notification batching with an immediate-delivery
policy merely to satisfy the suite.

Contract tests: `tests/gateway/test_local_process_completion_admission.py`.

Desktop proof: `canonical-reconnect-replay-owner.test.ts` models a server that
subscribes only on resume and withholds future frames otherwise. The real Linux
core suite exercises the two transitions with the scripted model, packaged
Electron build and the unchanged transcript oracle.

Retire this unit when upstream preserves equivalent producer identity,
subscribe-before-replay and authoritative terminal-state reconciliation. Revert
commits with this identity to roll back; restore the fixture window only if its
purpose changes to testing production batching.
