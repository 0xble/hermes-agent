# Agent-requested native update lifecycle

```console
hermes gateway update --reason "Activating the delegation-label fix."
```

The reason is required, at most 240 characters, and one nonblank paragraph. Validation precedes IPC or writes. The live gateway resolves the caller's direct or nested session ancestry from its own database. It uses the same detached updater, durable route and progress watcher as `/update`, without a synthetic user message. Managed, non-git and non-messaging origins fail closed.

After acceptance, end the initiating turn immediately. Do not wait for the updater, write marker files or issue another restart. The native updater owns graceful drain and restart; background delegations count as active work. Waiting for completion inside the initiating turn would wait on its own drain.

The supplied reason is reused in separate Updating, Restarting and final notices. Progress remains native output. Pending state preserves the reason, original topic, acknowledged progress offset and phase delivery flags across restart. Failed sends retain the record for retry. Legacy/manual records without a reason remain readable.

A successful notification requires the detached process to have exited, a finalized native receipt for this request and matching runtime revision evidence. The updater's pre-restart zero exit marker alone is insufficient. Missing evidence times out as unknown runtime state, never successful completion. Recovery wording promises only an attempt where supported, not that every interrupted task resumed.

The notification deadline bounds watching, not the updater's lifetime. If termination is still unknown, an accepted timeout notice is checkpointed separately: the pending/claimed admission marker and all update files remain owned by that request, and another launch is refused. Startup recovery can resume the existing bounded watcher without repeating an acknowledged timeout notice. Canonical completion can still deliver the final notice and release admission; a known process exit with missing runtime proof may finalize as unverified at the deadline. Do not delete the markers to retry an unresolved updater.

Upstream PR [107445](https://github.com/NousResearch/hermes-agent/pull/107445), associated with issue [107427](https://github.com/NousResearch/hermes-agent/issues/107427), supplies the Linux systemd cgroup escape. This branch adopts its commit with authorship rather than maintaining another implementation. The remaining local delta covers the agent handoff, reason, durable notifications and stricter completion evidence. The updater retains its native profile/fleet behavior; deployment authorization is an operator boundary, not a default-profile product restriction.
