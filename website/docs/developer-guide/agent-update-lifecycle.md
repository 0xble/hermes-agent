# Agent-requested native update lifecycle

```console
hermes gateway update --reason "Activating the delegation-label fix."
```

The reason is required, at most 240 characters, and one nonblank paragraph. Validation precedes IPC or writes. The live gateway resolves the caller's direct or nested session ancestry from its own database. It uses the same detached updater, durable route and progress watcher as `/update`, without a synthetic user message. Managed, non-git and non-messaging origins fail closed.

To select an approved immutable target, add `--revision "$APPROVED_SHA"`, where
`APPROVED_SHA` is the exact full 40-character lowercase commit SHA. The CLI validates
it before IPC; the gateway validates it again, retains it with the pending request,
and forwards it to `hermes update --gateway --revision`. This uses the existing
pinned updater path, not checkout of a possibly stale local `main`. Compatibility
guards still refuse targets missing the required reader/guard capabilities. Branch
and upstream movement, dirty-state repair and ZIP fallback remain disabled on the
pinned path; native quick snapshots, cron scheduling and result/ledger data are
unchanged. Omitting the flag retains the existing branch-update behavior.

Pinned requests use the `agent-update-revision` control verb. Older gateways refuse
the unknown verb instead of silently dropping the target. The CLI cannot distinguish
that refusal from a transport failure: `query_gateway_control` returns `None` for
both, so the diagnostic reports unknown acceptance. Do not automatically retry or
fall back to an unpinned request. Inspect the running gateway's version and existing
pending update state before choosing recovery. If the gateway predates this handoff,
an operator-owned bootstrap through the already-supported
`hermes update --revision "$APPROVED_SHA"` requires approval and coordination with
other work, with any pending update reconciled first. A new CLI alone cannot add a
verb to a running old gateway.

CLI exit zero means only that this request was accepted, **not** that source was
updated or a runtime restarted. An existing pending update refuses the new request
without changing its target/reason. An unavailable response leaves acceptance
unknown and must not trigger an automatic retry on another socket or without the
pin. Nonzero gateway handler results propagate to the CLI process exit status.

After acceptance, end the initiating turn immediately. Do not wait for the updater, write marker files or issue another restart. The native updater owns graceful drain and restart; background delegations count as active work. Waiting for completion inside the initiating turn would wait on its own drain.

The supplied reason is reused in separate Updating, Restarting and final notices. Progress remains native output. Pending state preserves the reason, original topic, acknowledged progress offset and phase delivery flags across restart. Failed sends retain the record for retry. Legacy/manual records without a reason remain readable.

A successful notification requires the detached process to have exited, a finalized native receipt for this request and matching runtime revision evidence. The updater's pre-restart zero exit marker alone is insufficient. Missing evidence times out as unknown runtime state, never successful completion. Recovery wording promises only an attempt where supported, not that every interrupted task resumed.

Admission initialization uses an OS-owned file lock separate from the request marker. Output and exit files are initialized before complete JSON is atomically published. A live initializer blocks competing launches regardless of file age; after its death, the next lock owner can replace abandoned staging bytes. The lock file keeps its inode and must not be deleted. Publication is the conservative uncertainty boundary: an unclassified spawn error or interruption before spawn leaves the request fenced until canonical reconciliation. Only executable-not-found or permission-denied classified at the native process-creation boundary proves no child started and permits release of the exact unchanged pending marker, while no claimed marker exists. Legacy empty/partial authoritative markers are not automatically reclaimed, because they lack trustworthy initializer ownership; emptiness or age alone is not proof that retry is safe.

Claimed marker names remain readable for legacy recovery, but the current updater and notifier do not rename pending markers to claimed. The earlier notifier transfer was removed when native lifecycle notification ownership replaced it. Any future transfer must synchronize checkpoint writes with that producer.

The notification deadline bounds watching, not the updater's lifetime. If termination is still unknown, an accepted timeout notice is checkpointed separately: the pending/claimed admission marker and all update files remain owned by that request, and another launch is refused. Startup recovery can resume the existing bounded watcher without repeating an acknowledged timeout notice. Canonical completion can still deliver the final notice and release admission; a known process exit with missing runtime proof may finalize as unverified at the deadline. Do not delete the markers to retry an unresolved updater.

Upstream PR [107445](https://github.com/NousResearch/hermes-agent/pull/107445), associated with issue [107427](https://github.com/NousResearch/hermes-agent/issues/107427), supplies the Linux systemd cgroup escape. This branch adopts its commit with authorship rather than maintaining another implementation. The remaining local delta covers the agent handoff, reason, durable notifications and stricter completion evidence. The updater retains its native profile/fleet behavior; deployment authorization is an operator boundary, not a default-profile product restriction.
